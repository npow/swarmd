"""Verify launch.sh references all expected daemon specialists."""

from __future__ import annotations

from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[1] / "launch.sh"


def _script() -> str:
    return LAUNCHER.read_text()


def test_launcher_references_all_daemons():
    text = _script()
    for name in (
        "pattern_detector",
        "success_verifier",
        "coordinator",
        "supervisor",
        "llm_loop",
        "spawner",
    ):
        assert name in text, f"launcher does not reference {name}"


def test_launcher_has_cleanup_trap():
    text = _script()
    assert "trap cleanup" in text
    assert "kill -9" in text, "launcher should SIGKILL stragglers on exit"


def test_launcher_hash_pins_mission():
    text = _script()
    assert "hash-pin" in text
    assert "_launch_helper.py" in text


def test_launcher_writes_workspace_settings():
    text = _script()
    assert "$WORKSPACE/.claude/settings.json" in text
    assert "SETTINGS_BAK" in text, "launcher must back up existing settings"


def test_launcher_writes_launcher_pid_before_spawning_specialists():
    """The launcher MUST drop a launcher.pid file before specialists spawn.

    Specialists consult this pid on every loop tick and self-terminate if the
    launcher is gone. Without this, SIGKILL on launch.sh leaks daemons forever
    (see swarm.lib.launcher_liveness for the mechanism).

    Ordering matters: the pid must be recorded BEFORE the spawn loop. Otherwise
    a specialist could race ahead, notice the file is missing, and exit
    immediately — which would look to the user like the daemon is crashing.
    """
    text = _script()
    assert "launcher.pid" in text, (
        "launcher must write a launcher.pid file so specialists can check "
        "liveness; see swarm/lib/launcher_liveness.py"
    )
    pid_write_idx = text.index("launcher.pid")
    # "swarm.specialists." appears in the spawn loop:
    #   python3 -m "swarm.specialists.$d" "$SESSION_ID"
    spawn_idx = text.index("swarm.specialists.")
    assert pid_write_idx < spawn_idx, (
        "launcher.pid must be written BEFORE the specialist spawn loop "
        "to avoid a race where a specialist starts and self-exits "
        "before the pid file exists."
    )
