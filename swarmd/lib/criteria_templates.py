"""Task-type criteria template library for swarm mission.yaml generation.

Heuristically infers the artifact type a mission will produce and returns
a list of criterion dicts ready to drop into mission.yaml.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

TaskType = Literal["code", "doc", "research", "exploration", "skill"]

# ---------------------------------------------------------------------------
# Detection heuristic
# ---------------------------------------------------------------------------

# Each entry: (task_type, list_of_patterns).
# Each pattern contributes 1 point when found (case-insensitive) in the spec text.
# Ties broken by order of this list: code > doc > research > skill > exploration.
_SIGNALS: list[tuple[TaskType, list[str]]] = [
    (
        "code",
        [
            r"\bimplement\b",
            r"\bpytest\b",
            r"\btest\b",
            r"\brefactor\b",
            r"\bfunction\b",
            r"\bclass\b",
            r"\bmodule\b",
            r"\.py\b",
            r"\.ts\b",
            r"\.go\b",
            r"\.rs\b",
            r"\.java\b",
            r"\bsrc/",
            r"\btests/",
        ],
    ),
    (
        "doc",
        [
            r"\bdesign\b",
            r"\bspec\b",
            r"\bRFC\b",
            r"\barchitecture\b",
            r"\btrade-off\b",
            r"\bdecision\b",
            r"\binterface contract\b",
            r"^## Architecture",
        ],
    ),
    (
        "research",
        [
            r"\bresearch\b",
            r"\blandscape\b",
            r"\bliterature\b",
            r"\bstate of the art\b",
            r"\bcitation\b",
            r"\bsurvey\b",
            r"\bbrief\b",
        ],
    ),
    (
        "skill",
        [
            r"\bskill\b",
            r"\bprompt\b",
            r"\bsystem instruction\b",
            r"\bslash command\b",
            r"\bagent persona\b",
            r"\.claude/skills/",
            r"\bskills/",
        ],
    ),
    (
        "exploration",
        [
            r"\bexplore\b",
            r"\binvestigate\b",
            r"\bfind\b",
            r"\bunderstand\b",
            r"\bdiscover\b",
            r"\bmap the space\b",
        ],
    ),
]


def detect_task_type(spec_path: Path) -> TaskType:
    """Heuristic inference of the artifact type a mission will produce,
    based on keywords and structure in the spec/design file."""
    text = spec_path.read_text(errors="replace")
    scores: dict[TaskType, int] = {tt: 0 for tt, _ in _SIGNALS}
    for task_type, patterns in _SIGNALS:
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE | re.MULTILINE):
                scores[task_type] += 1

    # Ties broken by order: code > doc > research > skill > exploration
    order: list[TaskType] = ["code", "doc", "research", "skill", "exploration"]
    best_type = max(order, key=lambda t: (scores[t], -order.index(t)))
    return best_type


# ---------------------------------------------------------------------------
# Criteria templates
# ---------------------------------------------------------------------------


def get_criteria(
    task_type: TaskType,
    workspace: Path,
    *,
    spec: Path,
    design: Path,
    plan: Path,
) -> list[dict]:
    """Return a list of criterion dicts ready for mission.yaml.

    Each dict has: id, description, check, timeout_sec, idempotent.
    """
    if task_type == "code":
        return [
            {
                "id": "pytest_passes",
                "description": "All pytest tests pass",
                "check": f"cd {workspace} && python -m pytest tests/ -q",
                "timeout_sec": 180,
                "idempotent": True,
            },
            {
                "id": "test_count_floor",
                "description": "At least 5 test functions",
                "check": (
                    f"test $(grep -rc 'def test_' {workspace}/tests/ 2>/dev/null"
                    f" | awk -F: '{{s+=$2}} END {{print s+0}}') -ge 5"
                ),
                "timeout_sec": 30,
                "idempotent": True,
            },
            {
                "id": "no_stubs",
                "description": "No bare pass/TODO/NotImplementedError placeholders in src/",
                "check": (
                    f"! grep -rE '^[[:space:]]*(pass|TODO|NotImplementedError)'"
                    f" {workspace}/src/"
                ),
                "timeout_sec": 30,
                "idempotent": True,
            },
            {
                "id": "deep_qa_code",
                "description": "deep-qa finds no critical and <=2 major defects",
                "check": (
                    f"deep-qa-cli {workspace} --type=code --max-critical=0 --max-major=2"
                ),
                "timeout_sec": 300,
                "idempotent": True,
            },
        ]

    if task_type == "doc":
        return [
            {
                "id": "required_sections",
                "description": "Spec has required sections",
                "check": (
                    f"grep -q '^## Architecture' {spec}"
                    f" && grep -q '^## Trade-offs' {spec}"
                    f" && grep -q '^## Success Criteria' {spec}"
                ),
                "timeout_sec": 10,
                "idempotent": True,
            },
            {
                "id": "no_placeholders",
                "description": "No TBD/TODO/FIXME in the spec",
                "check": f"! grep -E 'TBD|TODO|FIXME' {spec}",
                "timeout_sec": 10,
                "idempotent": True,
            },
            {
                "id": "word_count_floor",
                "description": "Spec is substantive (>=500 words)",
                "check": f"test $(wc -w < {spec}) -ge 500",
                "timeout_sec": 10,
                "idempotent": True,
            },
            {
                "id": "deep_qa_doc",
                "description": "deep-qa finds no critical defects",
                "check": (
                    f"deep-qa-cli {spec} --type=doc --max-critical=0 --max-major=2"
                ),
                "timeout_sec": 300,
                "idempotent": True,
            },
        ]

    if task_type == "research":
        return [
            {
                "id": "dimension_coverage",
                "description": "Report covers >=5 dimensions (top-level sections)",
                "check": f"test $(grep -c '^## ' {spec}) -ge 5",
                "timeout_sec": 10,
                "idempotent": True,
            },
            {
                "id": "citation_count",
                "description": "At least 10 citations",
                "check": f"test $(grep -cE 'http[s]?://' {spec}) -ge 10",
                "timeout_sec": 10,
                "idempotent": True,
            },
            {
                "id": "deep_qa_research",
                "description": "deep-qa passes fact-check + no critical defects",
                "check": (
                    f"deep-qa-cli {spec} --type=research --max-critical=0 --max-major=3"
                ),
                "timeout_sec": 300,
                "idempotent": True,
            },
        ]

    if task_type == "exploration":
        return [
            {
                "id": "findings_count",
                "description": "At least 10 findings documented",
                "check": f"test $(grep -c '^- ' {spec}) -ge 10",
                "timeout_sec": 10,
                "idempotent": True,
            },
            {
                "id": "deep_qa_exploration",
                "description": "deep-qa passes",
                "check": (
                    f"deep-qa-cli {spec} --type=research --max-critical=0 --max-major=5"
                ),
                "timeout_sec": 300,
                "idempotent": True,
            },
        ]

    if task_type == "skill":
        return [
            {
                "id": "skill_yaml_valid",
                "description": "Skill frontmatter valid",
                "check": f"head -20 {spec} | grep -qE '^description: .+'",
                "timeout_sec": 10,
                "idempotent": True,
            },
            {
                "id": "deep_qa_skill",
                "description": "deep-qa finds no critical issues",
                "check": (
                    f"deep-qa-cli {spec} --type=skill --max-critical=0 --max-major=2"
                ),
                "timeout_sec": 300,
                "idempotent": True,
            },
        ]

    raise ValueError(f"Unknown task_type: {task_type!r}")
