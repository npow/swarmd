"""Tests for the statusline one-liner formatter."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


from swarmd.lib.status import (
    CriterionStatus,
    SessionSnapshot,
    SpecialistHealth,
)
from swarmd.statusline import format_line


def _snap(**overrides) -> SessionSnapshot:
    defaults = dict(
        session_id="abc12345deadbeef",
        mission_title="Build fizzbuzz lib",
        workspace="/tmp/w",
        launcher_alive=True,
        started_at=0.0,
        duration_sec=32 * 60,  # 32 min
        iter_count=47,
        criteria=[
            CriterionStatus(id="pytest_passes", status="pass", exit_code=0, last_check_ts=1.0),
            CriterionStatus(id="test_count", status="pass", exit_code=0, last_check_ts=1.0),
            CriterionStatus(id="no_mocks", status="fail", exit_code=1, last_check_ts=1.0),
        ],
        all_pass=False,
        hold_sec=0.0,
        hold_target_sec=300.0,
        findings_total=4,
        findings_critical=1,
        findings_major=2,
        recent_findings=[],
        interventions_total=3,
        interventions_pending_ack=1,
        recent_interventions=[],
        health=[
            SpecialistHealth(
                name="coordinator", pid=1, last_beat_age_sec=1.0,
                is_stale=False, cycles=10,
            )
        ],
        events_per_minute=4.2,
        events_total=147,
    )
    defaults.update(overrides)
    return SessionSnapshot(**defaults)


def test_format_line_partial_pass_shape():
    line = format_line(_snap())
    assert "swarm abc12345" in line
    assert "2/3" in line
    assert "iter 47" in line
    assert "32m" in line  # duration
    assert "pending ack" in line  # 1 pending
    # A single line, no newline in middle
    assert "\n" not in line


def test_format_line_all_pass_within_hold():
    snap = _snap(
        criteria=[
            CriterionStatus(id="a", status="pass", exit_code=0, last_check_ts=1.0),
            CriterionStatus(id="b", status="pass", exit_code=0, last_check_ts=1.0),
        ],
        all_pass=True,
        hold_sec=47.0,
        hold_target_sec=300.0,
        interventions_pending_ack=0,
    )
    line = format_line(snap)
    assert "2/2" in line
    assert "hold" in line  # hold progress shown
    assert "0:47" in line
    assert "pending ack" not in line


def test_format_line_all_pass_hold_complete():
    snap = _snap(
        criteria=[
            CriterionStatus(id="a", status="pass", exit_code=0, last_check_ts=1.0),
        ],
        all_pass=True,
        hold_sec=350.0,
        hold_target_sec=300.0,
        interventions_pending_ack=0,
    )
    line = format_line(snap)
    assert "MISSION" in line  # completion marker
    # No "hold" progress shown once done
    assert "hold 5:50" not in line


def test_format_line_post_mortem_prefix():
    snap = _snap(launcher_alive=False)
    line = format_line(snap)
    assert line.startswith("[ended]")


def test_format_line_capped_at_200_chars():
    long_title = "x" * 500
    snap = _snap(mission_title=long_title)
    line = format_line(snap)
    assert len(line) <= 200


def test_format_line_empty_when_snapshot_has_no_mission_and_no_criteria():
    """Truly empty session (fresh mkdir, no data) — return empty string so
    Claude Code shows a blank statusline instead of noise."""
    empty = _snap(
        mission_title="",
        workspace="",
        launcher_alive=False,
        criteria=[],
        interventions_total=0,
        interventions_pending_ack=0,
        findings_total=0,
        health=[],
    )
    assert format_line(empty) == ""


# ---------------------------------------------------------------------------
# Task 10: wrapper subprocess tests
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]
_WRAPPER = _REPO / "swarm" / "swarm-statusline"


def test_wrapper_exists_and_is_executable():
    assert _WRAPPER.exists()
    assert os.access(_WRAPPER, os.X_OK), f"not executable: {_WRAPPER}"


def test_wrapper_auto_mode_with_no_sessions(tmp_swarm_root):
    env = os.environ.copy()
    env["SWARM_ROOT"] = str(tmp_swarm_root)
    r = subprocess.run(
        [str(_WRAPPER), "--auto"],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_wrapper_auto_mode_emits_line_for_session(tmp_swarm_root, session_id, tmp_path):
    """Given a session with a mission, --auto finds and formats it."""
    import yaml

    from swarmd.lib.paths import ensure_session_dirs, events_path, mission_yaml_path

    ws = tmp_path / "ws"
    ws.mkdir()
    ensure_session_dirs(session_id)
    mission_yaml_path(session_id).parent.mkdir(parents=True, exist_ok=True)
    mission_yaml_path(session_id).write_text(
        yaml.safe_dump(
            {
                "mission": "Sample mission",
                "workspace": str(ws),
                "success_criteria": [{"id": "ok", "description": "", "check": "true"}],
            }
        )
    )
    events_path(session_id).write_text('{"x":1}\n')

    env = os.environ.copy()
    env["SWARM_ROOT"] = str(tmp_swarm_root)
    r = subprocess.run(
        [str(_WRAPPER), "--auto"],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    assert r.returncode == 0
    assert "swarm" in r.stdout
    assert session_id[:8] in r.stdout


def test_wrapper_json_mode_emits_parseable_json(tmp_swarm_root, session_id):
    env = os.environ.copy()
    env["SWARM_ROOT"] = str(tmp_swarm_root)
    r = subprocess.run(
        [str(_WRAPPER), session_id, "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert data["session_id"] == session_id


def test_wrapper_json_mode_valid_when_beat_has_no_timestamp(tmp_swarm_root, session_id):
    """A .beat file with no last_cycle_ts yields last_beat_age_sec=math.inf.
    The --json output must still be valid RFC 8259 JSON (no bare Infinity)."""
    from swarmd.lib.paths import health_beat_path

    # Write a beat file with no timestamp — triggers math.inf path in _load_health
    beat = health_beat_path(session_id, "coordinator")
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text(json.dumps({"pid": 1234, "cycles_completed": 5}))

    env = os.environ.copy()
    env["SWARM_ROOT"] = str(tmp_swarm_root)
    r = subprocess.run(
        [str(_WRAPPER), session_id, "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    assert r.returncode == 0, f"stderr: {r.stderr}"
    # Must parse without error — bare Infinity would raise json.JSONDecodeError
    data = json.loads(r.stdout)
    assert data["session_id"] == session_id
    # last_beat_age_sec should be None (sanitized from math.inf)
    health_entries = data["health"]
    assert len(health_entries) == 1
    assert health_entries[0]["last_beat_age_sec"] is None


def test_statusline_accepts_session_arg(tmp_swarm_root, session_id, tmp_path):
    """--session <sid> and bare positional <sid> both produce non-empty output.

    Regression for Bug #3: main(["--session", sid]) was treating "--session"
    as the session_id, causing SessionSnapshot.load() to raise ValueError and
    silently print an empty line.
    """
    import io
    import unittest.mock as mock
    import yaml

    from swarmd.lib.paths import (
        _reset_for_tests,
        ensure_session_dirs,
        events_path,
        mission_yaml_path,
        session_dir,
    )
    from swarmd.statusline import main

    ws = tmp_path / "ws"
    ws.mkdir()
    ensure_session_dirs(session_id)
    mission_yaml_path(session_id).parent.mkdir(parents=True, exist_ok=True)
    mission_yaml_path(session_id).write_text(
        yaml.safe_dump({
            "mission": "Test mission",
            "workspace": str(ws),
            "success_criteria": [{"id": "ok", "description": "", "check": "true"}],
        })
    )
    events_path(session_id).write_text('{"x":1}\n')
    (session_dir(session_id) / "verifier_status.json").write_text(
        '{"ts":1.0,"all_pass":false,"per_criterion":{"ok":{"status":"pass","exit_code":0}}}'
    )

    with mock.patch.dict(os.environ, {"SWARM_ROOT": str(tmp_swarm_root)}):
        _reset_for_tests()

        # Positional form: main([sid]) — should already work
        captured = io.StringIO()
        with mock.patch("sys.stdout", captured):
            main([session_id])
        out_positional = captured.getvalue().strip()
        assert out_positional != "", f"positional form returned empty for sid={session_id}"
        assert session_id[:8] in out_positional, f"sid prefix not in output: {out_positional!r}"

        # --session form: main(["--session", sid]) — this was broken
        captured2 = io.StringIO()
        with mock.patch("sys.stdout", captured2):
            main(["--session", session_id])
        out_session = captured2.getvalue().strip()
        assert out_session != "", f"--session form returned empty for sid={session_id}"
        assert session_id[:8] in out_session, f"sid prefix not in --session output: {out_session!r}"
