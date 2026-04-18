"""Tests for assertion_count_floor and allowed_deps invariants."""

from __future__ import annotations

from swarm.schemas.mission import Mission
from swarm.specialists.success_verifier import enforce_invariants


def _mission(tmp_path, **invariants):
    return Mission.model_validate(
        {
            "mission": "t",
            "workspace": str(tmp_path),
            "success_criteria": [
                {"id": "a", "description": "", "check": "true"}
            ],
            "invariants": invariants,
        }
    )


def test_assertion_count_floor_triggers(tmp_path):
    f = tmp_path / "tests" / "test_x.py"
    f.parent.mkdir()
    f.write_text(
        "def test_one():\n"
        "    pass  # no assertion!\n"
    )
    m = _mission(tmp_path, assertion_count_floor={"tests/test_x.py": 5})
    findings = enforce_invariants(m)
    matches = [
        x
        for x in findings
        if x.subtype == "scope_reduction" and any("test_x.py" in p for p in x.evidence.files)
    ]
    assert len(matches) == 1
    assert "assertion count" in matches[0].verdict


def test_assertion_count_floor_passes(tmp_path):
    f = tmp_path / "tests" / "test_x.py"
    f.parent.mkdir()
    f.write_text(
        "def test_a():\n"
        "    assert 1 == 1\n"
        "    assert 2 == 2\n"
        "def test_b():\n"
        "    assert True\n"
    )
    m = _mission(tmp_path, assertion_count_floor={"tests/test_x.py": 3})
    findings = enforce_invariants(m)
    matches = [
        x
        for x in findings
        if x.subtype == "scope_reduction" and any("test_x.py" in p for p in x.evidence.files)
    ]
    assert matches == []


def test_assertion_count_floor_counts_self_assert(tmp_path):
    f = tmp_path / "tests" / "test_x.py"
    f.parent.mkdir()
    f.write_text(
        "import unittest\n"
        "class T(unittest.TestCase):\n"
        "    def test_a(self):\n"
        "        self.assertEqual(1, 1)\n"
        "        self.assertTrue(True)\n"
    )
    m = _mission(tmp_path, assertion_count_floor={"tests/test_x.py": 2})
    findings = [
        x
        for x in enforce_invariants(m)
        if x.subtype == "scope_reduction" and any("test_x.py" in p for p in x.evidence.files)
    ]
    assert findings == []


def test_assertion_count_floor_missing_file(tmp_path):
    m = _mission(tmp_path, assertion_count_floor={"tests/missing.py": 3})
    findings = [
        x
        for x in enforce_invariants(m)
        if any("missing.py" in p for p in x.evidence.files)
    ]
    assert len(findings) == 1
    assert "missing" in findings[0].verdict.lower()
