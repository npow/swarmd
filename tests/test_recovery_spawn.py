"""Tests for recovery_spawn."""

from __future__ import annotations

import json
from dataclasses import dataclass

import yaml

from swarmd.lib.paths import mission_yaml_path, session_dir
from swarmd.specialists.recovery_spawn import (
    RecoveryResult,
    spawn_recovery,
    write_briefing,
)


@dataclass
class FakeProc:
    pid: int = 12345


def _stub_spawner(argv: list[str], env: dict[str, str]) -> FakeProc:
    # Record args for later inspection
    _stub_spawner.last_argv = argv  # type: ignore[attr-defined]
    _stub_spawner.last_env = env  # type: ignore[attr-defined]
    return FakeProc()


def _write_mission(session_id, text="rebuild auth system"):
    p = mission_yaml_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump(
            {
                "mission": text,
                "workspace": "/tmp",
                "success_criteria": [
                    {"id": "a", "description": "", "check": "true"}
                ],
            }
        )
    )


# --- write_briefing ---


def test_briefing_written(session_id):
    p = write_briefing(
        session_id,
        reason="test",
        failed_signatures=["sig123"],
        tried_strategies=["decomposition"],
        last_n_findings=["cheat: scope_reduction"],
    )
    assert p.exists()
    body = p.read_text()
    assert "sig123" in body
    assert "decomposition" in body
    assert "scope_reduction" in body
    assert "Do NOT repeat" in body


def test_briefing_handles_empty_lists(session_id):
    p = write_briefing(
        session_id,
        reason="r",
        failed_signatures=[],
        tried_strategies=[],
        last_n_findings=[],
    )
    assert p.exists()
    body = p.read_text()
    assert "Recovery Briefing" in body


# --- spawn_recovery ---


def test_spawn_recovery_writes_briefing_and_spawns(session_id):
    _write_mission(session_id)
    r = spawn_recovery(
        session_id,
        reason="strike 3 exhausted",
        failed_signatures=["abc"],
        tried_strategies=["templated_diversity"],
        spawner=_stub_spawner,
    )
    assert r.spawned is True
    assert r.pid == 12345
    assert r.briefing_path is not None
    assert r.briefing_path.exists()

    # Verify spawner was called with the right args
    argv = _stub_spawner.last_argv  # type: ignore[attr-defined]
    assert argv[0] == "claude"
    assert "--session-id" in argv
    assert session_id in argv
    # Mission prose included in the last arg
    assert "rebuild auth system" in argv[-1]
    assert "RECOVERY spawn" in argv[-1]
    assert str(r.briefing_path) in argv[-1]

    # SESSION_ID preserved in env
    env = _stub_spawner.last_env  # type: ignore[attr-defined]
    assert env.get("SESSION_ID") == session_id


def test_spawn_recovery_writes_in_flight_marker(session_id):
    _write_mission(session_id)
    spawn_recovery(
        session_id,
        reason="r",
        spawner=_stub_spawner,
    )
    marker = session_dir(session_id) / "recovery_in_flight.json"
    assert marker.exists()
    data = json.loads(marker.read_text())
    assert data["pid"] == 12345
    assert "spawned_at" in data
    assert data["reason"] == "r"


def test_spawn_recovery_fails_when_no_mission(session_id):
    # No mission.yaml written
    r = spawn_recovery(session_id, reason="r", spawner=_stub_spawner)
    assert r.spawned is False
    assert r.error is not None
    assert "mission.yaml not found" in r.error


def test_spawn_recovery_handles_spawner_error(session_id):
    _write_mission(session_id)

    def _err_spawn(_argv, _env):
        raise RuntimeError("simulated spawn failure")

    r = spawn_recovery(session_id, reason="r", spawner=_err_spawn)
    assert r.spawned is False
    assert r.error is not None
    assert "simulated spawn failure" in r.error


def test_recovery_result_frozen():
    r = RecoveryResult(spawned=True, pid=1, briefing_path=None)
    try:
        r.spawned = False  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("RecoveryResult should be frozen")
