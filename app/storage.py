from __future__ import annotations

import asyncio
import os
from pathlib import Path

from app.models import (
    Database,
    ProcessedRecord,
    SourceConfig,
    UserProfile,
    utc_now_iso,
)


class JsonStorage:
    def __init__(self, path: Path, default_interval_seconds: int, max_records: int = 300) -> None:
        self.path = path
        self.default_interval_seconds = default_interval_seconds
        self.max_records = max_records
        self._lock = asyncio.Lock()
        self._db = Database()

    async def initialize(self, admin_ids: frozenset[int]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            if self.path.exists():
                raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
                self._db = Database.model_validate_json(raw)

            changed = False
            for admin_id in admin_ids:
                key = str(admin_id)
                if key not in self._db.users:
                    profile = UserProfile(telegram_id=admin_id, role="admin")
                    profile.settings.interval_seconds = self.default_interval_seconds
                    self._db.users[key] = profile
                    changed = True
                else:
                    user = self._db.users[key]
                    if user.role != "admin" or not user.active:
                        user.role = "admin"
                        user.active = True
                        changed = True

            if changed or not self.path.exists():
                await self._save_unlocked()

    async def _save_unlocked(self) -> None:
        self._db.updated_at = utc_now_iso()
        payload = self._db.model_dump_json(indent=2)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")

        def write_atomic() -> None:
            with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)

        await asyncio.to_thread(write_atomic)

    async def get_user(self, telegram_id: int) -> UserProfile | None:
        async with self._lock:
            user = self._db.users.get(str(telegram_id))
            return user.model_copy(deep=True) if user else None

    async def list_users(self, active_only: bool = False) -> list[UserProfile]:
        async with self._lock:
            users = list(self._db.users.values())
            if active_only:
                users = [user for user in users if user.active]
            users.sort(key=lambda user: (user.role != "admin", user.telegram_id))
            return [user.model_copy(deep=True) for user in users]

    async def add_user(
        self,
        telegram_id: int,
        added_by: int,
        display_name: str | None = None,
        role: str = "user",
    ) -> tuple[UserProfile, bool]:
        async with self._lock:
            key = str(telegram_id)
            created = key not in self._db.users
            if created:
                profile = UserProfile(
                    telegram_id=telegram_id,
                    display_name=display_name,
                    role="admin" if role == "admin" else "user",
                    added_by=added_by,
                )
                profile.settings.interval_seconds = self.default_interval_seconds
                self._db.users[key] = profile
            else:
                profile = self._db.users[key]
                profile.active = True
                profile.settings.paused = False
                if display_name:
                    profile.display_name = display_name
            await self._save_unlocked()
            return profile.model_copy(deep=True), created

    async def deactivate_user(self, telegram_id: int) -> bool:
        async with self._lock:
            profile = self._db.users.get(str(telegram_id))
            if not profile or profile.role == "admin":
                return False
            profile.active = False
            profile.settings.paused = True
            await self._save_unlocked()
            return True

    async def add_source(self, telegram_id: int, url: str, name: str | None = None) -> SourceConfig:
        async with self._lock:
            profile = self._require_user_unlocked(telegram_id)
            normalized = url.rstrip("/")
            for existing in profile.sources:
                if existing.url.rstrip("/") == normalized:
                    raise ValueError("This source has already been added")
            source = SourceConfig(url=url, name=name)
            profile.sources.append(source)
            await self._save_unlocked()
            return source.model_copy(deep=True)

    async def remove_source(self, telegram_id: int, source_id: str) -> bool:
        async with self._lock:
            profile = self._require_user_unlocked(telegram_id)
            before = len(profile.sources)
            profile.sources = [source for source in profile.sources if source.id != source_id]
            changed = len(profile.sources) != before
            if changed:
                await self._save_unlocked()
            return changed

    async def toggle_source(self, telegram_id: int, source_id: str) -> SourceConfig | None:
        async with self._lock:
            source = self._find_source_unlocked(telegram_id, source_id)
            if not source:
                return None
            source.enabled = not source.enabled
            await self._save_unlocked()
            return source.model_copy(deep=True)

    async def set_user_field(self, telegram_id: int, field: str, value: object) -> UserProfile:
        allowed = {"interval_seconds", "paused", "process_first_item"}
        if field not in allowed:
            raise ValueError(f"Unsupported settings field: {field}")
        async with self._lock:
            profile = self._require_user_unlocked(telegram_id)
            setattr(profile.settings, field, value)
            await self._save_unlocked()
            return profile.model_copy(deep=True)

    async def set_prompt(self, telegram_id: int, prompt_type: str, value: str) -> None:
        if prompt_type not in {"discovery", "extraction", "processing"}:
            raise ValueError("Unknown prompt type")
        async with self._lock:
            profile = self._require_user_unlocked(telegram_id)
            setattr(profile.settings.prompts, prompt_type, value.strip())
            await self._save_unlocked()

    async def set_output_field(self, telegram_id: int, field: str, value: object) -> None:
        allowed = {
            "csv_enabled",
            "telegram_enabled",
            "telegram_channel_id",
            "publisher_bot_token",
        }
        if field not in allowed:
            raise ValueError(f"Unsupported output field: {field}")
        async with self._lock:
            profile = self._require_user_unlocked(telegram_id)
            setattr(profile.settings.outputs, field, value)
            await self._save_unlocked()

    async def update_source_success(
        self,
        telegram_id: int,
        source_id: str,
        *,
        latest_url: str,
        processed: bool,
    ) -> None:
        async with self._lock:
            source = self._find_source_unlocked(telegram_id, source_id)
            if not source:
                return
            now = utc_now_iso()
            source.last_checked_at = now
            source.last_success_at = now
            source.last_error = None
            source.consecutive_errors = 0
            if processed or source.last_seen_url is None:
                source.last_seen_url = latest_url
            await self._save_unlocked()

    async def update_source_unchanged(self, telegram_id: int, source_id: str) -> None:
        async with self._lock:
            source = self._find_source_unlocked(telegram_id, source_id)
            if not source:
                return
            now = utc_now_iso()
            source.last_checked_at = now
            source.last_success_at = now
            source.last_error = None
            source.consecutive_errors = 0
            await self._save_unlocked()

    async def update_source_error(self, telegram_id: int, source_id: str, error: str) -> None:
        async with self._lock:
            source = self._find_source_unlocked(telegram_id, source_id)
            if not source:
                return
            source.last_checked_at = utc_now_iso()
            source.last_error = error[:1000]
            source.consecutive_errors += 1
            await self._save_unlocked()

    async def has_processed_url(self, telegram_id: int, url: str) -> bool:
        async with self._lock:
            profile = self._db.users.get(str(telegram_id))
            if not profile:
                return False
            return any(record.article.source_url == url for record in profile.processed)

    async def add_processed_record(self, telegram_id: int, record: ProcessedRecord) -> None:
        async with self._lock:
            profile = self._require_user_unlocked(telegram_id)
            profile.processed.append(record)
            if len(profile.processed) > self.max_records:
                profile.processed = profile.processed[-self.max_records :]
            await self._save_unlocked()

    def _require_user_unlocked(self, telegram_id: int) -> UserProfile:
        profile = self._db.users.get(str(telegram_id))
        if not profile:
            raise KeyError(f"User {telegram_id} was not found")
        return profile

    def _find_source_unlocked(self, telegram_id: int, source_id: str) -> SourceConfig | None:
        profile = self._db.users.get(str(telegram_id))
        if not profile:
            return None
        return next((source for source in profile.sources if source.id == source_id), None)
