"""Tests for SessionSnapshot data layer — the abstraction boundary
between state files and consumers. When Temporal replaces file-backed
state, these tests re-run unchanged against a Temporal-backed load()."""

from __future__ import annotations

import math
import time

import pytest

from swarmd.lib.status import (
    CriterionStatus,
    SessionSnapshot,
    SpecialistHealth,
)


def test_criterion_status_fields():
    c = CriterionStatus(id="x", status="pass", exit_code=0, last_check_ts=123.0)
    assert c.id == "x"
    assert c.status == "pass"
    assert c.exit_code == 0
    assert c.last_check_ts == 123.0


def test_criterion_status_frozen():
    c = CriterionStatus(id="x", status="pass", exit_code=0, last_check_ts=0.0)
    with pytest.raises((AttributeError, TypeError)):
        c.status = "fail"  # type: ignore[misc]


def test_specialist_health_fields():
    h = SpecialistHealth(
        name="coordinator",
        pid=1234,
        last_beat_age_sec=3.0,
        is_stale=False,
        cycles=42,
    )
    assert h.name == "coordinator"
    assert h.pid == 1234
    assert h.is_stale is False


def test_specialist_health_inf_age_for_missing_beat():
    h = SpecialistHealth(
        name="llm_loop",
        pid=None,
        last_beat_age_sec=math.inf,
        is_stale=True,
        cycles=0,
    )
    assert math.isinf(h.last_beat_age_sec)


def test_load_empty_session_returns_sentinel_snapshot(tmp_swarm_root, session_id):
    """No files written beyond ensure_session_dirs() — every field should
    come back with its pinned sentinel value, never raise."""
    snap = SessionSnapshot.load(session_id)
    assert snap.session_id == session_id
    assert snap.mission_title == ""
    assert snap.workspace == ""
    assert snap.launcher_alive is False
    assert snap.started_at == 0.0
    assert snap.duration_sec == 0.0
    assert snap.iter_count == 0
    assert snap.criteria == []
    assert snap.all_pass is False
    assert snap.hold_sec == 0.0
    assert snap.hold_target_sec == 0.0
    assert snap.findings_total == 0
    assert snap.findings_critical == 0
    assert snap.findings_major == 0
    assert snap.recent_findings == []
    assert snap.interventions_total == 0
    assert snap.interventions_pending_ack == 0
    assert snap.recent_interventions == []
    assert snap.health == []
    assert snap.events_per_minute == 0.0
    assert snap.events_total == 0


def test_load_invalid_session_id_raises(tmp_swarm_root):
    with pytest.raises(ValueError):
        SessionSnapshot.load("../etc")


import json
import os


def test_load_launcher_alive_when_pid_file_has_live_pid(tmp_swarm_root, session_id):
    from swarmd.lib.launcher_liveness import write_launcher_pid

    write_launcher_pid(session_id, os.getpid())
    snap = SessionSnapshot.load(session_id)
    assert snap.launcher_alive is True


def test_load_launcher_dead_when_pid_file_missing(tmp_swarm_root, session_id):
    # No write_launcher_pid call
    snap = SessionSnapshot.load(session_id)
    assert snap.launcher_alive is False


def test_load_criteria_from_verifier_status(tmp_swarm_root, session_id):
    from swarmd.lib.paths import session_dir

    (session_dir(session_id) / "verifier_status.json").write_text(
        json.dumps(
            {
                "ts": 1234.5,
                "all_pass": False,
                "per_criterion": {
                    "pytest_passes": {"status": "pass", "exit_code": 0},
                    "no_mocks": {"status": "fail", "exit_code": 1},
                },
            }
        )
    )
    snap = SessionSnapshot.load(session_id)
    assert snap.all_pass is False
    assert len(snap.criteria) == 2
    by_id = {c.id: c for c in snap.criteria}
    assert by_id["pytest_passes"].status == "pass"
    assert by_id["pytest_passes"].exit_code == 0
    assert by_id["pytest_passes"].last_check_ts == 1234.5
    assert by_id["no_mocks"].status == "fail"
    assert by_id["no_mocks"].exit_code == 1


def test_load_criteria_when_verifier_status_missing(tmp_swarm_root, session_id):
    snap = SessionSnapshot.load(session_id)
    assert snap.criteria == []
    assert snap.all_pass is False


def test_load_criteria_when_verifier_status_unparseable(tmp_swarm_root, session_id):
    from swarmd.lib.paths import session_dir

    (session_dir(session_id) / "verifier_status.json").write_text("{not json")
    snap = SessionSnapshot.load(session_id)
    assert snap.criteria == []
    assert snap.all_pass is False


import yaml


def _write_mission(session_id: str, title: str, workspace: str, hold: int = 120):
    from swarmd.lib.paths import mission_yaml_path

    p = mission_yaml_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump(
            {
                "mission": title,
                "workspace": workspace,
                "success_criteria": [{"id": "ok", "description": "", "check": "true"}],
                "verification": {"run_every_sec": 10, "hold_window_sec": hold},
            }
        )
    )


