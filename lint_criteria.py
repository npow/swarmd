#!/usr/bin/env python3
"""lint_criteria — flag trivially-satisfiable mission success criteria.

The swarm refuses to stop until ALL success_criteria hold continuously for
`hold_window_sec`. If criteria are trivially satisfiable (e.g. `test -f X`
passes with an empty file), the agent will "win" without doing the real
work and the session terminates prematurely. This linter catches the
common anti-patterns before a mission is launched.

Usage:
    python3 lint_criteria.py <mission.yaml>            # exit 1 on weak
    python3 lint_criteria.py --allow-weak <m.yaml>     # exit 0, warn only
    python3 lint_criteria.py --json <m.yaml>           # machine-readable

The linter is deliberately conservative: it flags obvious trivialities and
missing anti-cheat floors, but cannot detect semantically weak criteria
(e.g. "pytest passes" where the tests are trivial). That's what the
anticheat_critic_panel catches at runtime. Consider this a pre-flight
smoke check, not a full verifier.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

Severity = Literal["weak", "info"]

# Patterns that are satisfied by near-zero work. Match the whole check body
# (we strip whitespace first). These are conservative on purpose — a long
# pipeline that happens to start with `test -f foo` is NOT flagged; only a
# check whose entire body is the trivial pattern.
TRIVIAL_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"^test\s+-[fd]\s+\S+$",
        "bare file/dir existence check",
        "passes the moment an empty file/dir exists — add a content or behavior check",
    ),
    (
        r"^ls\s+\S+",
        "ls-only check",
        "passes even if the dir is empty — assert on contents or a command that exercises the dir",
    ),
    (
        r"^true$",
        "tautological true",
        "this check never fails — remove it or replace with a real outcome check",
    ),
    (
        r"^:$",
        "tautological colon",
        "`:` is a no-op — remove or replace with a real outcome check",
    ),
    (
        r"^echo\s+",
        "echo-only check",
        "echo never fails — remove or replace with a real outcome check",
    ),
]

# Patterns that silently neutralize an otherwise-real check.
POISON_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"\|\|\s*(true\b|:(?:\s|$))",
        "`|| true` or `|| :` suppresses failure",
        "remove the `|| true` — the check must be allowed to fail",
    ),
    (
        r"2>/dev/null\s*;\s*true\b",
        "stderr redirect then true",
        "this swallows errors — do not suppress",
    ),
]

# Markers that indicate at least one criterion enforces a real floor
# (test count, coverage, line count, negative grep for stubs/TODOs).
ANTI_CHEAT_MARKERS: list[str] = [
    r"-ge\s+\d+",
    r"-gt\s+\d+",
    r"--fail-under",
    r"--cov-fail-under",
    r"^\s*!\s*grep",              # leading negative grep
    r";\s*!\s*grep",              # chained negative grep
    r"&&\s*!\s*grep",             # negative grep after &&
    r"grep\s+-[a-zA-Z]*v\b",      # grep -v (excluded patterns)
    r"wc\s+-l.*-ge\s+\d+",
    r"mypy\s+--strict",
    r"coverage\s+report\s+--fail-under",
]

# Tokens that suggest the mission involves writing code or tests. If any
# criterion mentions one of these AND no anti-cheat marker is present, we
# flag the mission as under-specified.
CODE_TOKENS: list[str] = [
    r"\bpytest\b",
    r"\bunittest\b",
    r"\bcargo\s+test\b",
    r"\bgo\s+test\b",
    r"\bnpm\s+test\b",
    r"\bnpx\s+jest\b",
    r"\bjest\b",
    r"\bvitest\b",
    r"\bmocha\b",
    r"\brspec\b",
    r"\bphpunit\b",
]


@dataclass(frozen=True)
class Finding:
    criterion_id: str
    severity: Severity
    category: str
    reason: str
    suggestion: str

    def to_dict(self) -> dict:
        return asdict(self)


def _lint_one(cid: str, check: str) -> list[Finding]:
    out: list[Finding] = []
    stripped = check.strip()

    for pattern, category, suggestion in TRIVIAL_PATTERNS:
        if re.match(pattern, stripped):
            out.append(
                Finding(cid, "weak", category, f"check body matches {category!r}", suggestion)
            )
            break  # one trivial verdict per criterion is enough

    for pattern, category, suggestion in POISON_PATTERNS:
        if re.search(pattern, stripped):
            out.append(Finding(cid, "weak", category, f"contains {category!r}", suggestion))

    return out


def _has_anti_cheat(checks: list[str]) -> bool:
    joined = "\n".join(checks)
    return any(re.search(m, joined, re.MULTILINE) for m in ANTI_CHEAT_MARKERS)


def _mentions_code(checks: list[str]) -> bool:
    joined = "\n".join(checks)
    return any(re.search(m, joined) for m in CODE_TOKENS)


def _is_ceremonial(criterion: dict) -> bool:
    """A criterion explicitly marked as ceremonial / human-gated is exempt."""
    cid = str(criterion.get("id", "")).lower()
    desc = str(criterion.get("description", "")).lower()
    return (
        cid in {"user_says_done", "human_verifier"}
        or "ceremonial" in desc
        or "human verifier" in desc
    )


def lint_mission(mission: dict) -> list[Finding]:
    """Lint a parsed mission dict. Returns findings (may be empty)."""
    findings: list[Finding] = []
    criteria = mission.get("success_criteria") or []

    if not criteria:
        findings.append(
            Finding(
                "<mission>", "weak", "no_criteria",
                "mission has no success_criteria",
                "add at least one executable check whose exit code defines done",
            )
        )
        return findings

    non_ceremonial_checks: list[str] = []
    for c in criteria:
        cid = str(c.get("id") or "<unnamed>")
        check = str(c.get("check") or "")
        if _is_ceremonial(c):
            continue
        non_ceremonial_checks.append(check)
        findings.extend(_lint_one(cid, check))

    if (
        non_ceremonial_checks
        and _mentions_code(non_ceremonial_checks)
        and not _has_anti_cheat(non_ceremonial_checks)
    ):
        findings.append(
            Finding(
                "<mission>", "weak", "no_anti_cheat_floor",
                "code/tests referenced but no anti-cheat criterion present",
                "add one of: test-count floor (e.g. `... -ge 5`), coverage threshold "
                "(`--fail-under=80`), or negative grep (`! grep -rE 'pass|TODO'`)",
            )
        )

    return findings


def _render_text(findings: list[Finding]) -> str:
    if not findings:
        return "rigor OK — no weak criteria detected"
    lines = [f"WEAK CRITERIA DETECTED ({len(findings)}):"]
    for f in findings:
        lines.append(f"  [{f.severity}] {f.criterion_id} ({f.category}): {f.reason}")
        lines.append(f"      → {f.suggestion}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    allow_weak = False
    as_json = False
    paths: list[str] = []
    for a in argv[1:]:
        if a == "--allow-weak":
            allow_weak = True
        elif a == "--json":
            as_json = True
        elif a.startswith("-"):
            print(f"unknown flag: {a}", file=sys.stderr)
            return 2
        else:
            paths.append(a)

    if len(paths) != 1:
        print("usage: lint_criteria.py [--allow-weak] [--json] <mission.yaml>", file=sys.stderr)
        return 2

    try:
        import yaml
    except ImportError:
        print("lint_criteria: PyYAML is required", file=sys.stderr)
        return 2

    try:
        mission = yaml.safe_load(Path(paths[0]).read_text())
    except Exception as exc:
        print(f"failed to parse {paths[0]}: {exc}", file=sys.stderr)
        return 2

    findings = lint_mission(mission or {})

    if as_json:
        print(json.dumps([f.to_dict() for f in findings], indent=2))
    else:
        print(_render_text(findings))

    if not findings:
        return 0
    if allow_weak:
        return 0
    return 1 if any(f.severity == "weak" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
