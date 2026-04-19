"""Tests for swarm.lib.criteria_templates — task-type detection and criteria generation."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_spec(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "spec.md"
    p.write_text(content)
    return p


def _dummy_paths(tmp_path: Path):
    """Return (workspace, spec, design, plan) Paths pointing into tmp_path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = tmp_path / "spec.md"
    spec.write_text("spec content")
    design = tmp_path / "design.md"
    design.write_text("design content")
    plan = tmp_path / "plan.md"
    plan.write_text("plan content")
    return workspace, spec, design, plan


# ---------------------------------------------------------------------------
# detect_task_type tests
# ---------------------------------------------------------------------------


def test_detect_code_from_spec(tmp_path):
    from swarmd.lib.criteria_templates import detect_task_type

    spec = _write_spec(
        tmp_path,
        "implement the feature using pytest\n"
        "put the module at src/foo.py and tests in tests/",
    )
    assert detect_task_type(spec) == "code"


def test_detect_doc_from_spec(tmp_path):
    from swarmd.lib.criteria_templates import detect_task_type

    spec = _write_spec(
        tmp_path,
        "Write a design document.\n\n"
        "## Architecture\n\nDescribe the system architecture.\n\n"
        "## Trade-offs\n\nList the trade-offs.\n",
    )
    assert detect_task_type(spec) == "doc"


def test_detect_research_from_spec(tmp_path):
    from swarmd.lib.criteria_templates import detect_task_type

    spec = _write_spec(
        tmp_path,
        "Research the landscape of vector databases.\n"
        "Include a citation for each tool. Survey existing literature.",
    )
    assert detect_task_type(spec) == "research"


def test_detect_skill_from_spec(tmp_path):
    from swarmd.lib.criteria_templates import detect_task_type

    spec = _write_spec(
        tmp_path,
        "Create a new skill for the slash command.\n"
        "Save the system instruction to .claude/skills/my-skill/SKILL.md\n"
        "## Skill\nDefine the agent persona here.",
    )
    assert detect_task_type(spec) == "skill"


def test_detect_exploration_from_spec(tmp_path):
    from swarmd.lib.criteria_templates import detect_task_type

    spec = _write_spec(
        tmp_path,
        "Explore the codebase to understand the module boundaries.\n"
        "Investigate how the scheduler works and discover its entry points.",
    )
    assert detect_task_type(spec) == "exploration"


# ---------------------------------------------------------------------------
# get_criteria tests
# ---------------------------------------------------------------------------


def test_get_criteria_code_returns_pytest_criterion(tmp_path):
    from swarmd.lib.criteria_templates import get_criteria

    workspace, spec, design, plan = _dummy_paths(tmp_path)
    criteria = get_criteria("code", workspace, spec=spec, design=design, plan=plan)
    checks = [c["check"] for c in criteria]
    assert any("pytest" in chk for chk in checks), f"no pytest in checks: {checks}"


def test_get_criteria_doc_references_deep_qa_cli(tmp_path):
    from swarmd.lib.criteria_templates import get_criteria

    workspace, spec, design, plan = _dummy_paths(tmp_path)
    criteria = get_criteria("doc", workspace, spec=spec, design=design, plan=plan)
    checks = [c["check"] for c in criteria]
    assert any("deep-qa-cli" in chk for chk in checks), (
        f"deep-qa-cli not found in doc criteria: {checks}"
    )


def test_all_criteria_have_required_fields(tmp_path):
    from swarmd.lib.criteria_templates import TaskType, get_criteria

    workspace, spec, design, plan = _dummy_paths(tmp_path)
    required = {"id", "description", "check", "timeout_sec", "idempotent"}
    task_types: list[TaskType] = ["code", "doc", "research", "exploration", "skill"]
    for tt in task_types:
        criteria = get_criteria(tt, workspace, spec=spec, design=design, plan=plan)
        assert len(criteria) > 0, f"no criteria returned for task_type={tt!r}"
        for c in criteria:
            missing = required - set(c.keys())
            assert not missing, (
                f"task_type={tt!r} criterion {c.get('id')!r} missing fields: {missing}"
            )


def test_check_commands_are_bash_syntactically_valid(tmp_path):
    """Every check string must parse cleanly under `bash -n`."""
    from swarmd.lib.criteria_templates import TaskType, get_criteria

    workspace, spec, design, plan = _dummy_paths(tmp_path)
    task_types: list[TaskType] = ["code", "doc", "research", "exploration", "skill"]
    failures: list[tuple[str, str, str]] = []

    for tt in task_types:
        criteria = get_criteria(tt, workspace, spec=spec, design=design, plan=plan)
        for c in criteria:
            check_cmd = c["check"]
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False
            ) as fh:
                fh.write(check_cmd + "\n")
                fpath = fh.name
            result = subprocess.run(
                ["bash", "-n", fpath],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                failures.append((tt, c["id"], result.stderr.strip()))

    assert not failures, (
        "bash -n syntax failures:\n"
        + "\n".join(f"  [{tt}] {cid}: {err}" for tt, cid, err in failures)
    )
