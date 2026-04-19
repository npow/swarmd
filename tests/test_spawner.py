"""Tests for spawner admission control + tree tracking."""

from __future__ import annotations

from swarmd.schemas.mission import Concurrency
from swarmd.specialists.spawner import (
    AdmissionResult,
    SpawnerState,
    SpawnRequest,
    admit_spawn,
    depth_exceeded_finding,
    drain_queue_one,
    enqueue,
    load_tree,
    mark_dead,
    register_spawn,
    save_tree,
)


def _concurrency(**kw) -> Concurrency:
    defaults = dict(max_total_live=4, max_depth=3, max_fan_out_per_parent=2)
    defaults.update(kw)
    return Concurrency(**defaults)


def _req(parent: str = "root", depth: int = 1) -> SpawnRequest:
    return SpawnRequest(
        parent_id=parent, depth=depth, mission="test", context_summary=""
    )


# --- admission ---


def test_admit_when_under_budget():
    state = SpawnerState()
    assert admit_spawn(state, _req(), _concurrency()) == AdmissionResult.ADMIT


def test_reject_depth_exceeded():
    state = SpawnerState()
    req = _req(depth=10)
    assert admit_spawn(state, req, _concurrency(max_depth=3)) == AdmissionResult.REJECT


def test_queue_when_total_live_at_budget():
    state = SpawnerState(
        nodes={
            f"c{i}": {"parent": "root", "status": "running", "depth": 1}
            for i in range(4)
        }
    )
    assert admit_spawn(state, _req(), _concurrency(max_total_live=4)) == AdmissionResult.QUEUE


def test_queue_when_parent_fanout_at_budget():
    state = SpawnerState(
        nodes={
            f"c{i}": {"parent": "alpha", "status": "running", "depth": 1}
            for i in range(2)
        }
    )
    assert (
        admit_spawn(state, _req("alpha"), _concurrency(max_fan_out_per_parent=2))
        == AdmissionResult.QUEUE
    )


def test_dead_children_do_not_count_against_fanout():
    state = SpawnerState(
        nodes={
            "c1": {"parent": "alpha", "status": "dead", "depth": 1},
            "c2": {"parent": "alpha", "status": "running", "depth": 1},
        }
    )
    assert (
        admit_spawn(state, _req("alpha"), _concurrency(max_fan_out_per_parent=2))
        == AdmissionResult.ADMIT
    )


# --- tree persistence ---


def test_save_and_load_tree(session_id):
    state = SpawnerState(
        nodes={"c1": {"parent": "root", "status": "running", "depth": 1}},
        queue=[],
        spawned_total=1,
    )
    save_tree(session_id, state)
    loaded = load_tree(session_id)
    assert loaded.live() == 1
    assert loaded.spawned_total == 1
    assert "c1" in loaded.nodes


def test_load_tree_empty_when_missing(session_id):
    state = load_tree(session_id)
    assert state.live() == 0
    assert state.nodes == {}


def test_register_spawn_updates_state(session_id):
    state = SpawnerState()
    state = register_spawn(session_id, state, _req("root", 1), "c1", pid=12345)
    assert state.nodes["c1"]["pid"] == 12345
    assert state.nodes["c1"]["status"] == "running"
    assert state.spawned_total == 1
    # Persisted
    reloaded = load_tree(session_id)
    assert "c1" in reloaded.nodes


def test_mark_dead(session_id):
    state = SpawnerState()
    state = register_spawn(session_id, state, _req(), "c1", pid=1)
    state = mark_dead(session_id, state, "c1")
    assert state.nodes["c1"]["status"] == "dead"
    assert state.live() == 0


def test_enqueue_and_drain(session_id):
    # 4 children of distinct parents so total-live is the only bottleneck
    state = SpawnerState(
        nodes={
            f"c{i}": {"parent": f"p{i}", "status": "running", "depth": 1}
            for i in range(4)
        }
    )
    c = _concurrency(max_total_live=4, max_fan_out_per_parent=4)
    # Queued request targets a fresh parent
    state = enqueue(session_id, state, _req("q_parent"))
    assert len(state.queue) == 1
    # At total-live budget, draining returns None
    drained = drain_queue_one(session_id, state, c)
    assert drained is None
    # Kill one child, now the queue drains
    state = mark_dead(session_id, state, "c0")
    drained = drain_queue_one(session_id, state, c)
    assert drained is not None
    assert state.queue == []


def test_depth_exceeded_finding_format(session_id):
    f = depth_exceeded_finding(session_id, _req("root", 10), _concurrency(max_depth=3))
    assert f.type == "drift"
    assert f.subtype == "recursion_no_base"
    assert "depth=10" in f.evidence.claim_excerpt
    assert "max_depth=3" in f.evidence.claim_excerpt
