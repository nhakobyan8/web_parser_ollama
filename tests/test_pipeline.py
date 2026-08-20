import asyncio
from types import SimpleNamespace

from app.exporters import CsvExporter
from app.fetcher import FetchResult
from app.models import ArticleData
from app.pipeline import NewsPipeline
from app.storage import JsonStorage


class FakeFetcher:
    async def fetch(self, url: str) -> FetchResult:
        if url.endswith("/news"):
            html = '<main><article><a href="/post/1">Latest news</a></article></main>'
        else:
            html = "<article><h1>Headline</h1><p>Complete article text for testing.</p></article>"
        return FetchResult(url, url, html, 200, "fake")


class FakeOllama:
    async def discover_latest_url(self, prompt: str, compact_page: str, base_url: str) -> str:
        return "https://93.184.216.34/post/1"

    async def extract_article(self, prompt: str, compact_page: str, source_url: str) -> ArticleData:
        return ArticleData(
            title="Headline",
            text="Complete article text for testing.",
            source_url=source_url,
            language="ru",
        )

    async def process_article(self, prompt: str, article: ArticleData) -> str:
        return f"Finished content: {article.title}"


class FakePublisher:
    async def publish(self, profile, text: str) -> None:
        raise AssertionError("Publication is disabled in the profile")


def test_pipeline_processes_article_and_persists_state(tmp_path):
    async def scenario():
        storage = JsonStorage(tmp_path / "users.json", default_interval_seconds=300)
        await storage.initialize(frozenset({100}))
        await storage.set_user_field(100, "process_first_item", True)
        source = await storage.add_source(100, "https://93.184.216.34/news")
        profile = await storage.get_user(100)
        assert profile is not None

        settings = SimpleNamespace(
            max_discovery_chars=20_000,
            max_article_chars=20_000,
            allow_external_article_urls=False,
        )
        pipeline = NewsPipeline(
            settings=settings,
            storage=storage,
            fetcher=FakeFetcher(),
            ollama=FakeOllama(),
            csv_exporter=CsvExporter(tmp_path / "exports"),
            telegram_publisher=FakePublisher(),
        )
        result = await pipeline.run(profile, source)

        assert result.status == "processed"
        assert result.record is not None
        assert result.record.csv_saved is True
        assert (tmp_path / "exports" / "user_100_articles.csv").exists()

        restored = await storage.get_user(100)
        assert restored is not None
        assert restored.sources[0].last_seen_url == "https://93.184.216.34/post/1"
        assert len(restored.processed) == 1

    asyncio.run(scenario())
