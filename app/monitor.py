from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from aiogram import Bot

from app.config import Settings
from app.exporters import split_telegram_text
from app.models import PipelineResult, SourceConfig, UserProfile
from app.pipeline import NewsPipeline
from app.storage import JsonStorage

logger = logging.getLogger(__name__)


class MonitorService:
    def __init__(
        self,
        *,
        settings: Settings,
        storage: JsonStorage,
        pipeline: NewsPipeline,
        bot: Bot,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.pipeline = pipeline
        self.bot = bot
        self._stop_event = asyncio.Event()
        self._scheduler_task: asyncio.Task[None] | None = None
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._source_locks: dict[tuple[int, str], asyncio.Lock] = {}

    async def start(self) -> None:
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._stop_event.clear()
        self._scheduler_task = asyncio.create_task(self._scheduler_loop(), name="source-scheduler")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass

    async def run_source(
        self,
        telegram_id: int,
        source_id: str,
        *,
        force: bool = False,
        notify: bool = False,
    ) -> PipelineResult:
        key = (telegram_id, source_id)
        lock = self._source_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return PipelineResult(status="skipped", message="The source is already being processed")

        async with lock, self._semaphore:
            profile = await self.storage.get_user(telegram_id)
            if not profile or not profile.active:
                return PipelineResult(status="skipped", message="The user is disabled")
            source = next((item for item in profile.sources if item.id == source_id), None)
            if not source:
                return PipelineResult(status="skipped", message="The source was not found")
            if not source.enabled and not force:
                return PipelineResult(status="skipped", message="The source is disabled")

            result = await self.pipeline.run(profile, source, force=force)
            if notify or self._should_notify(result):
                await self._notify(profile, source, result)
            return result

    async def run_all_for_user(self, telegram_id: int, force: bool = False) -> None:
        profile = await self.storage.get_user(telegram_id)
        if not profile:
            return
        await asyncio.gather(
            *(
                self.run_source(telegram_id, source.id, force=force, notify=True)
                for source in profile.sources
                if source.enabled
            ),
            return_exceptions=True,
        )

    async def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                users = await self.storage.list_users(active_only=True)
                tasks = []
                now = datetime.now(UTC)
                for profile in users:
                    if profile.settings.paused:
                        continue
                    for source in profile.sources:
                        if source.enabled and self._is_due(profile, source, now):
                            tasks.append(
                                asyncio.create_task(
                                    self.run_source(profile.telegram_id, source.id),
                                    name=f"monitor-{profile.telegram_id}-{source.id}",
                                )
                            )
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler loop error")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.settings.scheduler_tick_seconds,
                )
            except TimeoutError:
                pass

    @staticmethod
    def _parse_datetime(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None

    def _is_due(self, profile: UserProfile, source: SourceConfig, now: datetime) -> bool:
        last_checked = self._parse_datetime(source.last_checked_at)
        if last_checked is None:
            return True
        interval = source.interval_seconds or profile.settings.interval_seconds
        if source.consecutive_errors:
            error_backoff = min(30 * (2 ** min(source.consecutive_errors - 1, 6)), 3600)
            interval = max(interval, error_backoff)
        return (now - last_checked).total_seconds() >= interval

    def _should_notify(self, result: PipelineResult) -> bool:
        if result.status == "processed":
            return self.settings.notify_success
        if result.status == "error":
            return self.settings.notify_errors
        return False

    async def _notify(
        self,
        profile: UserProfile,
        source: SourceConfig,
        result: PipelineResult,
    ) -> None:
        source_name = source.name or source.url
        icon = {
            "processed": "✅",
            "error": "❌",
            "baseline": "🧭",
            "unchanged": "ℹ️",
            "skipped": "⏭",
        }.get(result.status, "ℹ️")
        lines = [f"{icon} {source_name}", result.message]
        if result.article_url:
            lines.append(result.article_url)
        if result.record:
            outputs = []
            if result.record.csv_saved:
                outputs.append("CSV saved")
            if result.record.telegram_published:
                outputs.append("published to channel")
            if result.record.csv_error:
                outputs.append(f"CSV error: {result.record.csv_error}")
            if result.record.telegram_error:
                outputs.append(f"channel error: {result.record.telegram_error}")
            if outputs:
                lines.append("; ".join(outputs))
        try:
            for chunk in split_telegram_text("\n".join(lines)):
                await self.bot.send_message(profile.telegram_id, chunk)
        except Exception:
            logger.exception("Could not send a notification to user %s", profile.telegram_id)
