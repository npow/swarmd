"""Contract tests for SessionSnapshot — invariants any backend must satisfy.

When a Temporal-backed `SessionSnapshot.load()` is added (SWARM_BACKEND=temporal
branch), parametrize this file to run against both backends. Until then, the
file-backed implementation is the only subject."""

from __future__ import annotations

import time

from swarm.lib.status import SessionSnapshot


def test_load_is_total_never_raises_for_valid_session_id(tmp_swarm_root, session_id):
    """load() must return a value for any valid session_id, even one with
    zero state files populated."""
    snap = SessionSnapshot.load(session_id)
    assert isinstance(snap, SessionSnapshot)
    assert snap.session_id == session_id


def test_load_is_idempotent(tmp_swarm_root, session_id):
    """Two back-to-back loads with no state change produce the same snapshot
    (modulo fields dependent on wall clock — we exclude those)."""
    a = SessionSnapshot.load(session_id)
    b = SessionSnapshot.load(session_id)
    # Wall-clock-free fields must match exactly
    for field_name in (
        "session_id", "mission_title", "workspace", "launcher_alive",
        "iter_count", "criteria", "all_pass", "hold_target_sec",
        "findings_total", "findings_critical", "findings_major",
        "interventions_total", "interventions_pending_ack",
        "events_total",
    ):
        assert getattr(a, field_name) == getattr(b, field_name), (
            f"field {field_name} not idempotent across loads"
        )


def test_load_monotonic_duration(tmp_swarm_root, session_id):
    """duration_sec never decreases across back-to-back loads."""
    a = SessionSnapshot.load(session_id)
    time.sleep(0.05)
    b = SessionSnapshot.load(session_id)
    assert b.duration_sec >= a.duration_sec


def test_all_pass_implies_empty_fail_criteria(tmp_swarm_root, session_id):
    """If all_pass is True, no criterion has status 'fail'."""
    snap = SessionSnapshot.load(session_id)
    if snap.all_pass:
        for c in snap.criteria:
            assert c.status != "fail"


def test_hold_sec_nonnegative(tmp_swarm_root, session_id):
    """hold_sec is never negative."""
    snap = SessionSnapshot.load(session_id)
    assert snap.hold_sec >= 0.0


def test_findings_counts_consistent(tmp_swarm_root, session_id):
    """critical + major <= total, and each count >= 0."""
    snap = SessionSnapshot.load(session_id)
    assert snap.findings_total >= 0
    assert snap.findings_critical >= 0
    assert snap.findings_major >= 0
    assert snap.findings_critical + snap.findings_major <= snap.findings_total


def test_interventions_pending_le_total(tmp_swarm_root, session_id):
    snap = SessionSnapshot.load(session_id)
    assert 0 <= snap.interventions_pending_ack <= snap.interventions_total


def test_health_is_stale_matches_age(tmp_swarm_root, session_id):
    """For every health entry, is_stale is True iff last_beat_age_sec > STALE_SEC."""
    from swarm.lib.status import STALE_SEC

    snap = SessionSnapshot.load(session_id)
    for h in snap.health:
        assert h.is_stale == (h.last_beat_age_sec > STALE_SEC)
