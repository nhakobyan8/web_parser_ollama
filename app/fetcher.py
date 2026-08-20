from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import cloudscraper
import httpx

logger = logging.getLogger(__name__)


USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
)

BLOCK_MARKERS = (
    "cf-chl-",
    "cf-turnstile",
    "captcha",
    "attention required! | cloudflare",
    "checking your browser",
    "verify you are human",
    "access denied",
)


class FetchError(RuntimeError):
    pass


@dataclass(slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    html: str
    status_code: int
    method: str


@dataclass(slots=True)
class _RawResponse:
    status_code: int
    headers: dict[str, str]
    content: bytes


def normalize_http_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise FetchError("Only complete HTTP/HTTPS URLs are allowed")
    if parsed.username or parsed.password:
        raise FetchError("URLs containing a username or password are not allowed")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{host}{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


async def validate_public_url(url: str) -> str:
    normalized = normalize_http_url(url)
    host = urlsplit(normalized).hostname
    if not host:
        raise FetchError("The URL does not contain a domain")
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
        raise FetchError("Local addresses are not allowed")

    try:
        literal_ip = ipaddress.ip_address(host)
        addresses = [literal_ip]
    except ValueError:
        try:
            records = await asyncio.to_thread(
                socket.getaddrinfo,
                host,
                None,
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise FetchError(f"Could not resolve the IP address for domain {host}") from exc
        addresses = list({ipaddress.ip_address(record[4][0]) for record in records})

    if not addresses:
        raise FetchError("The domain did not resolve to any IP address")
    for address in addresses:
        if not address.is_global:
            raise FetchError("Local, reserved, and private IP addresses are not allowed")
    return normalized


class SafeHtmlFetcher:
    def __init__(
        self,
        *,
        timeout_seconds: int,
        attempts: int,
        max_html_bytes: int,
        max_redirects: int = 5,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.attempts = max(1, attempts)
        self.max_html_bytes = max_html_bytes
        self.max_redirects = max_redirects
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> FetchResult:
        requested_url = await validate_public_url(url)
        last_error: Exception | None = None

        for attempt in range(1, self.attempts + 1):
            try:
                return await self._fetch_with_redirects(requested_url, requested_url)
            except FetchError as exc:
                last_error = exc
                if attempt >= self.attempts:
                    break
                await asyncio.sleep(min(2 ** (attempt - 1), 5) + random.random())
        raise FetchError(str(last_error or "Could not fetch the page"))

    async def _fetch_with_redirects(self, requested_url: str, current_url: str) -> FetchResult:
        for _ in range(self.max_redirects + 1):
            current_url = await validate_public_url(current_url)
            headers = self._headers(current_url)
            response = await self._request_httpx(current_url, headers)
            method = "httpx"

            if self._should_use_browser_fallback(response):
                response = await self._request_cloudscraper(current_url, headers)
                method = "cloudscraper"

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise FetchError("The server returned a redirect without a destination")
                current_url = urljoin(current_url, location)
                continue

            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code} while fetching {current_url}")

            content_type = response.headers.get("content-type", "").lower()
            if content_type and not any(
                allowed in content_type
                for allowed in ("text/html", "application/xhtml+xml", "text/plain")
            ):
                raise FetchError(f"The source returned an unsupported Content-Type: {content_type}")

            if len(response.content) > self.max_html_bytes:
                raise FetchError(f"HTML exceeds the {self.max_html_bytes}-byte limit")

            html = self._decode(response.content, content_type)
            if self._looks_blocked(html):
                raise FetchError("The website returned a CAPTCHA or blocking page")
            if len(html.strip()) < 50:
                raise FetchError("The source returned an empty page")
            return FetchResult(
                requested_url=requested_url,
                final_url=current_url,
                html=html,
                status_code=response.status_code,
                method=method,
            )
        raise FetchError(f"Too many redirects for {requested_url}")

    async def _request_httpx(self, url: str, headers: dict[str, str]) -> _RawResponse:
        try:
            async with self._client.stream("GET", url, headers=headers) as response:
                length = response.headers.get("content-length")
                if length and length.isdigit() and int(length) > self.max_html_bytes:
                    raise FetchError(f"The response exceeds the {self.max_html_bytes}-byte limit")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.max_html_bytes:
                        raise FetchError(
                            f"The response exceeds the {self.max_html_bytes}-byte limit"
                        )
                    chunks.append(chunk)
                return _RawResponse(
                    status_code=response.status_code,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    content=b"".join(chunks),
                )
        except httpx.HTTPError as exc:
            raise FetchError(f"Network error: {exc}") from exc

    async def _request_cloudscraper(self, url: str, headers: dict[str, str]) -> _RawResponse:
        def request() -> _RawResponse:
            scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
            try:
                response = scraper.get(
                    url,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    allow_redirects=False,
                )
                content_length = response.headers.get("content-length")
                if (
                    content_length
                    and content_length.isdigit()
                    and int(content_length) > self.max_html_bytes
                ):
                    raise FetchError(f"The response exceeds the {self.max_html_bytes}-byte limit")
                if len(response.content) > self.max_html_bytes:
                    raise FetchError(f"The response exceeds the {self.max_html_bytes}-byte limit")
                return _RawResponse(
                    status_code=response.status_code,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    content=response.content,
                )
            finally:
                scraper.close()

        try:
            return await asyncio.to_thread(request)
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(f"Browser-mode fetch failed: {exc}") from exc

    @staticmethod
    def _headers(url: str) -> dict[str, str]:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}/"
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,hy;q=0.8,en-US;q=0.7,en;q=0.6",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": origin,
            "Upgrade-Insecure-Requests": "1",
        }

    @staticmethod
    def _looks_blocked(html: str) -> bool:
        sample = html[:200_000].lower()
        return any(marker in sample for marker in BLOCK_MARKERS)

    def _should_use_browser_fallback(self, response: _RawResponse) -> bool:
        if response.status_code in {403, 429, 503}:
            return True
        content_type = response.headers.get("content-type", "").lower()
        return "html" in content_type and self._looks_blocked(
            self._decode(response.content[:200_000], content_type)
        )

    @staticmethod
    def _decode(content: bytes, content_type: str) -> str:
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";", 1)[0].strip(" \"'")
        for encoding in (charset, "utf-8", "windows-1251", "latin-1"):
            try:
                return content.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return content.decode("utf-8", errors="replace")
