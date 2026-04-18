"""Tests for MissionState (durable carry-state for continue_as_new).

Per plan Task 3 and spec §6.2/§8.

The MissionState is the ONLY state the MissionWorkflow must persist across
continue_as_new calls. Everything else is derivable from Temporal history.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from swarm.durable.state import CriterionState, MissionState, SpawnTree


def test_mission_state_roundtrip() -> None:
    """Full-shape MissionState must serialize/deserialize losslessly."""
    s = MissionState(
        phase="running",
        criteria_state={
            "c1": CriterionState(
                pass_=True,
                last_check_ts=datetime(2026, 4, 18, tzinfo=timezone.utc),
                streak_sec=30,
            ),
        },
        hold_window_start=None,
        findings_count=0,
        abort_reason=None,
        child_workflow_ids={},
        strikes_by_dimension={},
        tried_strategies=[],
        spawn_tree=SpawnTree(live_count=0, per_parent_fan_out={}),
        pending_interventions=[],
    )
    d = s.model_dump()
    s2 = MissionState.model_validate(d)
    assert s2 == s


def test_empty_carry_is_first_launch() -> None:
    """continue_as_new callers use empty MissionState() as sentinel for first launch."""
    s = MissionState.empty()
    assert s.phase == "launching"
    assert s.criteria_state == {}
    assert s.child_workflow_ids == {}
    assert s.findings_count == 0
    assert s.abort_reason is None
    assert s.strikes_by_dimension == {}
    assert s.tried_strategies == []
    assert s.spawn_tree.live_count == 0
    assert s.spawn_tree.per_parent_fan_out == {}
    assert s.pending_interventions == []


def test_criterion_state_pass_alias() -> None:
    """`pass_` is the Python attribute; "pass" is the serialized key."""
    c = CriterionState(pass_=True)
    d = c.model_dump(by_alias=True)
    assert d["pass"] is True
    # round-trip via alias (what the workflow history will actually contain)
    c2 = CriterionState.model_validate({"pass": True, "streak_sec": 5.0})
    assert c2.pass_ is True
    assert c2.streak_sec == 5.0


def test_phase_literal_enforced() -> None:
    """Bogus phase values must be rejected by the Literal validator."""
    with pytest.raises(ValidationError):
        MissionState(phase="bogus")  # type: ignore[arg-type]


def test_spawn_tree_roundtrip() -> None:
    """SpawnTree carries per-parent fan-out counters for admission control."""
    tree = SpawnTree(
        live_count=3,
        per_parent_fan_out={"parent-a": 2, "parent-b": 1},
    )
    d = tree.model_dump()
    tree2 = SpawnTree.model_validate(d)
    assert tree2 == tree


def test_criterion_state_defaults() -> None:
    """Default CriterionState is not-yet-checked: pass_=False, no streak, no ts."""
    c = CriterionState()
    assert c.pass_ is False
    assert c.streak_sec == 0.0
    assert c.last_check_ts is None
    assert c.exit_code is None
    assert c.stderr_tail == ""
