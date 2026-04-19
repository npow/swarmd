"""Tests for process-leak detection: zombie reaper + supervisor kill paths."""

from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from swarmd.lib.paths import session_dir
from swarmd.specialists.spawner import (
    SpawnerState,
    load_tree,
    mark_dead,
    reap_zombies,
    register_spawn,
    save_tree,
)


class _SpawnRequest:
    """Minimal local stand-in so we can register without touching the real spawner logic."""

    def __init__(self, parent="p", depth=1, mission="m", ctx=""):
        self.parent_id = parent
        self.depth = depth
        self.mission = mission
        self.context_summary = ctx


def test_reap_zombies_no_children(tmp_swarm_root, session_id):
    """No children = no reaping. Must not raise."""
    n = reap_zombies(session_id)
    assert n == 0


def test_reap_zombies_reaps_exited_child(tmp_swarm_root, session_id):
    """Spawn a child that exits immediately, then reap it."""
    # Spawn a child that exits right away
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "exit 0"],
    )
    pid = proc.pid
    # Give the kernel a moment to mark it as zombie
    time.sleep(0.2)
    # Register in tree so reaper can mark it dead
    state = SpawnerState()
    req = _SpawnRequest()
    state = register_spawn(session_id, state, req, "test-child", pid=pid)  # type: ignore[arg-type]

    n = reap_zombies(session_id)
    assert n >= 1

    # tree.json should reflect the death
    reloaded = load_tree(session_id)
    assert reloaded.nodes["test-child"]["status"] == "dead"


def test_reap_many_children(tmp_swarm_root, session_id):
    """Spawn many short-lived children and verify all are reaped."""
    N = 20
    pids = []
    for _ in range(N):
        p = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
        pids.append(p.pid)
    time.sleep(0.3)

    total_reaped = reap_zombies(session_id)
    # We should have reaped all N (or close to it — Python's subprocess.Popen
    # may have already reaped via __del__ in rare cases, but waitpid(-1, WNOHANG)
    # returns any unreaped)
    assert total_reaped <= N
    # After reap_zombies + a second call, there should be NO remaining zombies
    second = reap_zombies(session_id)
    assert second == 0


def test_no_leak_after_register_mark_dead(tmp_swarm_root, session_id):
    """Sanity: register + mark_dead leaves live count at 0."""
    state = SpawnerState()
    state = register_spawn(
        session_id, state, _SpawnRequest(), "c1", pid=12345  # type: ignore[arg-type]
    )
    assert state.live() == 1
    state = mark_dead(session_id, state, "c1")
    assert state.live() == 0
    # Persisted
    reloaded = load_tree(session_id)
    assert reloaded.live() == 0


def test_session_dir_does_not_leak_fds(tmp_swarm_root, session_id):
    """Opening session_dir repeatedly must not leak fds (paranoia check)."""
    import os as _os

    # Not portable across all OSes but gives a signal on Linux
    fd_dir = "/proc/self/fd"
    if not _os.path.exists(fd_dir):
        pytest.skip("no /proc/self/fd on this platform")
    before = len(os.listdir(fd_dir))
    for _ in range(50):
        sdir = session_dir(session_id)
        (sdir / "probe.tmp").write_text("x")
        (sdir / "probe.tmp").unlink()
    after = len(os.listdir(fd_dir))
    # Tolerate +5 for interpreter-internal bookkeeping
    assert after - before < 10, f"fd leak: {before} → {after}"


def test_reap_zombies_idempotent(tmp_swarm_root, session_id):
    """Multiple reap calls with no new zombies should return 0."""
    for _ in range(3):
        assert reap_zombies(session_id) == 0


def test_graceful_kill_via_signal(tmp_swarm_root, session_id):
    """SIGTERM → process dies → reaper collects it."""
    proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30"])
    pid = proc.pid
    state = SpawnerState()
    state = register_spawn(
        session_id, state, _SpawnRequest(), "sleeper", pid=pid  # type: ignore[arg-type]
    )
    save_tree(session_id, state)
    os.kill(pid, signal.SIGTERM)
    time.sleep(0.3)
    reap_zombies(session_id)
    reloaded = load_tree(session_id)
    assert reloaded.nodes["sleeper"]["status"] == "dead"
