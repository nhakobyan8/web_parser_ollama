from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import escape
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, Tag

NOISE_TAGS = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "object",
    "embed",
    "form",
    "header",
    "input",
    "button",
    "select",
    "option",
    "nav",
    "aside",
    "footer",
}

NOISE_MARKERS = {
    "advert",
    "ads",
    "banner",
    "breadcrumb",
    "cookie",
    "consent",
    "footer",
    "header",
    "menu",
    "modal",
    "newsletter",
    "navigation",
    "popup",
    "promo",
    "recommend",
    "related",
    "share",
    "sidebar",
    "social",
    "subscribe",
    "widget",
    "comment",
}


@dataclass(slots=True)
class CompactPage:
    content: str
    anchors_count: int


def _clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for comment in soup.find_all(string=lambda item: isinstance(item, Comment)):
        comment.extract()
    for tag in soup.find_all(NOISE_TAGS):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag):
            continue
        style = str(tag.get("style", "")).replace(" ", "").lower()
        if tag.has_attr("hidden") or "display:none" in style or "visibility:hidden" in style:
            tag.decompose()
            continue
        marker_text = " ".join(
            [str(tag.get("id", "")), " ".join(tag.get("class", [])), str(tag.get("role", ""))]
        ).lower()
        marker_tokens = set(re.split(r"[^a-z0-9_-]+", marker_text))
        if marker_tokens & NOISE_MARKERS:
            tag.decompose()
    return soup


def _squash(value: str, limit: int | None = None) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if limit is not None and len(value) > limit:
        return value[: limit - 1].rstrip() + "…"
    return value


def compact_discovery_page(html: str, base_url: str, max_chars: int) -> CompactPage:
    soup = _clean_soup(html)
    title = _squash(soup.title.get_text(" ", strip=True), 500) if soup.title else ""
    lines = [f'<page base_url="{escape(base_url, quote=True)}">']
    if title:
        lines.append(f"<title>{escape(title)}</title>")

    seen: set[str] = set()
    anchors_count = 0
    section_label = ""
    for element in soup.find_all(["h1", "h2", "h3", "a"]):
        if element.name in {"h1", "h2", "h3"}:
            section_label = _squash(element.get_text(" ", strip=True), 180)
            continue

        href = element.get("href")
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlsplit(absolute)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        absolute = parsed._replace(fragment="").geturl()
        if absolute in seen:
            continue
        seen.add(absolute)

        text = _squash(element.get_text(" ", strip=True) or element.get("aria-label", ""), 350)
        if not text and element.find("img"):
            text = _squash(element.find("img").get("alt", ""), 350)
        if not text:
            continue

        container = element.find_parent(["article", "li", "section", "div"])
        context = _squash(container.get_text(" ", strip=True), 650) if container else text
        class_hint = ""
        if container:
            class_hint = _squash(" ".join(container.get("class", [])), 160)
        line = (
            f'<item section="{escape(section_label, quote=True)}" '
            f'class="{escape(class_hint, quote=True)}">'
            f'<a href="{escape(absolute, quote=True)}">{escape(text)}</a>'
            f"<context>{escape(context)}</context></item>"
        )
        if sum(len(item) for item in lines) + len(line) + 20 > max_chars:
            break
        lines.append(line)
        anchors_count += 1

    lines.append("</page>")
    return CompactPage(content="\n".join(lines), anchors_count=anchors_count)


def compact_article_page(html: str, page_url: str, max_chars: int) -> CompactPage:
    original = BeautifulSoup(html, "lxml")
    metadata = _extract_metadata(original, page_url)
    soup = _clean_soup(html)
    body = soup.body or soup

    candidates = list(body.find_all(["article", "main"]))
    candidates.extend(
        tag
        for tag in body.find_all(["div", "section"])
        if any(
            marker in " ".join([str(tag.get("id", "")), " ".join(tag.get("class", []))]).lower()
            for marker in ("article", "content", "entry", "post", "story", "news", "正文")
        )
    )
    largest = max(candidates, key=lambda tag: len(tag.get_text(" ", strip=True)), default=body)
    if len(largest.get_text(" ", strip=True)) < 500:
        largest = body

    lines = [
        f'<article_page url="{escape(page_url, quote=True)}">',
        f"<metadata>{escape(json.dumps(metadata, ensure_ascii=False))}</metadata>",
        "<content>",
    ]
    count = 0
    for element in largest.find_all(["h1", "h2", "h3", "p", "li", "blockquote", "time", "img"]):
        if element.name == "img":
            src = element.get("src") or element.get("data-src") or element.get("data-lazy-src")
            if not src:
                continue
            src = urljoin(page_url, src)
            alt = _squash(element.get("alt", ""), 300)
            line = f'<image src="{escape(src, quote=True)}" alt="{escape(alt, quote=True)}" />'
        else:
            text = _squash(element.get_text(" ", strip=True))
            if not text:
                continue
            tag_name = element.name
            attrs = ""
            if tag_name == "time" and element.get("datetime"):
                attrs = f' datetime="{escape(str(element.get("datetime")), quote=True)}"'
            line = f"<{tag_name}{attrs}>{escape(text)}</{tag_name}>"

        if sum(len(item) for item in lines) + len(line) + 30 > max_chars:
            lines.append("<truncated>true</truncated>")
            break
        lines.append(line)
        count += 1

    lines.extend(["</content>", "</article_page>"])
    return CompactPage(content="\n".join(lines), anchors_count=count)


def _extract_metadata(soup: BeautifulSoup, page_url: str) -> dict[str, object]:
    result: dict[str, object] = {"source_url": page_url}
    meta_mapping = {
        "og:title": "title",
        "twitter:title": "title",
        "og:image": "image_url",
        "twitter:image": "image_url",
        "article:published_time": "published_at",
        "article:modified_time": "updated_at",
        "article:author": "author",
        "article:section": "category",
        "og:locale": "language",
    }
    for tag in soup.find_all("meta"):
        key = str(tag.get("property") or tag.get("name") or "").lower()
        content = _squash(str(tag.get("content") or ""), 3000)
        target = meta_mapping.get(key)
        if target and content and target not in result:
            if target == "image_url":
                content = urljoin(page_url, content)
            result[target] = content

    json_ld: list[object] = []
    for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = script.string or script.get_text()
        if not raw or len(raw) > 200_000:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in _walk_json_ld(parsed):
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if any(value in {"Article", "NewsArticle", "BlogPosting", "Report"} for value in types):
                json_ld.append(item)
                break
        if json_ld:
            break
    if json_ld:
        result["json_ld_article"] = json_ld[0]
    return result


def _walk_json_ld(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json_ld(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_ld(child)
