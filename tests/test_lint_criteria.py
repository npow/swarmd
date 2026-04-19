"""Tests for swarm.lint_criteria — the pre-launch weak-criteria detector."""

from __future__ import annotations

from swarmd.lint_criteria import Finding, lint_mission


def _mission(**criteria_by_id) -> dict:
    """Build a mission dict from id=check kwargs for terse test fixtures."""
    crits = [{"id": cid, "check": body} for cid, body in criteria_by_id.items()]
    return {"success_criteria": crits}


def _categories(findings: list[Finding]) -> set[str]:
    return {f.category for f in findings}


def _ids(findings: list[Finding]) -> set[str]:
    return {f.criterion_id for f in findings}


# ---------- trivial checks ----------


def test_bare_test_f_is_weak():
    out = lint_mission(_mission(a="test -f /tmp/foo"))
    assert "bare file/dir existence check" in _categories(out)


def test_bare_test_d_is_weak():
    out = lint_mission(_mission(a="test -d tests/"))
    assert "bare file/dir existence check" in _categories(out)


def test_ls_only_is_weak():
    out = lint_mission(_mission(a="ls src/"))
    assert "ls-only check" in _categories(out)


def test_true_is_weak():
    out = lint_mission(_mission(a="true"))
    assert "tautological true" in _categories(out)


def test_colon_is_weak():
    out = lint_mission(_mission(a=":"))
    assert "tautological colon" in _categories(out)


def test_echo_only_is_weak():
    out = lint_mission(_mission(a="echo ok"))
    assert "echo-only check" in _categories(out)


def test_leading_whitespace_still_flagged():
    out = lint_mission(_mission(a="  test -f app.py  "))
    assert "bare file/dir existence check" in _categories(out)


# ---------- poison patterns ----------


def test_or_true_suppresses_failure():
    out = lint_mission(_mission(a="pytest tests/ || true"))
    cats = _categories(out)
    assert "`|| true` or `|| :` suppresses failure" in cats


def test_or_colon_suppresses_failure():
    out = lint_mission(_mission(a="pytest tests/ || :"))
    assert "`|| true` or `|| :` suppresses failure" in _categories(out)


def test_trailing_true_swallows_stderr():
    out = lint_mission(_mission(a="make 2>/dev/null ; true"))
    assert "stderr redirect then true" in _categories(out)


# ---------- anti-cheat floor required ----------


def test_pytest_without_floor_is_flagged():
    out = lint_mission(_mission(a="pytest tests/"))
    assert "no_anti_cheat_floor" in _categories(out)


def test_pytest_with_test_count_floor_passes():
    out = lint_mission(
        _mission(
            a="pytest tests/",
            b="test $(grep -rc 'def test_' tests/ | awk -F: '{s+=$2} END {print s}') -ge 5",
        )
    )
    assert "no_anti_cheat_floor" not in _categories(out)


def test_pytest_with_coverage_floor_passes():
    out = lint_mission(
        _mission(
            a="pytest tests/",
            b="coverage report --fail-under=80",
        )
    )
    assert "no_anti_cheat_floor" not in _categories(out)


def test_pytest_with_negative_grep_passes():
    out = lint_mission(
        _mission(
            a="pytest tests/",
            b="! grep -rE '^\\s*(pass|TODO)' src/",
        )
    )
    assert "no_anti_cheat_floor" not in _categories(out)


def test_non_code_mission_does_not_require_floor():
    """Pure infra mission (no code/tests) shouldn't trigger the floor rule."""
    out = lint_mission(
        _mission(
            a="curl -f http://localhost:8080/health",
            b="psql -c 'select 1'",
        )
    )
    assert "no_anti_cheat_floor" not in _categories(out)


def test_go_test_without_floor_is_flagged():
    out = lint_mission(_mission(a="go test ./..."))
    assert "no_anti_cheat_floor" in _categories(out)


def test_npm_test_without_floor_is_flagged():
    out = lint_mission(_mission(a="npm test"))
    assert "no_anti_cheat_floor" in _categories(out)


# ---------- ceremonial mode ----------


def test_user_says_done_is_ceremonial_and_exempt():
    mission = {
        "success_criteria": [
            {"id": "user_says_done", "check": "test -f /tmp/ws/.done"},
        ]
    }
    out = lint_mission(mission)
    assert out == []


def test_description_mentions_ceremonial_is_exempt():
    mission = {
        "success_criteria": [
            {
                "id": "marker",
                "description": "ceremonial marker; human verifier gates the swarm",
                "check": "test -f /tmp/ws/.done",
            },
        ]
    }
    out = lint_mission(mission)
    assert out == []


def test_ceremonial_exemption_only_for_that_criterion():
    """A ceremonial criterion shouldn't mask other weak criteria alongside it."""
    mission = {
        "success_criteria": [
            {"id": "user_says_done", "check": "test -f /tmp/ws/.done"},
            {"id": "real", "check": "test -f /tmp/app.py"},  # still weak
        ]
    }
    out = lint_mission(mission)
    assert "real" in _ids(out)
    assert "user_says_done" not in _ids(out)


# ---------- edge cases ----------


def test_empty_criteria_is_weak():
    out = lint_mission({"success_criteria": []})
    assert "no_criteria" in _categories(out)


def test_missing_criteria_key_is_weak():
    out = lint_mission({})
    assert "no_criteria" in _categories(out)


def test_strong_mission_passes_clean():
    """Realistic strong mission: pytest + test count + no stubs."""
    mission = {
        "success_criteria": [
            {"id": "pytest", "check": "python -m pytest tests/ -q"},
            {
                "id": "min_tests",
                "check": "test $(grep -rc 'def test_' tests/ | awk -F: '{s+=$2} END {print s+0}') -ge 5",
            },
            {
                "id": "no_stubs",
                "check": "! grep -rE '^[[:space:]]*(pass|TODO|NotImplementedError)' src/",
            },
        ]
    }
    assert lint_mission(mission) == []


def test_multiple_weak_criteria_all_reported():
    out = lint_mission(
        _mission(
            bare_f="test -f app.py",
            bare_d="test -d tests/",
            bare_echo="echo hi",
        )
    )
    assert {"bare_f", "bare_d", "bare_echo"}.issubset(_ids(out))


def test_long_pipeline_starting_with_test_f_is_NOT_flagged():
    """Non-trivial pipeline shouldn't match the bare-existence rule."""
    out = lint_mission(
        _mission(
            a="test -f app.py && python app.py --version | grep -q '1.0'",
            b="test $(grep -rc 'def test_' tests/) -ge 3 && pytest tests/",
        )
    )
    # No trivial or poison findings expected
    cats = _categories(out)
    assert "bare file/dir existence check" not in cats
    assert "no_anti_cheat_floor" not in cats


def test_json_output_serializable():
    """Findings must be JSON-serializable for --json mode."""
    import json as _json
    out = lint_mission(_mission(a="test -f x"))
    serialized = _json.dumps([f.to_dict() for f in out])
    assert "bare file/dir existence check" in serialized
