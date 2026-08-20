from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

from aiogram import Bot

from app.models import ProcessedRecord, UserProfile

CSV_FIELDS = [
    "record_id",
    "user_telegram_id",
    "source_id",
    "source_home_url",
    "article_url",
    "title",
    "text",
    "published_at",
    "updated_at",
    "author",
    "category",
    "image_url",
    "language",
    "entities",
    "processed_text",
    "discovered_at",
    "telegram_published",
    "telegram_error",
]


def csv_path_for(exports_dir: Path, telegram_id: int) -> Path:
    return exports_dir / f"user_{telegram_id}_articles.csv"


def split_telegram_text(text: str, limit: int = 4096) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


class CsvExporter:
    def __init__(self, exports_dir: Path) -> None:
        self.exports_dir = exports_dir
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        self._locks: dict[int, asyncio.Lock] = {}

    def path_for(self, telegram_id: int) -> Path:
        return csv_path_for(self.exports_dir, telegram_id)

    async def append(self, profile: UserProfile, record: ProcessedRecord) -> Path:
        lock = self._locks.setdefault(profile.telegram_id, asyncio.Lock())
        async with lock:
            path = self.path_for(profile.telegram_id)
            await asyncio.to_thread(self._append_sync, path, profile, record)
            return path

    @staticmethod
    def _append_sync(path: Path, profile: UserProfile, record: ProcessedRecord) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists() or path.stat().st_size == 0
        article = record.article
        row = {
            "record_id": record.id,
            "user_telegram_id": profile.telegram_id,
            "source_id": record.source_id,
            "source_home_url": record.source_home_url,
            "article_url": article.source_url,
            "title": article.title,
            "text": article.text,
            "published_at": article.published_at,
            "updated_at": article.updated_at,
            "author": article.author,
            "category": article.category,
            "image_url": article.image_url,
            "language": article.language,
            "entities": json.dumps(article.entities, ensure_ascii=False),
            "processed_text": record.processed_text,
            "discovered_at": record.discovered_at,
            "telegram_published": record.telegram_published,
            "telegram_error": record.telegram_error,
        }
        with path.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, delimiter=";")
            if write_header:
                writer.writeheader()
            writer.writerow(row)


class TelegramPublisher:
    def __init__(self, control_bot: Bot) -> None:
        self.control_bot = control_bot

    async def publish(self, profile: UserProfile, text: str) -> None:
        output = profile.settings.outputs
        channel_id = self._parse_channel_id(output.telegram_channel_id)
        if channel_id is None:
            raise ValueError("Telegram channel is not configured")

        owns_bot = bool(output.publisher_bot_token)
        publisher = Bot(token=output.publisher_bot_token) if owns_bot else self.control_bot
        try:
            chunks = split_telegram_text(text)
            if not chunks:
                raise ValueError("The final publication text is empty")
            for chunk in chunks:
                await publisher.send_message(chat_id=channel_id, text=chunk)
        finally:
            if owns_bot:
                await publisher.session.close()

    async def test(self, profile: UserProfile) -> None:
        await self.publish(profile, "✅ Test publication completed successfully.")

    @staticmethod
    def _parse_channel_id(value: str | None) -> int | str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if value.lstrip("-").isdigit():
            return int(value)
        if value.startswith("@"):
            return value
        return f"@{value}"
