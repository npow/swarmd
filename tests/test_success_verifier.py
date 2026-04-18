"""Tests for success_verifier."""

from __future__ import annotations

from pathlib import Path

from swarm.schemas.mission import Mission, SuccessCriterion
from swarm.specialists.success_verifier import (
    enforce_invariants,
    run_all_checks,
    run_check,
)


def _mission(tmp_path: Path, **overrides) -> Mission:
    data = {
        "mission": "t",
        "workspace": str(tmp_path),
        "success_criteria": [
            {"id": "ok", "description": "always ok", "check": "true"},
        ],
    }
    data.update(overrides)
    return Mission.model_validate(data)


def test_run_check_pass(tmp_path):
    c = SuccessCriterion(id="ok", description="", check="true")
    r = run_check(c, str(tmp_path))
    assert r.status == "pass"
    assert r.exit_code == 0


def test_run_check_fail(tmp_path):
    c = SuccessCriterion(id="no", description="", check="false")
    r = run_check(c, str(tmp_path))
    assert r.status == "fail"
    assert r.exit_code != 0


def test_run_check_timeout(tmp_path):
    c = SuccessCriterion(id="slow", description="", check="sleep 5", timeout_sec=1)
    r = run_check(c, str(tmp_path))
    assert r.status == "fail"
    assert "TIMEOUT" in r.stderr


def test_run_check_clean_env(tmp_path):
    # Check should NOT see caller env vars
    c = SuccessCriterion(
        id="env_check",
        description="",
        check="test -z \"$SWARM_SECRET\"",
    )
    import os

    os.environ["SWARM_SECRET"] = "should_not_leak"
    try:
        r = run_check(c, str(tmp_path))
        assert r.status == "pass", f"env leaked: stdout={r.stdout}"
    finally:
        del os.environ["SWARM_SECRET"]


def test_run_all_checks_multi(tmp_path):
    m = Mission.model_validate(
        {
            "mission": "t",
            "workspace": str(tmp_path),
            "success_criteria": [
                {"id": "a", "description": "", "check": "true"},
                {"id": "b", "description": "", "check": "false"},
            ],
        }
    )
    results = run_all_checks("sid", m)
    assert results["a"].status == "pass"
    assert results["b"].status == "fail"


def test_enforce_invariants_no_mock_detects(tmp_path):
    tests = tmp_path / "app" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_x.py").write_text("from unittest.mock import Mock\n")
    m = Mission.model_validate(
        {
            "mission": "t",
            "workspace": str(tmp_path),
            "success_criteria": [
                {"id": "a", "description": "", "check": "true"},
            ],
            "invariants": {"no_mock": ["app/tests"]},
        }
    )
    findings = enforce_invariants(m)
    assert any(f.subtype == "mock_out" for f in findings)


def test_enforce_invariants_no_mock_clean(tmp_path):
    tests = tmp_path / "app" / "tests"
    tests.mkdir(parents=True)
    (tests / "test_x.py").write_text("def test_one():\n    assert 1 == 1\n")
    m = Mission.model_validate(
        {
            "mission": "t",
            "workspace": str(tmp_path),
            "success_criteria": [
                {"id": "a", "description": "", "check": "true"},
            ],
            "invariants": {"no_mock": ["app/tests"]},
        }
    )
    assert [f for f in enforce_invariants(m) if f.subtype == "mock_out"] == []


def test_enforce_invariants_test_count_floor_triggers(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("def test_one():\n    assert True\n")
    m = Mission.model_validate(
        {
            "mission": "t",
            "workspace": str(tmp_path),
            "success_criteria": [
                {"id": "a", "description": "", "check": "true"},
            ],
            "invariants": {"test_count_floor": 5},
        }
    )
    findings = enforce_invariants(m)
    assert any(f.subtype == "scope_reduction" for f in findings)
