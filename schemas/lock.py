"""Lock schema — mission.lock.json contents."""

from __future__ import annotations

from pydantic import BaseModel, Field


class Baseline(BaseModel):
    test_count: int = 0
    assertion_counts: dict[str, int] = Field(default_factory=dict)


class MissionLock(BaseModel):
    session_id: str
    locked_at: str
    files: dict[str, str] = Field(
        default_factory=dict, description="path -> sha256:<hex>"
    )
    baseline: Baseline = Field(default_factory=Baseline)
