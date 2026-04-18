"""Tests for schema validation."""

from __future__ import annotations

import pytest

from swarm.schemas.event import Event
from swarm.schemas.finding import Evidence, Finding
from swarm.schemas.intervention import Intervention
from swarm.schemas.mission import Mission


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
