"""Finding schema — structured report emitted by a specialist."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FindingType = Literal[
    "cheat", "drift", "loop", "thrash", "fabrication", "verification", "meta"
]
Severity = Literal["critical", "major", "minor"]
JudgeStatus = Literal["pending", "completed", "timed_out"]


class Evidence(BaseModel):
    files: list[str] = Field(default_factory=list)
    diff_snippet: str | None = None
    tool_calls: list[str] = Field(default_factory=list)
    claim_excerpt: str | None = None


class Finding(BaseModel):
    id: str
    source: str = Field(description="e.g. pattern_detector.loop")
    subject_session: str
    spawner_id: str
    type: FindingType
    subtype: str
    severity: Severity
    cited_events: list[str] = Field(default_factory=list)
    evidence: Evidence = Field(default_factory=Evidence)
    verdict: str | None = None
    judge_status: JudgeStatus = "pending"
    schema_version: int = 1
