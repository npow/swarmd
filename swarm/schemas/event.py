"""Event schema — one row per tool invocation in the transcript."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

HookName = Literal["SessionStart", "PostToolUse", "Stop", "PreCompact"]


class Event(BaseModel):
    id: str = Field(description="Monotonic event id, e.g. e-<n>-<short>")
    session_id: str
    spawner_id: str
    parent_id: str | None = None
    depth: int = 0
    ts_monotonic: float
    ts_wall: str
    hook: HookName
    tool_name: str | None = None
    tool_input_summary: str | None = None
    tool_response_summary: str | None = None
    detail_ref: str | None = None
    schema_version: int = 1
