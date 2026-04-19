"""Tests for spawner.run_daemon_once — the part that actually launches subprocesses."""

from __future__ import annotations

from dataclasses import dataclass

from swarmd.schemas.mission import Concurrency
from swarmd.specialists.spawner import (
    SpawnerState,
    enqueue,
    load_tree,
    run_daemon_once,
    save_tree,
)


@dataclass
class FakeProc:
    pid: int = 55555


def _spawner(argv: list[str], env: dict[str, str]) -> FakeProc:
    _spawner.last_argv = argv  # type: ignore[attr-defined]
    _spawner.last_env = env  # type: ignore[attr-defined]
    return FakeProc()


def test_run_daemon_once_drains_admitted_request(session_id):
    # Start with empty tree, enqueue a request that would ADMIT immediately
    state = SpawnerState()
    from swarmd.specialists.spawner import SpawnRequest

    state = enqueue(session_id, state, SpawnRequest(parent_id="root", depth=1, mission="do X"))
    c = Concurrency(max_total_live=4, max_depth=3, max_fan_out_per_parent=2)

    result = run_daemon_once(session_id, c, spawner=_spawner)
    assert result == 1

    # Verify tree state was updated
    reloaded = load_tree(session_id)
    assert reloaded.live() == 1
    assert reloaded.queue == []
    # spawn_total incremented
    assert reloaded.spawned_total == 1

    # Verify spawner was called correctly
    argv = _spawner.last_argv  # type: ignore[attr-defined]
    assert "claude" in argv[0]
    assert "--session-id" in argv
    assert session_id in argv
    # Mission text present
    assert "do X" in argv[-1]


def test_run_daemon_once_no_queue(session_id):
    c = Concurrency(max_total_live=4, max_depth=3, max_fan_out_per_parent=2)
    result = run_daemon_once(session_id, c, spawner=_spawner)
    assert result == 0


def test_run_daemon_once_respects_budget(session_id):
    # Fill the tree to max_total_live
    state = SpawnerState(
        nodes={
            "c0": {"parent": "root", "status": "running", "depth": 1},
            "c1": {"parent": "p1", "status": "running", "depth": 1},
        }
    )
    from swarmd.specialists.spawner import SpawnRequest

    state = enqueue(session_id, state, SpawnRequest(parent_id="root", depth=1, mission="queued"))
    save_tree(session_id, state)

    c = Concurrency(max_total_live=2, max_depth=3, max_fan_out_per_parent=4)
    result = run_daemon_once(session_id, c, spawner=_spawner)
    # At budget — nothing admitted
    assert result == 0
    reloaded = load_tree(session_id)
    assert len(reloaded.queue) == 1  # request still queued


def test_run_daemon_once_handles_spawner_error(session_id):
    from swarmd.specialists.spawner import SpawnRequest

    state = SpawnerState()
    state = enqueue(session_id, state, SpawnRequest(parent_id="root", depth=1, mission="x"))
    c = Concurrency()

    def _failing(argv, env):
        raise RuntimeError("simulated spawn failure")

    result = run_daemon_once(session_id, c, spawner=_failing)
    # Returns 0 because spawn failed; request was popped from queue already
    # (current semantics: pop then try; failure just means no running child)
    assert result == 0
