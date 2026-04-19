"""Specialists must self-terminate when their launcher is gone.

These tests spawn each specialist as a real subprocess in an isolated
SWARM_ROOT and assert they exit cleanly without a launcher.pid file.
If the check is ever removed from a specialist's main loop, the
corresponding test will hang until the timeout and then fail — which
is the outcome we want (a noisy regression, not a silent leak).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarmd.lib.launcher_liveness import launcher_pid_path, write_launcher_pid
from swarmd.lib.paths import ensure_session_dirs, mission_yaml_path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every long-running specialist the launcher starts. event_scribe is
# deliberately NOT in this list — it's a library, not a daemon.
SPECIALISTS = [
    "coordinator",
    "pattern_detector",
    "success_verifier",
    "supervisor",
    "llm_loop",
    "resource_monitor",
]


def _spawn_specialist(specialist: str, session_id: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    # Pass the tmp SWARM_ROOT / SWARM_CONFIG from the fixture's monkeypatch
    # through to the subprocess so it points at the same isolated root.
    env["SWARM_ROOT"] = os.environ["SWARM_ROOT"]
    env["SWARM_CONFIG"] = os.environ.get(
        "SWARM_CONFIG", str(Path(os.environ["SWARM_ROOT"]).parent / "config")
    )
    env["PEER_CONSULT_DISABLED"] = "1"
    return subprocess.Popen(
        [sys.executable, "-m", f"swarm.specialists.{specialist}", session_id],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _write_valid_mission(session_id: str, workspace: Path) -> None:
    """Write a minimal valid mission.yaml so mission-loading specialists
    don't crash before they can check launcher liveness."""
    mission_yaml_path(session_id).parent.mkdir(parents=True, exist_ok=True)
    mission_yaml_path(session_id).write_text(
        f"""mission: "test"
workspace: "{workspace}"
success_criteria:
  - id: ok
    description: "trivial"
    check: "true"
    timeout_sec: 5
verification:
  run_every_sec: 10
  hold_window_sec: 5
"""
    )


@pytest.mark.parametrize("specialist", SPECIALISTS)
def test_specialist_exits_when_no_launcher_pid(
    tmp_swarm_root, session_id, tmp_path, specialist
):
    """Specialist MUST exit(0) quickly when launcher.pid does not exist.

    Without a launcher.pid, the only correct behavior is to self-terminate
    immediately — otherwise a kill -9 on launch.sh would leak this daemon
    forever (the bug that motivated this module).
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_valid_mission(session_id, workspace)
    # Make sure no launcher.pid exists (ensure_session_dirs ran via fixture,
    # but doesn't create this file).
    assert not launcher_pid_path(session_id).exists()

    proc = _spawn_specialist(specialist, session_id)
    try:
        rc = proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail(
            f"{specialist} did NOT self-terminate within 8s — this is the "
            f"leak bug. Stderr:\n{proc.stderr.read().decode()}"
        )
    stderr = proc.stderr.read().decode()
    assert rc == 0, (
        f"{specialist} exited with {rc} (expected 0).\nstderr:\n{stderr}"
    )


@pytest.mark.parametrize("specialist", SPECIALISTS)
def test_specialist_exits_when_launcher_pid_dead(
    tmp_swarm_root, session_id, tmp_path, specialist
):
    """Specialist exits cleanly when launcher.pid points to a dead pid."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_valid_mission(session_id, workspace)

    # Spawn a short-lived process, capture its pid, wait for it to die.
    dead_proc = subprocess.Popen(["/bin/sh", "-c", "exit 0"])
    dead_pid = dead_proc.pid
    dead_proc.wait()
    time.sleep(0.1)  # kernel reap grace period

    write_launcher_pid(session_id, dead_pid)

    proc = _spawn_specialist(specialist, session_id)
    try:
        rc = proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail(
            f"{specialist} did NOT exit when launcher pid was dead. "
            f"Stderr:\n{proc.stderr.read().decode()}"
        )
    assert rc == 0


def test_specialist_stays_alive_when_launcher_is_alive(
    tmp_swarm_root, session_id, tmp_path
):
    """Sanity: with a live launcher.pid, a specialist does NOT exit early.

    We use coordinator (period_sec=5.0) and give it 2 seconds — well within
    its first loop iteration. The process should still be running.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    _write_valid_mission(session_id, workspace)
    # Write OUR pid as the launcher pid — pytest is alive, so the check
    # will pass. This isolates us from the mission-loading code.
    write_launcher_pid(session_id, os.getpid())
    ensure_session_dirs(session_id)

    proc = _spawn_specialist("coordinator", session_id)
    try:
        time.sleep(2.0)
        assert proc.poll() is None, (
            "coordinator exited prematurely while launcher pid was alive.\n"
            f"stderr:\n{proc.stderr.read().decode()}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
