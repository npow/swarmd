"""Durable MissionState — the only state carried across ``continue_as_new``.

Per spec §6.2 (carry-state) and §8 (state management). Everything in
``MissionState`` is the irreducible minimum the parent ``MissionWorkflow``
must persist across restarts; every other derivable quantity can be
reconstructed from Temporal history or from the on-disk ``events.jsonl`` /
``findings.jsonl`` mirrors.

``MissionState`` is a ``pydantic.BaseModel`` so Temporal's JSON converter
can serialize it without any custom dataconverter. All fields have
defaults so ``MissionState.empty()`` works as the first-launch sentinel.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# Phase machine per spec §6.2. Kept as a Literal so the workflow code can
# pattern-match exhaustively and pydantic rejects typos at validation time.
Phase = Literal[
    "launching",
    "running",
    "passing",
    "hold_window",
    "complete",
    "aborting",
    "aborted",
    "failed_terminal",
]


class CriterionState(BaseModel):
    """Live state of a single success criterion during a mission.

    ``pass_`` (Python name) <-> ``pass`` (serialized / wire name). ``pass`` is
    a Python reserved word so the field uses an alias.
    """

    # `pass` is a Python reserved keyword, so the attribute is `pass_` and the
    # alias is the JSON field name `pass`. `populate_by_name=True` lets callers
    # use either form when constructing from dicts.
    pass_: bool = Field(False, alias="pass")
    last_check_ts: datetime | None = None
    streak_sec: float = 0.0
    exit_code: int | None = None
    stderr_tail: str = ""
    # Debounce counter: how many consecutive check failures have occurred
    # while the criterion was in a passing state. A single transient failure
    # (e.g., agent launched a new run that hasn't completed yet) doesn't flip
    # pass→fail; only `fail_debounce` consecutive failures do. Reset to 0
    # whenever the check passes.
    consecutive_fails: int = 0

    model_config = {"populate_by_name": True}


class SpawnTree(BaseModel):
    """Subagent admission-control state per spec §6.4 row 9.

    ``live_count`` is the total number of currently-spawned subagents across
    the whole mission; ``per_parent_fan_out`` tracks how many children each
    parent has spawned so we can enforce per-parent fan-out caps without
    rescanning Temporal history.
    """

    live_count: int = 0
    per_parent_fan_out: dict[str, int] = Field(default_factory=dict)


class MissionState(BaseModel):
    """Durable state carried across ``MissionWorkflow.continue_as_new`` calls.

    This is the ONLY state the workflow must persist. Everything else can be
    derived from Temporal history.
    """

    phase: Phase = "launching"
    criteria_state: dict[str, CriterionState] = Field(default_factory=dict)
    hold_window_start: datetime | None = None
    findings_count: int = 0
    abort_reason: str | None = None
    # Child workflow IDs for the observer specialists spawned by the parent.
    # Keys: {"pattern_detector", "llm_critic", "resource_monitor"}.
    child_workflow_ids: dict[str, str] = Field(default_factory=dict)
    # Escape-ladder state per spec §6.4 row 5 — once a dimension (e.g. "loop"
    # or "drift") accumulates enough strikes the workflow escalates.
    strikes_by_dimension: dict[str, int] = Field(default_factory=dict)
    # Strategies already attempted on the current struck dimension, so we
    # don't cycle through the same remediation twice.
    tried_strategies: list[str] = Field(default_factory=list)
    # Subagent admission-control counters per spec §6.4 row 9.
    spawn_tree: SpawnTree = Field(default_factory=SpawnTree)
    # Progress monotonicity — detect death spirals where the agent is stuck
    # or regressing. max_passing is the high-water mark of simultaneously
    # passing criteria; stall_cycles counts how many verifier cycles have
    # elapsed without exceeding it. When stall_cycles >= threshold, the
    # workflow emits a "progress_stalled" finding and can escalate.
    max_passing: int = 0
    stall_cycles: int = 0
    # Interventions that were issued but not yet acknowledged. The workflow
    # re-issues them after the ack timeout (120s per spec).
    pending_interventions: list[dict] = Field(default_factory=list)
    # Test-only: when set by the ``force_continue_as_new`` signal, the
    # verifier loop triggers ``continue_as_new`` on its next iteration
    # without waiting for Temporal to hit the 50k-event / 50MB threshold
    # that normally drives ``is_continue_as_new_suggested``. Production
    # missions will never set this. Kept on the state (not an instance
    # attr) so it also survives the very continue_as_new it triggers —
    # but the trigger site clears it before calling ``continue_as_new``,
    # so the resumed incarnation sees ``force_continue_as_new=False``
    # which prevents an infinite cas loop.
    force_continue_as_new: bool = False

    @classmethod
    def empty(cls) -> "MissionState":
        """Sentinel for first-launch MissionWorkflow.run() — all defaults."""
        return cls()
