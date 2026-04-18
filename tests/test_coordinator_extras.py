"""Tests for coordinator's re-issue loop and plan-checkpoint scheduling."""

from __future__ import annotations

import time

from swarm.lib.interventions import ack as ack_intervention
from swarm.lib.interventions import read_all
from swarm.lib.locking import write_line
from swarm.lib.paths import interventions_path
from swarm.schemas.intervention import Intervention
from swarm.specialists.coordinator import (
    REISSUE_AFTER_SEC,
    _maybe_emit_checkpoint,
    _reissue_stale,
)


def _seed_intervention(session_id: str, age_sec: float, acked: bool = False) -> str:
    iid = f"i-{int((time.time() - age_sec) * 1000)}-abc"
    iv = Intervention(id=iid, tier="correct", reason="redo")
    write_line(interventions_path(session_id), iv.model_dump_json())
    if acked:
        ack_intervention(session_id, iid, "stop_blocked")
    return iid


def test_reissue_stale_unacked(session_id, monkeypatch):
    # Seed an old, unacked intervention
    iid = _seed_intervention(session_id, age_sec=REISSUE_AFTER_SEC + 60)
    reissued: set[str] = set()
    n = _reissue_stale(session_id, reissued)
    assert n == 1
    assert iid in reissued
    # The new entry should be present in interventions.jsonl with a fresh id
    items = read_all(session_id)
    assert len(items) == 2
    assert any("REISSUE" in iv.reason for iv in items)


def test_reissue_does_not_double_issue(session_id):
    _seed_intervention(session_id, age_sec=REISSUE_AFTER_SEC + 60)
    reissued: set[str] = set()
    _reissue_stale(session_id, reissued)
    # Second call should NOT re-issue again
    n2 = _reissue_stale(session_id, reissued)
    assert n2 == 0


def test_reissue_skips_acked(session_id):
    _seed_intervention(session_id, age_sec=REISSUE_AFTER_SEC + 60, acked=True)
    reissued: set[str] = set()
    n = _reissue_stale(session_id, reissued)
    assert n == 0


def test_reissue_skips_fresh(session_id):
    _seed_intervention(session_id, age_sec=10)  # well below threshold
    reissued: set[str] = set()
    assert _reissue_stale(session_id, reissued) == 0


def test_plan_checkpoint_emits_after_cadence(session_id, sample_mission):
    sample_mission.observer_config.plan_checkpoint_every_sec = 1
    last_ts = time.time() - 5  # well past cadence
    emitted, new_ts = _maybe_emit_checkpoint(session_id, sample_mission, last_ts)
    assert emitted is True
    assert new_ts > last_ts
    # Verify the intervention was written and tagged correctly
    items = read_all(session_id)
    assert any(iv.strategy_used == "plan_checkpoint" for iv in items)
    cp = next(iv for iv in items if iv.strategy_used == "plan_checkpoint")
    assert cp.tier == "info"
    assert "current sub-goal" in cp.reason


def test_plan_checkpoint_not_emit_too_soon(session_id, sample_mission):
    sample_mission.observer_config.plan_checkpoint_every_sec = 1000
    last_ts = time.time() - 5
    emitted, _ = _maybe_emit_checkpoint(session_id, sample_mission, last_ts)
    assert emitted is False
    assert read_all(session_id) == []


def test_plan_checkpoint_no_mission(session_id):
    emitted, _ = _maybe_emit_checkpoint(session_id, None, time.time() - 1000)
    assert emitted is False
