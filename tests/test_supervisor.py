"""Tests for supervisor daemon."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from swarm.lib.heartbeat import beat
from swarm.lib.locking import write_line
from swarm.lib.paths import findings_path, health_beat_path
from swarm.schemas.finding import Finding
from swarm.specialists.supervisor import (
    DEFAULT_SPECIALISTS,
    ROTATION_EXHAUSTION_K,
    HealthStatus,
    check_all,
    check_rotation_exhaustion,
    count_rotation_cheats,
    emit_mission_level_alert,
    respawn,
)


@dataclass
class FakeProc:
    pid: int = 99999


def _stub_spawner(argv: list[str], env: dict[str, str]) -> FakeProc:
    _stub_spawner.last_argv = argv  # type: ignore[attr-defined]
    return FakeProc()


def _write_cheat(session_id: str, subtype: str, i: int = 0) -> None:
    f = Finding(
        id=f"f-{i}-cheat",
        source="anticheat",
        subject_session=session_id,
        spawner_id=session_id,
        type="cheat",
        subtype=subtype,
        severity="critical",
        verdict="detected",
    )
    write_line(findings_path(session_id), f.model_dump_json())


def test_check_all_reports_stale_when_no_beats(session_id):
    statuses = check_all(session_id, specialists=("pattern_detector",))
    assert len(statuses) == 1
    assert statuses[0].stale is True
    assert statuses[0].pid is None


def test_check_all_reports_fresh_when_recent_beat(session_id):
    beat(session_id, "pattern_detector", 1)
    statuses = check_all(session_id, specialists=("pattern_detector",))
    assert statuses[0].stale is False
    assert statuses[0].pid is not None


def test_check_all_reports_stale_for_old_beat(session_id):
    # Write a beat with an ancient timestamp
    hb = health_beat_path(session_id, "pattern_detector")
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text(
        json.dumps(
            {
                "pid": 123,
                "last_cycle_ts": time.time() - 300,  # 5 min ago
                "cycles_completed": 1,
            }
        )
    )
    statuses = check_all(session_id, specialists=("pattern_detector",))
    assert statuses[0].stale is True


def test_respawn_calls_spawner(session_id, monkeypatch):
    monkeypatch.setenv("REPO_ROOT", "/Users/npow/code/research")
    pid = respawn(session_id, "pattern_detector", spawner=_stub_spawner)
    assert pid == 99999
    argv = _stub_spawner.last_argv  # type: ignore[attr-defined]
    assert argv[-1] == session_id
    assert "swarm.specialists.pattern_detector" in argv[-2]


def test_count_rotation_cheats(session_id):
    _write_cheat(session_id, "scope_reduction", 1)
    _write_cheat(session_id, "scope_reduction", 2)
    _write_cheat(session_id, "mock_out", 3)
    counts = count_rotation_cheats(session_id)
    assert counts == {"scope_reduction": 2, "mock_out": 1}


def test_count_rotation_cheats_empty(session_id):
    counts = count_rotation_cheats(session_id)
    assert counts == {}


def test_check_rotation_exhaustion_triggers_at_threshold(session_id):
    for i in range(ROTATION_EXHAUSTION_K):
        _write_cheat(session_id, "scope_reduction", i)
    exhausted = check_rotation_exhaustion(session_id)
    assert "scope_reduction" in exhausted


def test_check_rotation_exhaustion_below_threshold(session_id):
    _write_cheat(session_id, "scope_reduction", 1)
    exhausted = check_rotation_exhaustion(session_id)
    assert exhausted == []


def test_emit_mission_level_alert_structure(session_id):
    finding = emit_mission_level_alert(session_id, ["scope_reduction", "mock_out"])
    assert finding.type == "meta"
    assert finding.subtype == "mission_level_alert_pending"
    assert finding.severity == "critical"
    assert "scope_reduction" in finding.verdict
    assert "mock_out" in finding.verdict
    assert "User review required" in finding.verdict


def test_default_specialists_list():
    assert "pattern_detector" in DEFAULT_SPECIALISTS
    assert "success_verifier" in DEFAULT_SPECIALISTS
    assert "coordinator" in DEFAULT_SPECIALISTS


def test_health_status_frozen():
    s = HealthStatus("x", True, None)
    try:
        s.stale = False  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("HealthStatus should be frozen")
