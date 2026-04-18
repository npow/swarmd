"""Integration test: actually run launch.sh with a stubbed claude binary.

Exercises the full launcher flow end-to-end:
  - mint session_id
  - pin mission hashes
  - spawn daemons
  - launch claude (stubbed)
  - trap cleanup on exit: kill daemons, restore settings.json

No mocks at the shell contract — we use a real temp dir and a real
stub claude binary that exits after a short sleep.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "swarm" / "launch.sh"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _find_session_specialist_pids(session_id: str) -> list[int]:
    """Return pids of any `swarm.specialists.*` processes for this session."""
    return [pid for pid, _ in _find_session_specialists(session_id)]


def _find_session_specialists(session_id: str) -> list[tuple[int, str]]:
    """Return (pid, command_line) for each `swarm.specialists.*` process
    scoped to this session_id. Specialists are launched with the session_id
    as argv[1], so leaked daemons show up here.
    """
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    out: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        if "swarm.specialists." not in line:
            continue
        if session_id not in line:
            continue
        parts = line.strip().split(maxsplit=1)
        try:
            out.append((int(parts[0]), parts[1] if len(parts) > 1 else ""))
        except (ValueError, IndexError):
            continue
    return out


@pytest.fixture
def e2e_env():
    """Set up a throwaway filesystem + stubbed claude binary."""
    root = Path(tempfile.mkdtemp(prefix="swarm_e2e_"))
    try:
        (root / "bin").mkdir()
        (root / "swarm").mkdir()
        (root / "cfg").mkdir()
        (root / "ws" / "app" / "tests").mkdir(parents=True)
        # Stub claude that exits after 2s so trap runs quickly
        (root / "bin" / "claude").write_text(
            "#!/bin/bash\n"
            'echo "[STUB] args: $*" >&2\n'
            "sleep 2\n"
            "exit 0\n"
        )
        (root / "bin" / "claude").chmod(0o755)

        # Minimal workspace
        (root / "ws" / "app" / "__init__.py").touch()
        (root / "ws" / "app" / "tests" / "__init__.py").touch()
        (root / "ws" / "app" / "fizzbuzz.py").write_text(
            "def fizzbuzz(n):\n"
            "    if n % 15 == 0: return 'FizzBuzz'\n"
            "    if n % 3 == 0: return 'Fizz'\n"
            "    if n % 5 == 0: return 'Buzz'\n"
            "    return str(n)\n"
        )
        (root / "ws" / "app" / "tests" / "test_fizzbuzz.py").write_text(
            "from app.fizzbuzz import fizzbuzz\n"
            "def test(): assert fizzbuzz(15) == 'FizzBuzz'\n"
        )

        # Minimal mission
        (root / "mission.yaml").write_text(
            f"""mission: "smoke test"