def test_load_mission_title_truncates_to_80_chars(tmp_swarm_root, session_id, tmp_path):
    long = "x" * 200
    ws = tmp_path / "w"
    ws.mkdir()
    _write_mission(session_id, long, str(ws))
    snap = SessionSnapshot.load(session_id)
    assert len(snap.mission_title) == 80
    assert snap.mission_title == "x" * 80


def test_load_workspace_from_mission(tmp_swarm_root, session_id, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_mission(session_id, "Build X", str(ws))
    snap = SessionSnapshot.load(session_id)
    assert snap.workspace == str(ws)
    assert snap.mission_title == "Build X"


def test_load_hold_target_sec_from_mission(tmp_swarm_root, session_id, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _write_mission(session_id, "m", str(ws), hold=300)
    snap = SessionSnapshot.load(session_id)
    assert snap.hold_target_sec == 300.0


def test_load_hold_sec_is_zero_when_not_all_pass(tmp_swarm_root, session_id, tmp_path):
    from swarmd.lib.paths import session_dir

    ws = tmp_path / "ws"
    ws.mkdir()
    _write_mission(session_id, "m", str(ws), hold=120)
    (session_dir(session_id) / "verifier_status.json").write_text(
        json.dumps({"ts": time.time(), "all_pass": False, "per_criterion": {}})
    )
    snap = SessionSnapshot.load(session_id)
    assert snap.hold_sec == 0.0


def test_load_hold_sec_computed_from_verifier_ts_when_all_pass(
    tmp_swarm_root, session_id, tmp_path
):
    from swarmd.lib.paths import session_dir

    ws = tmp_path / "ws"
    ws.mkdir()
    _write_mission(session_id, "m", str(ws), hold=120)
    verifier_ts = time.time() - 30.0  # 30s ago
    (session_dir(session_id) / "verifier_status.json").write_text(
        json.dumps(
            {
                "ts": verifier_ts,
                "all_pass": True,
                "per_criterion": {"ok": {"status": "pass", "exit_code": 0}},
            }
        )
    )
    snap = SessionSnapshot.load(session_id)
    assert 29.0 < snap.hold_sec < 31.0


from swarmd.lib.locking import write_line
from swarmd.lib.paths import findings_path, interventions_path, interventions_acked_path
from swarmd.schemas.finding import Evidence, Finding
from swarmd.schemas.intervention import Intervention


def _mk_finding(sid: str, severity: str = "major", subtype: str = "loop") -> Finding:
    return Finding(
        id=f"f-{severity}-{subtype}-{time.time_ns()}",
        source="test",
        subject_session=sid,
        spawner_id=sid,
        type="loop",
        subtype=subtype,
        severity=severity,  # type: ignore[arg-type]
    )


def _mk_intervention(id_suffix: str = "") -> Intervention:
    return Intervention(
        id=f"i-{id_suffix}-{time.time_ns()}",
        tier="info",
        reason="test",
        consume_at="stop",
        requires_ack=True,
    )


def test_load_findings_counts(tmp_swarm_root, session_id):
    fp = findings_path(session_id)
    write_line(fp, _mk_finding(session_id, "critical", "tamper").model_dump_json())
    write_line(fp, _mk_finding(session_id, "major", "loop").model_dump_json())
    write_line(fp, _mk_finding(session_id, "major", "pass_transition").model_dump_json())
    write_line(fp, _mk_finding(session_id, "minor", "info").model_dump_json())
    snap = SessionSnapshot.load(session_id)
    assert snap.findings_total == 4
    assert snap.findings_critical == 1
    assert snap.findings_major == 2


def test_load_recent_findings_default_cap(tmp_swarm_root, session_id):
    fp = findings_path(session_id)
    for _ in range(20):
        write_line(fp, _mk_finding(session_id).model_dump_json())
    snap = SessionSnapshot.load(session_id)
    # Default cap defined in load(): 10
    assert len(snap.recent_findings) == 10
    assert snap.findings_total == 20


def test_load_malformed_findings_line_is_skipped(tmp_swarm_root, session_id):
    fp = findings_path(session_id)
    write_line(fp, _mk_finding(session_id).model_dump_json())
    write_line(fp, "{not json")
    write_line(fp, _mk_finding(session_id).model_dump_json())
    snap = SessionSnapshot.load(session_id)
    assert snap.findings_total == 2  # malformed line skipped


def test_load_interventions_counts_pending(tmp_swarm_root, session_id):
    from swarmd.lib.locking import write_line

    ip = interventions_path(session_id)
    iv_acked = _mk_intervention("a")
    iv_pending = _mk_intervention("b")
    write_line(ip, iv_acked.model_dump_json())
    write_line(ip, iv_pending.model_dump_json())
    write_line(interventions_acked_path(session_id), iv_acked.id)
    snap = SessionSnapshot.load(session_id)
    assert snap.interventions_total == 2
    assert snap.interventions_pending_ack == 1


# ---------------------------------------------------------------------------
# Task 6: health, iter_count, events
# ---------------------------------------------------------------------------

from swarmd.lib.heartbeat import beat
from swarmd.lib.paths import events_path


def test_load_health_from_beats(tmp_swarm_root, session_id):
    beat(session_id, "coordinator", cycles=5)
    beat(session_id, "supervisor", cycles=3)
    snap = SessionSnapshot.load(session_id)
    names = sorted(h.name for h in snap.health)
    assert names == ["coordinator", "supervisor"]
    for h in snap.health:
        assert h.pid is not None
        assert h.last_beat_age_sec < 5.0  # fresh
        assert h.is_stale is False


def test_load_iter_count_is_max_cycles_across_beats(tmp_swarm_root, session_id):
    beat(session_id, "coordinator", cycles=5)
    beat(session_id, "supervisor", cycles=42)
    beat(session_id, "pattern_detector", cycles=10)
    snap = SessionSnapshot.load(session_id)
    assert snap.iter_count == 42


def test_load_events_per_minute_zero_when_empty(tmp_swarm_root, session_id):
    snap = SessionSnapshot.load(session_id)
    assert snap.events_per_minute == 0.0
    assert snap.events_total == 0


def test_load_events_counts_total_lines(tmp_swarm_root, session_id):
    ep = events_path(session_id)
    # 5 bogus-but-line-terminated entries
    ep.write_text('{"x":1}\n{"x":2}\n\n{"x":3}\n{"x":4}\n{"x":5}\n')
    snap = SessionSnapshot.load(session_id)
    assert snap.events_total == 5  # blank line skipped


# ---------------------------------------------------------------------------
# Task 7: find_most_recent
# ---------------------------------------------------------------------------


def test_find_most_recent_returns_none_when_empty(tmp_swarm_root):
    assert SessionSnapshot.find_most_recent() is None


def test_find_most_recent_picks_newest_events_mtime(tmp_swarm_root, tmp_path):
    from swarmd.lib.paths import ensure_session_dirs, events_path

    sids = []
    for suffix in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
        ensure_session_dirs(suffix)
        events_path(suffix).write_text("{}\n")
        sids.append(suffix)
    # Give the middle one a stale mtime, and the newest a fresh mtime
    old = time.time() - 3600
    newer = time.time() - 60
    newest = time.time()
    os.utime(events_path("aaaaaaaaaaaa"), (old, old))
    os.utime(events_path("bbbbbbbbbbbb"), (newer, newer))
    os.utime(events_path("cccccccccccc"), (newest, newest))
    assert SessionSnapshot.find_most_recent() == "cccccccccccc"


def test_find_most_recent_ignores_sessions_with_no_events_jsonl(tmp_swarm_root):
    from swarmd.lib.paths import ensure_session_dirs, events_path

    ensure_session_dirs("aaaaaaaaaaaa")
    ensure_session_dirs("bbbbbbbbbbbb")
    events_path("bbbbbbbbbbbb").write_text("{}\n")
    # a has no events.jsonl
    from swarmd.lib.paths import events_path as _ep

    _ep("aaaaaaaaaaaa").unlink(missing_ok=True)
    assert SessionSnapshot.find_most_recent() == "bbbbbbbbbbbb"


def test_snapshot_load_agrees_with_verifier_status_when_all_pass(tmp_swarm_root, session_id):
    """SessionSnapshot.load() must use verifier_status.json as authoritative
    source for criteria counts when all criteria pass.

    Regression for Bug #2: statusline showed 2/4 while verifier_status.json
    had all 4 criteria passing. This test seeds a session with 4-criterion
    all_pass verifier state and asserts the snapshot reflects it correctly.
    """
    from pathlib import Path
    import shutil
    from swarmd.lib.paths import session_dir

    evidence_dir = Path(
        "/Users/npow/.swarm/state/324da372-5986-4724-9a3d-af6b06d175f8"
    )
    sdir = session_dir(session_id)
    for fname in ("events.jsonl", "findings.jsonl", "interventions.jsonl"):
        src = evidence_dir / fname
        if src.exists():
            shutil.copy(src, sdir / fname)

    # Write a verifier_status.json that says all 4 criteria pass — the
    # scenario the bug report describes: verifier says all_pass but statusline
    # showed a lower count.
    all_pass_verifier = {
        "ts": 1776549200.0,
        "all_pass": True,
        "all_pass_since": 1776549100.0,
        "per_criterion": {
            "pytest_passes":    {"status": "pass", "exit_code": 0},
            "behavior_correct": {"status": "pass", "exit_code": 0},
            "test_count_floor": {"status": "pass", "exit_code": 0},
            "no_stubs":         {"status": "pass", "exit_code": 0},
        },
    }
    (sdir / "verifier_status.json").write_text(json.dumps(all_pass_verifier))

    snap = SessionSnapshot.load(session_id)

    assert len(snap.criteria) == 4, (
        f"expected 4 criteria, got {len(snap.criteria)}: "
        f"{[c.id for c in snap.criteria]}"
    )
    failing = [c for c in snap.criteria if c.status != "pass"]
    assert failing == [], f"expected all pass, but these failed: {failing}"
    assert snap.all_pass is True
