from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx
from pydantic import ValidationError

from app.models import ArticleData, DiscoveryResult

logger = logging.getLogger(__name__)


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_seconds: int,
        num_ctx: int,
        keep_alive: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.keep_alive = keep_alive
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def wait_until_ready(self, attempts: int = 60, delay_seconds: int = 2) -> None:
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                response = await self._client.get("/api/tags")
                response.raise_for_status()
                models = {item.get("name") for item in response.json().get("models", [])}
                if self.model in models or any(
                    name and name.split(":", 1)[0] == self.model for name in models
                ):
                    return
                last_error = OllamaError(f"Model {self.model} has not been loaded yet")
            except (httpx.HTTPError, ValueError, OllamaError) as exc:
                last_error = exc
            await asyncio.sleep(delay_seconds)
        raise OllamaError(f"Ollama is not ready: {last_error}")

    async def discover_latest_url(self, prompt: str, compact_page: str, base_url: str) -> str:
        schema = DiscoveryResult.model_json_schema()
        content = await self._chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You analyze a cleaned page structure. Follow the user's prompt, but "
                        'technically return a JSON object in the form {"url": "..."}. '
                        "Do not add explanations or invent a URL that is absent from the input."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\n\nBase URL: {base_url}\n\n"
                        f"Cleaned page structure:\n{compact_page}"
                    ),
                },
            ],
            response_format=schema,
            temperature=0.0,
        )
        try:
            result = DiscoveryResult.model_validate_json(self._extract_json(content))
            return result.url.strip()
        except (ValidationError, json.JSONDecodeError) as exc:
            match = re.search(r"https?://[^\s<>\"']+", content)
            if match:
                return match.group(0).rstrip(".,);]")
            raise OllamaError(f"The model did not return a valid URL: {content[:300]}") from exc

    async def extract_article(self, prompt: str, compact_page: str, source_url: str) -> ArticleData:
        schema = ArticleData.model_json_schema()
        content = await self._chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract data only from the provided article page. Follow the JSON Schema "
                        "strictly. Do not invent missing values. The text field must preserve the "
                        "complete main article text."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\n\nActual article URL: {source_url}\n\n"
                        f"Cleaned article page:\n{compact_page}"
                    ),
                },
            ],
            response_format=schema,
            temperature=0.0,
        )
        try:
            article = ArticleData.model_validate_json(self._extract_json(content))
        except (ValidationError, json.JSONDecodeError) as exc:
            raise OllamaError(f"Invalid article JSON: {content[:500]}") from exc
        article.source_url = source_url
        return article

    async def process_article(self, prompt: str, article: ArticleData) -> str:
        content = await self._chat(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Transform the structured article data according to the user's prompt. "
                        "Return only the finished output without commenting on your work."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{prompt}\n\nStructured article data:\n{article.model_dump_json(indent=2)}"
                    ),
                },
            ],
            response_format=None,
            temperature=0.35,
        )
        result = self._strip_thinking(content).strip()
        if not result:
            raise OllamaError("The model returned an empty processing result")
        return result

    async def _chat(
        self,
        *,
        messages: list[dict[str, str]],
        response_format: dict[str, Any] | None,
        temperature: float,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "think": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": temperature,
                "num_ctx": self.num_ctx,
            },
        }
        if response_format is not None:
            payload["format"] = response_format

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._client.post("/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
                content = data.get("message", {}).get("content")
                if not isinstance(content, str) or not content.strip():
                    raise OllamaError("Ollama returned an empty response")
                return self._strip_thinking(content)
            except (httpx.HTTPError, ValueError, OllamaError) as exc:
                last_error = exc
                if attempt == 0:
                    await asyncio.sleep(2)
        raise OllamaError(f"Ollama request failed: {last_error}")

    @staticmethod
    def _strip_thinking(content: str) -> str:
        return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()

    @classmethod
    def _extract_json(cls, content: str) -> str:
        content = cls._strip_thinking(content).strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
            content = re.sub(r"\s*```$", "", content)
        start = content.find("{")
        end = content.rfind("}")
        return content[start : end + 1] if start >= 0 and end > start else content
