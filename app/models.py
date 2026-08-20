from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.defaults import (
    DEFAULT_DISCOVERY_PROMPT,
    DEFAULT_EXTRACTION_PROMPT,
    DEFAULT_PROCESSING_PROMPT,
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid4().hex[:12]


class AppModel(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class ArticleData(AppModel):
    title: str | None = None
    text: str
    published_at: str | None = None
    updated_at: str | None = None
    author: str | None = None
    category: str | None = None
    image_url: str | None = None
    source_url: str
    language: str | None = None
    entities: list[str] = Field(default_factory=list)

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("The model returned an empty article text")
        return cleaned


class SourceConfig(AppModel):
    id: str = Field(default_factory=new_id)
    url: str
    name: str | None = None
    enabled: bool = True
    interval_seconds: int | None = None
    last_seen_url: str | None = None
    last_checked_at: str | None = None
    last_success_at: str | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    created_at: str = Field(default_factory=utc_now_iso)

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip()
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("The source must be a complete HTTP/HTTPS URL")
        return value


class PromptSettings(AppModel):
    discovery: str = DEFAULT_DISCOVERY_PROMPT
    extraction: str = DEFAULT_EXTRACTION_PROMPT
    processing: str = DEFAULT_PROCESSING_PROMPT


class OutputSettings(AppModel):
    csv_enabled: bool = True
    telegram_enabled: bool = False
    telegram_channel_id: str | None = None
    publisher_bot_token: str | None = None


class UserSettings(AppModel):
    interval_seconds: int = 300
    paused: bool = False
    process_first_item: bool = False
    prompts: PromptSettings = Field(default_factory=PromptSettings)
    outputs: OutputSettings = Field(default_factory=OutputSettings)


class ProcessedRecord(AppModel):
    id: str = Field(default_factory=new_id)
    source_id: str
    source_home_url: str
    article: ArticleData
    processed_text: str
    discovered_at: str = Field(default_factory=utc_now_iso)
    csv_saved: bool = False
    csv_error: str | None = None
    telegram_published: bool = False
    telegram_error: str | None = None


class UserProfile(AppModel):
    telegram_id: int
    display_name: str | None = None
    role: Literal["admin", "user"] = "user"
    active: bool = True
    added_by: int | None = None
    created_at: str = Field(default_factory=utc_now_iso)
    settings: UserSettings = Field(default_factory=UserSettings)
    sources: list[SourceConfig] = Field(default_factory=list)
    processed: list[ProcessedRecord] = Field(default_factory=list)


class Database(AppModel):
    schema_version: int = 1
    updated_at: str = Field(default_factory=utc_now_iso)
    users: dict[str, UserProfile] = Field(default_factory=dict)


class DiscoveryResult(AppModel):
    url: str


class PipelineResult(AppModel):
    status: Literal["unchanged", "baseline", "processed", "error", "skipped"]
    message: str
    article_url: str | None = None
    record: ProcessedRecord | None = None
    details: dict[str, Any] = Field(default_factory=dict)
