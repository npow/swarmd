"""Mission schema — mission.yaml contents."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class SuccessCriterion(BaseModel):
    id: str
    description: str
    check: str
    timeout_sec: int = 120
    idempotent: bool = True


class Verification(BaseModel):
    run_every_sec: int = 60
    hold_window_sec: int = 120
    # Extra PATH entries prepended to the verifier's clean PATH. Lets a
    # mission point at e.g. its virtualenv bin, conda env, or local tools
    # without opening the door to arbitrary env inheritance.
    path_add: list[str] = Field(default_factory=list)
    # Specific env vars the verifier passes through from its parent into
    # the clean subprocess. Only listed names are propagated; values come
    # from the parent env at verifier-start time.
    env_passthrough: list[str] = Field(default_factory=list)


class Invariants(BaseModel):
    test_count_floor: int | None = None
    assertion_count_floor: dict[str, int] = Field(default_factory=dict)
    allowed_deps: list[str] = Field(default_factory=list)
    no_mock: list[str] = Field(default_factory=list)


class Concurrency(BaseModel):
    max_total_live: int = 16
    max_depth: int = 8
    max_fan_out_per_parent: int = 4


class PatternThresholds(BaseModel):
    loop_repeat_count: int = 5
    loop_window_events: int = 50
    oscillation_revert_count: int = 2
    oscillation_window_events: int = 30


class ObserverConfig(BaseModel):
    plan_checkpoint_every_sec: int = 300
    goal_drift_cadence_sec: int = 120
    progress_audit_cadence_sec: int = 120
    pattern_thresholds: PatternThresholds = Field(default_factory=PatternThresholds)


class Anticheat(BaseModel):
    primary: str = "claude -p --bare --model opus"
    second_opinion: str | None = None


class Mission(BaseModel):
    mission: str
    workspace: str
    success_criteria: list[SuccessCriterion]
    verification: Verification = Field(default_factory=Verification)
    invariants: Invariants = Field(default_factory=Invariants)
    concurrency: Concurrency = Field(default_factory=Concurrency)
    observer_config: ObserverConfig = Field(default_factory=ObserverConfig)
    anticheat: Anticheat = Field(default_factory=Anticheat)

    @field_validator("workspace")
    @classmethod
    def workspace_must_be_absolute(cls, v: str) -> str:
        if not Path(v).is_absolute():
            raise ValueError(f"workspace must be an absolute path, got: {v}")
        return v

    @field_validator("success_criteria")
    @classmethod
    def must_have_at_least_one_criterion(
        cls, v: list[SuccessCriterion]
    ) -> list[SuccessCriterion]:
        if not v:
            raise ValueError("mission must declare at least one success_criterion")
        ids = [c.id for c in v]
        if len(ids) != len(set(ids)):
            raise ValueError("success_criteria ids must be unique")
        return v
