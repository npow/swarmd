"""Tests for schema validation."""

from __future__ import annotations

import pytest

from swarmd.schemas.event import Event
from swarmd.schemas.finding import Evidence, Finding
from swarmd.schemas.intervention import Intervention
from swarmd.schemas.mission import Mission


def test_event_roundtrip():
    ev = Event(
        id="e-1-abc",
        session_id="s",
        spawner_id="s",
        ts_monotonic=1.0,
        ts_wall="2026-04-16T00:00:00Z",
        hook="PostToolUse",
        tool_name="Edit",
    )
    data = ev.model_dump_json()
    ev2 = Event.model_validate_json(data)
    assert ev == ev2


def test_finding_roundtrip():
    f = Finding(
        id="f-1-abc",
        source="pattern_detector.loop",
        subject_session="s",
        spawner_id="s",
        type="loop",
        subtype="repeat_exact_args",
        severity="major",
        cited_events=["e-1-abc"],
        evidence=Evidence(files=["x.py"]),
    )
    data = f.model_dump_json()
    f2 = Finding.model_validate_json(data)
    assert f == f2


def test_intervention_roundtrip():
    iv = Intervention(id="i-1-abc", tier="correct", reason="do better")
    data = iv.model_dump_json()
    iv2 = Intervention.model_validate_json(data)
    assert iv == iv2


def test_mission_rejects_relative_workspace(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        Mission.model_validate(
            {
                "mission": "x",
                "workspace": "relative/path",
                "success_criteria": [
                    {"id": "a", "description": "b", "check": "true"}
                ],
            }
        )


def test_mission_requires_criteria(tmp_path):
    with pytest.raises(ValueError, match="at least one"):
        Mission.model_validate(
            {
                "mission": "x",
                "workspace": str(tmp_path),
                "success_criteria": [],
            }
        )


def test_mission_rejects_duplicate_ids(tmp_path):
    with pytest.raises(ValueError, match="unique"):
        Mission.model_validate(
            {
                "mission": "x",
                "workspace": str(tmp_path),
                "success_criteria": [
                    {"id": "a", "description": "b", "check": "true"},
                    {"id": "a", "description": "c", "check": "true"},
                ],
            }
        )


def test_mission_defaults(sample_mission):
    assert sample_mission.verification.run_every_sec == 60
    assert sample_mission.concurrency.max_total_live == 16
    assert sample_mission.observer_config.pattern_thresholds.loop_repeat_count == 5


# ---------------------------------------------------------------------------
# Task 3 — mission schema extensions: max_duration_sec + observer_config
# cadences. These assert the new fields exist with spec-correct defaults, the
# pre-existing fields still work (backward compat), and user-provided values
# round-trip through pydantic validation.
# ---------------------------------------------------------------------------


def test_mission_max_duration_sec_default(tmp_path):
    """Mission.max_duration_sec defaults to 14400 (4 hours) per plan Task 3."""
    from swarmd.schemas.mission import Mission

    m = Mission(
        mission="x",
        workspace=str(tmp_path),
        success_criteria=[
            {"id": "a", "description": "b", "check": "true"},  # type: ignore[list-item]
        ],
    )
    assert m.max_duration_sec == 14400


def test_mission_max_duration_sec_user_value(tmp_path):
    """User-provided max_duration_sec is preserved."""
    from swarmd.schemas.mission import Mission

    m = Mission(
        mission="x",
        workspace=str(tmp_path),
        success_criteria=[
            {"id": "a", "description": "b", "check": "true"},  # type: ignore[list-item]
        ],
        max_duration_sec=3600,
    )
    assert m.max_duration_sec == 3600


def test_observer_config_new_cadence_fields_accepted():
    """Spec §6.2 observer_config: pattern_detector_sec, llm_critic_sec,
    resource_monitor_sec are accepted and preserved."""
    from swarmd.schemas.mission import ObserverConfig

    oc = ObserverConfig(
        pattern_detector_sec=15,
        llm_critic_sec=60,
        resource_monitor_sec=20,
    )
    assert oc.pattern_detector_sec == 15
    assert oc.llm_critic_sec == 60
    assert oc.resource_monitor_sec == 20


def test_observer_config_defaults_for_new_fields():
    """Plan Task 3 defaults for the three new cadences."""
    from swarmd.schemas.mission import ObserverConfig

    oc = ObserverConfig()
    assert oc.pattern_detector_sec == 10
    assert oc.llm_critic_sec == 120
    assert oc.resource_monitor_sec == 30


def test_observer_config_backward_compat_existing_fields():
    """Pre-existing ObserverConfig fields must still work (no break)."""
    from swarmd.schemas.mission import ObserverConfig, PatternThresholds

    oc = ObserverConfig(
        plan_checkpoint_every_sec=123,
        goal_drift_cadence_sec=77,
        progress_audit_cadence_sec=88,
        pattern_thresholds=PatternThresholds(loop_repeat_count=9),
    )
    assert oc.plan_checkpoint_every_sec == 123
    assert oc.goal_drift_cadence_sec == 77
    assert oc.progress_audit_cadence_sec == 88
    assert oc.pattern_thresholds.loop_repeat_count == 9


def test_mission_yaml_style_dict_with_new_fields(tmp_path):
    """A mission.yaml-style dict with the new fields round-trips through
    Mission.model_validate without losing existing validators."""
    from swarmd.schemas.mission import Mission

    m = Mission.model_validate(
        {
            "mission": "x",
            "workspace": str(tmp_path),
            "success_criteria": [
                {"id": "a", "description": "b", "check": "true"},
            ],
            "max_duration_sec": 600,
            "observer_config": {
                "pattern_detector_sec": 5,
                "llm_critic_sec": 90,
                "resource_monitor_sec": 25,
                # pre-existing keys on the same dict, still accepted:
                "progress_audit_cadence_sec": 30,
            },
        }
    )
    assert m.max_duration_sec == 600
    assert m.observer_config.pattern_detector_sec == 5
    assert m.observer_config.llm_critic_sec == 90
    assert m.observer_config.resource_monitor_sec == 25
    assert m.observer_config.progress_audit_cadence_sec == 30
