"""``enforce_invariants`` Temporal activity — run the mission invariants.

Per spec §6.3 row ``enforce_invariants``:

    enforce_invariants(workspace, invariants) → {findings: list[dict]}

    Short (seconds). Observational; safe to re-run.

Ported from ``specialists/success_verifier.py::enforce_invariants`` (lines
167-318). Semantics preserved per invariant:

* ``no_mock`` — a list of directory paths (relative to workspace) under which
  any ``unittest.mock`` / ``mock.patch`` / ``MagicMock`` / ``Mock(`` reference
  in a ``*.py`` file produces one finding per tainted file.
* ``test_count_floor`` — an int. Count ``def test_...`` across every
  ``test_*.py`` in the workspace; if total < floor, one finding.
* ``assertion_count_floor`` — a dict mapping relative file path → per-file
  minimum number of ``assert`` statements (including ``self.assert*(``
  method calls, the two conventions used in Python test files). One finding
  per file whose count is below its floor. A missing file is also a finding.
* ``allowed_deps`` — a list of allowlisted package names (optionally with
  version constraints, which are stripped). Installed pip packages must be
  a subset; one finding per disallowed installed package.

Implementation contract (from plan Task 6):

* Signature: ``async def enforce_invariants(workspace, invariants)
  -> InvariantsResult``.
* Pure, stdlib + pydantic. No Temporal primitives beyond ``@activity.defn``.
* Returns an ``InvariantsResult`` dataclass. Never raises on normal check
  paths; unexpected I/O errors on individual files are swallowed (the file
  is skipped) so one unreadable path cannot sink the whole sweep.
* Each finding is a spec §6.3-shaped dict: ``type=meta``,
  ``subtype=invariant_<name>``, ``severity=critical``, ``verdict=<details>``.
  The workflow layer decides whether to emit to ``findings.jsonl``, pause
  the mission, etc.
* Idempotent: same workspace state + same invariants → same findings.
* ``_pip_freeze`` is factored out as a module-level function so tests can
  patch it without spawning a real subprocess.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from temporalio import activity

from swarm.schemas.mission import Invariants

# Regex for detecting mock usage. Mirrors the success_verifier pattern so the
# migration preserves behavior exactly. Covers:
#   * ``unittest.mock`` (attribute or ``from unittest.mock import ...``)
#   * ``MagicMock`` class reference
#   * ``Mock(`` constructor call
#   * ``patch(`` decorator/context call (the common mock.patch entry point)
_MOCK_RE = re.compile(r"\b(unittest\.mock|MagicMock|Mock\(|patch\()")

# Per-file assertion patterns. Uses two independent counts:
#   * line-leading ``assert`` keyword (pytest-style)
#   * ``self.assert*(`` method calls (unittest-style)
_ASSERT_KEYWORD_RE = re.compile(r"^\s*assert\b", flags=re.MULTILINE)
_ASSERT_METHOD_RE = re.compile(r"\bself\.assert\w+\s*\(")

# Test-function pattern. Only count top-of-line ``def test_...`` to avoid
# picking up nested helper defs named ``test_*`` or strings that happen to
# contain the substring.
_TEST_DEF_RE = re.compile(r"^\s*def\s+test_\w+\(", flags=re.MULTILINE)


@dataclass
class InvariantsResult:
    """Outcome of one invariants sweep.

    ``findings`` is a list (possibly empty) of spec-shaped dicts. Empty list
    means all configured invariants passed. Absent invariants (None/empty)
    are silently skipped — not a violation.
    """

    findings: list[dict] = field(default_factory=list)


@activity.defn(name="enforce_invariants")
async def enforce_invariants(
    workspace: str, invariants: Invariants
) -> InvariantsResult:
    """Check ``invariants`` against ``workspace`` and return all findings.

    The activity never aborts early on a finding — all configured invariants
    are checked every run so the workflow sees the full picture. Order within
    the returned list is: no_mock → test_count_floor → assertion_count_floor →
    allowed_deps. The order is stable but should not be relied on by callers
    that route findings by subtype.
    """

    ws = Path(workspace)
    findings: list[dict] = []

    if invariants.no_mock:
        findings.extend(_check_no_mock(ws, invariants.no_mock))

    if invariants.test_count_floor is not None:
        findings.extend(_check_test_count_floor(ws, invariants.test_count_floor))

    if invariants.assertion_count_floor:
        findings.extend(
            _check_assertion_count_floor(ws, invariants.assertion_count_floor)
        )

    if invariants.allowed_deps:
        findings.extend(_check_allowed_deps(invariants.allowed_deps))

    return InvariantsResult(findings=findings)


def _finding(subtype: str, verdict: str) -> dict:
    """Build a spec §6.3-shaped finding dict.

    Factored so every check emits the same shape; a future schema tightening
    only has to edit this one spot.
    """
    return {
        "type": "meta",
        "subtype": f"invariant_{subtype}",
        "severity": "critical",
        "verdict": verdict,
    }


def _check_no_mock(workspace: Path, protected: list[str]) -> list[dict]:
    """One finding per ``*.py`` file under any protected directory that
    references ``unittest.mock`` / ``MagicMock`` / ``Mock(`` / ``patch(``.

    Unreadable files are skipped silently — a permission glitch on one file
    should not mask violations in the rest of the tree.
    """
    findings: list[dict] = []
    for rel in protected:
        root = (workspace / rel).resolve()
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            try:
                text = py.read_text(errors="ignore")
            except OSError:
                continue
            if _MOCK_RE.search(text):
                try:
                    rel_path = py.relative_to(workspace)
                except ValueError:
                    rel_path = py
                findings.append(
                    _finding(
                        "no_mock",
                        f"mock usage found in protected path: {rel_path}",
                    )
                )
    return findings


def _check_test_count_floor(workspace: Path, floor: int) -> list[dict]:
    """If the total ``def test_...`` count across every ``test_*.py`` file in
    the workspace is below ``floor``, return one finding.

    Counting at module-def granularity (not class-method) matches pytest's
    discovery model and the legacy ``success_verifier`` behavior.
    """
    count = 0
    for py in workspace.rglob("test_*.py"):
        try:
            text = py.read_text(errors="ignore")
        except OSError:
            continue
        count += len(_TEST_DEF_RE.findall(text))

    if count < floor:
        return [
            _finding(
                "test_count_floor",
                f"test count {count} dropped below floor {floor}",
            )
        ]
    return []


def _check_assertion_count_floor(
    workspace: Path, floors: dict[str, int]
) -> list[dict]:
    """Per-file assertion floor check.

    For each ``(rel_path, floor)`` entry:
    * if the file is missing → one finding (regression via deletion is a cheat).
    * else count ``assert`` keywords + ``self.assert*(`` method calls; if
      below floor → one finding.
    """
    findings: list[dict] = []
    for rel, floor in floors.items():
        full = (workspace / rel).resolve()
        if not full.exists():
            findings.append(
                _finding(
                    "assertion_count_floor",
                    f"{rel} missing — protected by assertion_count_floor",
                )
            )
            continue
        try:
            text = full.read_text(errors="ignore")
        except OSError:
            continue
        count = len(_ASSERT_KEYWORD_RE.findall(text)) + len(
            _ASSERT_METHOD_RE.findall(text)
        )
        if count < floor:
            findings.append(
                _finding(
                    "assertion_count_floor",
                    f"{rel}: assertion count {count} below floor {floor}",
                )
            )
    return findings


def _pip_freeze() -> list[str]:
    """Run ``python -m pip freeze`` and return the raw output lines.

    Factored so tests can mock the subprocess hop. Uses ``sys.executable`` so
    the check reflects the interpreter the worker is actually running under,
    not the first ``python3`` on PATH. A 10s timeout keeps a hung pip from
    stalling the activity; on timeout or non-zero exit we return ``[]`` which
    makes the check a no-op (fail-open on infra errors — the allowlist is an
    anti-cheat net, not the primary dep gate).
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.strip().splitlines()


def _check_allowed_deps(allowlist: list[str]) -> list[dict]:
    """One finding per installed pip package that is not in ``allowlist``.

    Parsing mirrors the legacy success_verifier:
    * Strip version constraints from allowlist entries (``pkg>=1.0`` → ``pkg``).
    * Normalize names to lowercase + ``-`` (so ``foo_bar`` and ``foo-bar``
      match).
    * Ignore blank lines, ``#`` comments, and ``name @ url`` VCS lines (those
      contain a space — pip's own heuristic).
    """
    allowed_names: set[str] = set()
    for spec in allowlist:
        bare = re.split(r"[<>=!~\[]", spec, 1)[0].strip().lower().replace("_", "-")
        if bare:
            allowed_names.add(bare)

    findings: list[dict] = []
    seen: set[str] = set()
    for raw in _pip_freeze():
        line = raw.strip()
        if not line or line.startswith("#") or " " in line:
            continue
        if "==" in line:
            name, _, version = line.partition("==")
        else:
            name, version = line, ""
        norm = name.strip().lower().replace("_", "-")
        if not norm or norm in seen:
            continue
        seen.add(norm)
        if norm not in allowed_names:
            detail = f"{norm}=={version}" if version else norm
            findings.append(
                _finding(
                    "allowed_deps",
                    f"installed package not in allowed_deps: {detail}",
                )
            )
    return findings
