"""Intervention schema — directive from coordinator to worker."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Tier = Literal[
    "info", "correct", "urgent", "recover", "mission_complete", "mission_level_alert"
]
ConsumeAt = Literal["stop", "post_tool", "either"]


class Intervention(BaseModel):
    id: str
    tier: Tier
    reason: str
    consume_at: ConsumeAt = "stop"
    requires_ack: bool = True
    referenced_findings: list[str] = Field(default_factory=list)
    strategy_used: str | None = None
    loop_signature: str | None = None
    schema_version: int = 1