workspace: "{root / 'ws'}"
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

        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _run_launcher(root: Path, timeout: int = 25) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["SWARM_ROOT"] = str(root / "swarm")
    env["SWARM_CONFIG"] = str(root / "cfg")
    env["PATH"] = f"{root / 'bin'}:" + env.get("PATH", "")
    return subprocess.run(
        ["bash", str(LAUNCHER), str(root / "mission.yaml")],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_launcher_runs_and_cleans_up(e2e_env):
    result = _run_launcher(e2e_env)

    # Launcher must exit cleanly (0 or claude's exit code)
    assert result.returncode == 0, f"launcher failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"

    # Expected log markers
    assert "Swarm v0.2 launching" in result.stdout
    assert "hash-pinned" in result.stdout
    assert "Launching claude" in result.stdout
    assert "tearing down" in result.stdout

    # Workspace's .claude/settings.json should be cleaned up
    post = e2e_env / "ws" / ".claude" / "settings.json"
    assert not post.exists(), "launcher failed to remove settings.json on exit"


def test_launcher_creates_session_state(e2e_env):
    result = _run_launcher(e2e_env)
    assert result.returncode == 0, result.stderr

    state_dir = e2e_env / "swarm" / "state"
    assert state_dir.exists()
    sessions = list(state_dir.iterdir())
    assert len(sessions) == 1, f"expected 1 session dir, got {sessions}"
    sdir = sessions[0]
    # Specialists should have at least created their log files
    for d in ("pattern_detector", "success_verifier", "coordinator"):
        assert (sdir / f"{d}.log").exists(), f"{d}.log not created"


def test_launcher_hash_pins_mission(e2e_env):
    result = _run_launcher(e2e_env)
    assert result.returncode == 0, result.stderr

    # mission.lock.json should exist and contain sha256 of mission.yaml
    mission_dir = e2e_env / "swarm" / "missions"
    sessions = list(mission_dir.iterdir())
    assert len(sessions) == 1
    lock = sessions[0] / "mission.lock.json"
    assert lock.exists()
    import json

    data = json.loads(lock.read_text())
    assert "mission.yaml" in data["files"]
    assert data["files"]["mission.yaml"].startswith("sha256:")


def test_launcher_rejects_missing_mission():
    env = os.environ.copy()
    r = subprocess.run(
        ["bash", str(LAUNCHER), "/nonexistent/mission.yaml"],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert r.returncode != 0
    assert "not found" in r.stderr.lower() or "not found" in r.stdout.lower()


def test_launcher_rejects_no_args():
    r = subprocess.run(
        ["bash", str(LAUNCHER)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert r.returncode != 0
    assert "usage" in r.stderr.lower()


def test_launcher_sigkill_does_not_leak_daemons(e2e_env):
    """SIGKILL on the launcher must not leave orphaned specialist daemons.

    The bash `trap cleanup EXIT INT TERM` does NOT fire on SIGKILL — that's
    by design in POSIX. The only line of defense is that specialists read
    $SESSION_STATE/launcher.pid each tick and self-terminate when the pid
    is dead. This is the regression test for the leaked-daemon bug: 74 orphan
    processes across 45 sessions observed in the wild.

    Worst-case latency is bounded by the longest specialist period
    (resource_monitor: 30s), so we allow up to 45s for all daemons to exit
    after we SIGKILL the launcher.
    """
    # Replace the 2-second claude stub with one that blocks indefinitely
    # on the main-session invocation (`claude --session-id ...`) but returns
    # immediately for LLM-critic invocations (`claude -p --bare ...`).
    # Without the short-circuit on -p, coordinator's anticheat_run_panel and
    # llm_loop's drift_judge block for 120-180s inside subprocess.run — not
    # a leak, but the loop can't notice the dead launcher until the subprocess
    # returns.  For this test we only care about the liveness check, not the
    # critic behavior, so short-circuit to keep the assertion window tight.
    stub = e2e_env / "bin" / "claude"
    stub.write_text(
        "#!/bin/bash\n"
        'if [[ "$1" == "-p" ]]; then\n'
        "  # LLM-critic invocation. Return a short innocuous response fast.\n"
        '  echo \'{"verdict":"UNCLEAR","reason":"test stub"}\'\n'
        "  exit 0\n"
        "fi\n"
        'echo "[STUB sleeping until killed] args: $*" >&2\n'
        "trap '' TERM INT\n"
        "sleep 300\n"
    )
    stub.chmod(0o755)

    env = os.environ.copy()
    env["SWARM_ROOT"] = str(e2e_env / "swarm")
    env["SWARM_CONFIG"] = str(e2e_env / "cfg")
    env["PATH"] = f"{e2e_env / 'bin'}:" + env.get("PATH", "")

    launcher = subprocess.Popen(
        ["bash", str(LAUNCHER), str(e2e_env / "mission.yaml")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    captured_pids: list[int] = []
    try:
        # Discover the session_id by watching the state dir.
        state_dir = e2e_env / "swarm" / "state"
        session_id: str | None = None
        deadline = time.time() + 20
        while time.time() < deadline and session_id is None:
            if state_dir.exists():
                sessions = list(state_dir.iterdir())
                if sessions:
                    session_id = sessions[0].name
            time.sleep(0.2)
        assert session_id is not None, (
            "session dir never appeared. launcher stderr:\n"
            + launcher.stderr.read(4000).decode(errors="replace")
        )

        # Wait for at least 3 specialists to have written heartbeats,
        # confirming they're really running (not just forked).
        health_dir = state_dir / session_id / "health"
        deadline = time.time() + 30
        while time.time() < deadline:
            if health_dir.exists():
                beats = list(health_dir.glob("*.beat"))
                if len(beats) >= 3:
                    break
            time.sleep(0.5)

        captured_pids = _find_session_specialist_pids(session_id)
        assert captured_pids, (
            "no specialist processes found for session "
            f"{session_id!r} before SIGKILL — launcher didn't spawn them?"
        )

        # SIGKILL the launcher. The bash trap will NOT fire.
        os.kill(launcher.pid, signal.SIGKILL)
        launcher.wait(timeout=5)
        assert not _pid_alive(launcher.pid), "launcher bash still alive after SIGKILL"

        # Poll for all session-tagged specialist processes to exit.
        # The worst-case is resource_monitor at 30s period; give 45s buffer.
        gc_deadline = time.time() + 45
        while time.time() < gc_deadline:
            if not _find_session_specialist_pids(session_id):
                break
            time.sleep(1)

        still_running = _find_session_specialists(session_id)
        assert not still_running, (
            f"LEAK: {len(still_running)} orphan daemons still running "
            f"45s after SIGKILL on launcher:\n"
            + "\n".join(f"  pid={pid} cmd={cmd}" for pid, cmd in still_running)
            + "\nEach specialist main loop must call "
            "exit_if_launcher_dead(session_id) at the top of every tick."
        )
    finally:
        # Ensure no stragglers escape this test regardless of outcome.
        if launcher.poll() is None:
            try:
                launcher.kill()
                launcher.wait(timeout=3)
            except Exception:
                pass
        for pid in captured_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
