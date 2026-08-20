from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value and value.strip() else default


def _env_ids(name: str) -> frozenset[int]:
    raw = os.getenv(name, "")
    result: set[int] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if item:
            result.add(int(item))
    return frozenset(result)


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    admin_ids: frozenset[int]
    data_dir: Path
    database_path: Path
    exports_dir: Path
    ollama_url: str
    ollama_model: str
    ollama_timeout_seconds: int
    ollama_num_ctx: int
    ollama_keep_alive: str
    request_timeout_seconds: int
    request_attempts: int
    max_html_bytes: int
    max_discovery_chars: int
    max_article_chars: int
    min_interval_seconds: int
    default_interval_seconds: int
    scheduler_tick_seconds: int
    max_concurrent_jobs: int
    allow_external_article_urls: bool
    notify_success: bool
    notify_errors: bool
    timezone: str

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")

        admin_ids = _env_ids("ADMIN_IDS")
        if not admin_ids:
            raise RuntimeError("ADMIN_IDS must contain at least one Telegram ID")

        data_dir = Path(os.getenv("DATA_DIR", "/app/data")).expanduser().resolve()
        return cls(
            telegram_bot_token=token,
            admin_ids=admin_ids,
            data_dir=data_dir,
            database_path=data_dir / "users.json",
            exports_dir=data_dir / "exports",
            ollama_url=os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen3:8b").strip(),
            ollama_timeout_seconds=_env_int("OLLAMA_TIMEOUT_SECONDS", 300),
            ollama_num_ctx=_env_int("OLLAMA_NUM_CTX", 32768),
            ollama_keep_alive=os.getenv("OLLAMA_KEEP_ALIVE", "15m"),
            request_timeout_seconds=_env_int("REQUEST_TIMEOUT_SECONDS", 40),
            request_attempts=_env_int("REQUEST_ATTEMPTS", 3),
            max_html_bytes=_env_int("MAX_HTML_BYTES", 8_000_000),
            max_discovery_chars=_env_int("MAX_DISCOVERY_CHARS", 45_000),
            max_article_chars=_env_int("MAX_ARTICLE_CHARS", 90_000),
            min_interval_seconds=_env_int("MIN_INTERVAL_SECONDS", 30),
            default_interval_seconds=_env_int("DEFAULT_INTERVAL_SECONDS", 300),
            scheduler_tick_seconds=_env_int("SCHEDULER_TICK_SECONDS", 5),
            max_concurrent_jobs=_env_int("MAX_CONCURRENT_JOBS", 2),
            allow_external_article_urls=_env_bool("ALLOW_EXTERNAL_ARTICLE_URLS", False),
            notify_success=_env_bool("NOTIFY_SUCCESS", True),
            notify_errors=_env_bool("NOTIFY_ERRORS", True),
            timezone=os.getenv("TZ", "Asia/Yerevan"),
        )
