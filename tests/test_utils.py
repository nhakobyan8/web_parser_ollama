from app.bot_ui import parse_interval
from app.exporters import split_telegram_text
from app.fetcher import FetchError, normalize_http_url


def test_interval_parser():
    assert parse_interval("30s") == 30
    assert parse_interval("5m") == 300
    assert parse_interval("1h") == 3600


def test_telegram_split_respects_limit():
    chunks = split_telegram_text(("A paragraph of text. " * 1000).strip(), limit=500)
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 500 for chunk in chunks)


def test_url_normalizer_rejects_credentials():
    try:
        normalize_http_url("https://user:pass@example.com/news")
    except FetchError:
        pass
    else:
        raise AssertionError("A URL containing credentials must be rejected")
