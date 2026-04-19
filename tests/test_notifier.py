"""Unit tests for swarm.specialists.notifier."""

from __future__ import annotations

import json
import time

import pytest

from swarmd.lib.locking import write_line
from swarmd.lib.paths import findings_path, interventions_path, session_dir
from swarmd.schemas.finding import Finding
from swarmd.schemas.intervention import Intervention
from swarmd.specialists.notifier import (
    NOTIFIER_CURSOR_FILENAME,
    format_notification,
    process_new_findings,
    process_new_interventions,
    should_notify_finding,
    should_notify_intervention,
)


def _f(sid: str, severity="major", subtype="loop") -> Finding:
    return Finding(
        id=f"f-{time.time_ns()}-{subtype}",
        source="test",
        subject_session=sid,
        spawner_id=sid,
        type="loop",
        subtype=subtype,
        severity=severity,  # type: ignore[arg-type]
    )


def test_should_notify_critical_yes():
    f = _f("abc", severity="critical", subtype="mock_out")
    n, _title = should_notify_finding(f)
    assert n is True


def test_should_notify_tamper_yes():
    f = _f("abc", severity="major", subtype="tamper_detected")
    n, _ = should_notify_finding(f)
    assert n is True


def test_should_notify_hold_window_met_yes():
    f = _f("abc", severity="major", subtype="hold_window_met")
    n, _ = should_notify_finding(f)
    assert n is True


def test_should_notify_specialist_degraded_yes():
    f = _f("abc", severity="major", subtype="specialist_degraded")
    n, _ = should_notify_finding(f)
    assert n is True


def test_should_notify_info_no():
    f = _f("abc", severity="minor", subtype="random")
    n, _ = should_notify_finding(f)
    assert n is False


def test_should_notify_mission_complete_intervention_yes():
    iv = Intervention(
        id="i-1", tier="mission_complete", reason="done",
        consume_at="stop", requires_ack=True,
    )
    n, _ = should_notify_intervention(iv)
    assert n is True


def test_should_notify_info_intervention_no():
    iv = Intervention(
        id="i-2", tier="info", reason="fyi",
        consume_at="stop", requires_ack=True,
    )
    n, _ = should_notify_intervention(iv)
    assert n is False


def test_process_findings_advances_cursor(tmp_swarm_root, session_id):
    calls: list[tuple[str, str]] = []

    def fake_notify(title: str, body: str) -> bool:
        calls.append((title, body))
        return True

    fp = findings_path(session_id)
    write_line(fp, _f(session_id, "critical", "tamper_detected").model_dump_json())
    write_line(fp, _f(session_id, "major", "random").model_dump_json())  # ignored
    write_line(fp, _f(session_id, "critical", "mock_out").model_dump_json())

    process_new_findings(session_id, notify_fn=fake_notify)

    assert len(calls) == 2
    cursor_file = session_dir(session_id) / NOTIFIER_CURSOR_FILENAME
    assert cursor_file.exists()
    assert int(cursor_file.read_text().strip()) == fp.stat().st_size


def test_process_findings_resumes_from_cursor(tmp_swarm_root, session_id):
    calls: list[tuple[str, str]] = []

    def fake_notify(title: str, body: str) -> bool:
        calls.append((title, body))
        return True

    fp = findings_path(session_id)
    write_line(fp, _f(session_id, "critical", "tamper_detected").model_dump_json())
    process_new_findings(session_id, notify_fn=fake_notify)
    assert len(calls) == 1

    # Add more; cursor should resume, not re-fire
    write_line(fp, _f(session_id, "critical", "mock_out").model_dump_json())
    process_new_findings(session_id, notify_fn=fake_notify)
    assert len(calls) == 2  # only +1 new


def test_partial_line_does_not_advance_cursor(tmp_swarm_root, session_id):
    """If the last chunk ends without \\n, cursor must NOT advance past it.
    Next tick will see the full line and notify normally."""
    calls: list[tuple[str, str]] = []

    def fake_notify(title: str, body: str) -> bool:
        calls.append((title, body))
        return True

    fp = findings_path(session_id)
    # Manually write a well-formed line then a partial line (no trailing \n)
    full = _f(session_id, "critical", "tamper_detected").model_dump_json() + "\n"
    partial_prefix = _f(session_id, "critical", "mock_out").model_dump_json()[:40]
    fp.write_text(full + partial_prefix)

    process_new_findings(session_id, notify_fn=fake_notify)
    assert len(calls) == 1
    cursor_file = session_dir(session_id) / NOTIFIER_CURSOR_FILENAME
    assert int(cursor_file.read_text().strip()) == len(full)

    # Complete the second line + continue
    with fp.open("a") as f:
        f.write(
            _f(session_id, "critical", "mock_out").model_dump_json()[40:] + "\n"
        )
    process_new_findings(session_id, notify_fn=fake_notify)
    assert len(calls) == 2


def test_swarm_quiet_env_disables_notifications(tmp_swarm_root, session_id, monkeypatch):
    monkeypatch.setenv("SWARM_QUIET", "1")

    calls: list[tuple[str, str]] = []

    def fake_notify(title: str, body: str) -> bool:
        calls.append((title, body))
        return True

    fp = findings_path(session_id)
    write_line(fp, _f(session_id, "critical", "tamper_detected").model_dump_json())
    process_new_findings(session_id, notify_fn=fake_notify)

    assert calls == []  # notify_fn not called
    # Cursor still advances so re-enabling doesn't back-fill
    cursor_file = session_dir(session_id) / NOTIFIER_CURSOR_FILENAME
    assert cursor_file.exists()
    assert int(cursor_file.read_text().strip()) == fp.stat().st_size


def test_format_notification_truncates_body():
    f = _f("abc", "critical", "tamper_detected")
    f = f.model_copy(update={"verdict": "x" * 1000})
    title, body = format_notification(f)
    assert "swarm" in title
    assert "tamper_detected" in title
    assert len(body) <= 400


def test_malformed_finding_line_is_skipped(tmp_swarm_root, session_id):
    calls: list[tuple[str, str]] = []

    def fake_notify(title: str, body: str) -> bool:
        calls.append((title, body))
        return True

    fp = findings_path(session_id)
    write_line(fp, _f(session_id, "critical", "tamper_detected").model_dump_json())
    write_line(fp, "{not json at all")
    write_line(fp, _f(session_id, "critical", "mock_out").model_dump_json())
    process_new_findings(session_id, notify_fn=fake_notify)
    assert len(calls) == 2
