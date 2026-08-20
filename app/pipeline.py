from __future__ import annotations

import logging
from urllib.parse import urljoin, urlsplit

from app.config import Settings
from app.exporters import CsvExporter, TelegramPublisher
from app.fetcher import FetchError, SafeHtmlFetcher, validate_public_url
from app.html_cleaner import compact_article_page, compact_discovery_page
from app.models import PipelineResult, ProcessedRecord, SourceConfig, UserProfile
from app.ollama_client import OllamaClient, OllamaError
from app.storage import JsonStorage

logger = logging.getLogger(__name__)


class NewsPipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        storage: JsonStorage,
        fetcher: SafeHtmlFetcher,
        ollama: OllamaClient,
        csv_exporter: CsvExporter,
        telegram_publisher: TelegramPublisher,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.fetcher = fetcher
        self.ollama = ollama
        self.csv_exporter = csv_exporter
        self.telegram_publisher = telegram_publisher

    async def run(
        self, profile: UserProfile, source: SourceConfig, force: bool = False
    ) -> PipelineResult:
        try:
            source_page = await self.fetcher.fetch(source.url)
            discovery_page = compact_discovery_page(
                source_page.html,
                source_page.final_url,
                self.settings.max_discovery_chars,
            )
            if discovery_page.anchors_count == 0:
                raise FetchError("No links remained after the page was cleaned")

            candidate = await self.ollama.discover_latest_url(
                profile.settings.prompts.discovery,
                discovery_page.content,
                source_page.final_url,
            )
            article_url = await self._normalize_article_url(candidate, source_page.final_url)

            if article_url == source.last_seen_url and not force:
                await self.storage.update_source_unchanged(profile.telegram_id, source.id)
                return PipelineResult(
                    status="unchanged",
                    message="No new items were found",
                    article_url=article_url,
                )

            already_processed = await self.storage.has_processed_url(
                profile.telegram_id, article_url
            )
            if already_processed and not force:
                await self.storage.update_source_success(
                    profile.telegram_id,
                    source.id,
                    latest_url=article_url,
                    processed=True,
                )
                return PipelineResult(
                    status="unchanged",
                    message="This item has already been processed",
                    article_url=article_url,
                )

            is_first_observation = source.last_seen_url is None
            if is_first_observation and not profile.settings.process_first_item and not force:
                await self.storage.update_source_success(
                    profile.telegram_id,
                    source.id,
                    latest_url=article_url,
                    processed=False,
                )
                return PipelineResult(
                    status="baseline",
                    message="The initial source state has been saved",
                    article_url=article_url,
                )

            article_page = await self.fetcher.fetch(article_url)
            compact_article = compact_article_page(
                article_page.html,
                article_page.final_url,
                self.settings.max_article_chars,
            )
            article = await self.ollama.extract_article(
                profile.settings.prompts.extraction,
                compact_article.content,
                article_page.final_url,
            )
            processed_text = await self.ollama.process_article(
                profile.settings.prompts.processing,
                article,
            )
            record = ProcessedRecord(
                source_id=source.id,
                source_home_url=source.url,
                article=article,
                processed_text=processed_text,
            )

            if profile.settings.outputs.telegram_enabled:
                try:
                    await self.telegram_publisher.publish(profile, processed_text)
                    record.telegram_published = True
                except Exception as exc:
                    logger.exception("Could not publish content for user %s", profile.telegram_id)
                    record.telegram_error = str(exc)[:1000]

            if profile.settings.outputs.csv_enabled:
                try:
                    await self.csv_exporter.append(profile, record)
                    record.csv_saved = True
                except Exception as exc:
                    logger.exception(
                        "Could not write the CSV file for user %s", profile.telegram_id
                    )
                    record.csv_error = str(exc)[:1000]

            await self.storage.add_processed_record(profile.telegram_id, record)
            await self.storage.update_source_success(
                profile.telegram_id,
                source.id,
                latest_url=article_url,
                processed=True,
            )
            return PipelineResult(
                status="processed",
                message="A new item has been processed",
                article_url=article_url,
                record=record,
            )
        except (FetchError, OllamaError, ValueError) as exc:
            message = str(exc)
            await self.storage.update_source_error(profile.telegram_id, source.id, message)
            return PipelineResult(status="error", message=message)
        except Exception as exc:
            logger.exception("Unexpected pipeline error")
            message = f"Unexpected error: {exc}"
            await self.storage.update_source_error(profile.telegram_id, source.id, message)
            return PipelineResult(status="error", message=message)

    async def _normalize_article_url(self, candidate: str, source_page_url: str) -> str:
        candidate = candidate.strip().strip("`<>").strip()
        absolute = urljoin(source_page_url, candidate)
        absolute = await validate_public_url(absolute)
        if not self.settings.allow_external_article_urls:
            source_host = (urlsplit(source_page_url).hostname or "").lower()
            article_host = (urlsplit(absolute).hostname or "").lower()
            if not self._same_site(source_host, article_host):
                raise ValueError(
                    f"The model selected external domain {article_host}; expected {source_host}"
                )
        return absolute

    @staticmethod
    def _same_site(first: str, second: str) -> bool:
        return first == second or first.endswith(f".{second}") or second.endswith(f".{first}")
