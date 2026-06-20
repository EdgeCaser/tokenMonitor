"""Typed records that flow from JSONL → ingest → DuckDB."""

from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel, Field


class ToolCallRecord(BaseModel):
    idx: int
    name: str
    input_chars: int
    input_preview: str


class TurnRecord(BaseModel):
    uuid: str
    parent_uuid: str | None = None
    request_id: str | None = None
    session_id: str
    project_path: str
    project_label: str
    git_branch: str | None = None
    model: str
    ts: datetime
    is_sidechain: bool = False

    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0

    service_tier: str | None = None
    stop_reason: str | None = None

    has_thinking: bool = False
    thinking_chars: int = 0
    text_chars: int = 0

    web_search_requests: int = 0
    web_fetch_requests: int = 0

    raw_usage: str = "{}"
    source_file: str
    source_offset: int
    host: str = "local"

    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class UserTurnRecord(BaseModel):
    uuid: str
    session_id: str
    project_path: str
    ts: datetime
    source_file: str
