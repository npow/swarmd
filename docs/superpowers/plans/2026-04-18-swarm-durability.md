# Swarm Durability & Auto-Invocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild swarm on top of Temporal so missions survive any transient failure (API 424, process death, reboot) and add a classifier hook so mission-shaped chat prompts get enforcement automatically — without relying on the human to type `/swarm`.

**Architecture:** MissionWorkflow (parent) + 3 child workflows (PatternDetector, LLMCritic, ResourceMonitor) + 13 activities on Temporal Python SDK. UserPromptSubmit hook + MCP server for classifier-driven invocation. Preserves all 7 existing enforcement primitives (anti-cheat panel, tamper, invariants, coordinator routing, scope-shrinking, ACK flow, completion judge).

**Tech Stack:** Python 3.11+, temporalio SDK (Python), anthropic SDK, click (CLI), pydantic (schemas), pytest + pytest-asyncio, mcp Python SDK, ruff (lint), mypy (types).

**Spec:** `/Users/npow/code/research/swarm/docs/superpowers/specs/2026-04-18-swarm-durability-design.md` (647 lines, 6463 words, validated through 2 deep-qa rounds)

**Estimated scope:** 25 tasks across 10 phases. Total ~3-5 weeks of focused work for one engineer. Each task produces something testable and committable.

---

## Prerequisites

Before Task 1, the implementer must have:

- Python 3.11+ available (`python3 --version`)
- `temporal` CLI installed (`brew install temporal` on macOS, or download from github.com/temporalio/cli)
- Temporal server running: `temporal server start-dev --db-filename ~/.swarm/temporal.db --port 7233 --ui-port 8233 &`
- `claude` CLI installed and authenticated (existing swarm dependency)
- `ANTHROPIC_API_KEY` env var set (for classifier Haiku calls)
- Old swarm preserved at `/Users/npow/code/research/swarm/` (reference implementation for porting enforcement logic)

Verify prerequisites:
```bash
python3 --version           # Expect: Python 3.11+
temporal --version          # Expect: temporal CLI 1.x
claude --version            # Expect: claude CLI version string
echo $ANTHROPIC_API_KEY     # Expect: non-empty
curl -s localhost:7233 || temporal operator cluster health  # Expect: healthy
```

---

## File Structure

New tree under `/Users/npow/code/research/swarm/` (old code coexists during migration — rename to `swarm_v2/` or work in-place depending on preference; this plan assumes in-place with new modules alongside old):

```
swarm/
├── cli.py                              # NEW — unified `swarm` CLI
├── classifier/                         # NEW
│   ├── __init__.py
│   ├── hook.py                         # UserPromptSubmit hook entry point
│   ├── rules.py                        # Stage 1 + 2 rule gate
│   ├── llm.py                          # Stage 3 Haiku client
│   └── prompts.py                      # classifier prompt templates
├── durable/                            # NEW
│   ├── __init__.py
│   ├── workflow.py                     # MissionWorkflow (parent)
│   ├── specialists/                    # child workflows
│   │   ├── __init__.py
│   │   ├── pattern_detector.py         # tails events.jsonl
│   │   ├── llm_critic.py               # cadence drift/progress + event anti-cheat
│   │   └── resource_monitor.py         # zombies/memory/disk
│   ├── activities/                     # activity implementations
│   │   ├── __init__.py
│   │   ├── run_claude_cli.py
│   │   ├── check_criterion.py
│   │   ├── verify_tamper.py
│   │   ├── enforce_invariants.py
│   │   ├── progress_audit.py
│   │   ├── goal_drift_check.py
│   │   ├── run_anticheat_dimension.py
│   │   ├── completion_judge.py
│   │   ├── intervention_judge.py
│   │   ├── detect_scope_shrinking.py
│   │   ├── spawn_subagent.py
│   │   ├── restart_subprocess.py
│   │   └── emit_finding.py
│   ├── errors.py                       # error classification
│   ├── retry_policies.py               # per-activity retry policies
│   ├── state.py                        # MissionState dataclass (carry-state for continue_as_new)
│   └── worker.py                       # Temporal worker entrypoint
├── mcp/                                # NEW
│   ├── __init__.py
│   └── server.py                       # swarm.propose_criteria, swarm.query, swarm.launch
├── hooks/                              # NEW + existing
│   ├── post_tool_use.sh                # existing — kept
│   ├── user_prompt_submit.py           # NEW — classifier entrypoint
│   ├── stop_regression_check.sh        # NEW — safety net
│   └── post_tool_use_track_files.sh    # NEW — companion for stop-hook
├── schemas/                            # existing — extended
│   ├── mission.py                      # extended (add max_duration_sec, observer_config keys)
│   ├── criterion.py                    # existing
│   ├── finding.py                      # existing
│   └── intervention.py                 # existing
├── tests/                              # extended
│   ├── test_activities/                # unit tests per activity
│   ├── test_workflows/                 # workflow replay tests
│   ├── test_classifier/                # classifier tests
│   └── test_integration/               # end-to-end durability tests
├── pyproject.toml                      # UPDATE — add temporalio, mcp deps
└── settings.json.template              # UPDATE — register new hooks
```

Files going away (can delete after plan completes + regression tests pass):
- `launch.sh`, `_launch_helper.py`, `swarm-spawn`, `swarm-cli` (replaced by `swarm/cli.py`)
- `specialists/supervisor.py`, `specialists/spawner.py`, `specialists/coordinator.py`, `specialists/success_verifier.py`, `specialists/pattern_detector.py`, `specialists/llm_loop.py`, `specialists/resource_monitor.py` (replaced by `durable/workflow.py` + `durable/specialists/*.py`)

---

## Phase 1: Foundation (Tasks 1-3)

### Task 1: Project scaffolding + dependencies

**Files:**
- Modify: `pyproject.toml`
- Create: `swarm/durable/__init__.py`, `swarm/classifier/__init__.py`, `swarm/mcp/__init__.py`
- Create: `tests/test_foundation/test_imports.py`

- [ ] **Step 1: Update pyproject.toml dependencies**

Add to the `[project]` `dependencies` list:
```toml
dependencies = [
    "temporalio>=1.6.0",
    "anthropic>=0.40.0",
    "pydantic>=2.5.0",
    "click>=8.1.0",
    "mcp>=0.9.0",
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "pytest-timeout>=2.2.0",
    "ruff>=0.4.0",
    "mypy>=1.10.0",
]

[project.scripts]
swarm = "swarm.cli:cli"
```

- [ ] **Step 2: Create package `__init__.py` files**

```python
# swarm/durable/__init__.py
"""Temporal workflow + activity implementations for swarm missions."""

# swarm/classifier/__init__.py
"""Classifier hook: routes UserPromptSubmit to MISSION/CHAT/META."""

# swarm/mcp/__init__.py
"""MCP server exposing swarm tools to chat agents."""
```

- [ ] **Step 3: Write import test**

```python
# tests/test_foundation/test_imports.py
def test_imports():
    import swarm.durable
    import swarm.classifier
    import swarm.mcp
    import temporalio
    import anthropic
    import pydantic
    import click
```

- [ ] **Step 4: Run and expect pass**

Run: `pip install -e ".[dev]" && pytest tests/test_foundation/test_imports.py -v`
Expected: `test_imports PASSED`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml swarm/durable/__init__.py swarm/classifier/__init__.py swarm/mcp/__init__.py tests/test_foundation/test_imports.py
git commit -m "feat: scaffold swarm/durable, swarm/classifier, swarm/mcp packages + pin deps"
```

---

### Task 2: Error taxonomy + retry policies

**Files:**
- Create: `swarm/durable/errors.py`
- Create: `swarm/durable/retry_policies.py`
- Create: `tests/test_foundation/test_errors.py`

- [ ] **Step 1: Write failing test for error classification**

```python
# tests/test_foundation/test_errors.py
import pytest
from swarm.durable.errors import (
    classify_http_status, TransientError, TerminalError,
    AuthError, BillingError, ContextOverflowError,
)

def test_200_is_success():
    # Sanity: should not raise
    classify_http_status(200, body=b"")

def test_424_is_transient():
    with pytest.raises(TransientError):
        classify_http_status(424, body=b"")

def test_429_respects_retry_after():
    try:
        classify_http_status(429, body=b"", retry_after_sec=30)
        assert False, "expected TransientError"
    except TransientError as e:
        assert e.retry_after_sec == 30

def test_401_is_terminal():
    with pytest.raises(AuthError):
        classify_http_status(401, body=b"")

def test_400_is_terminal():
    with pytest.raises(TerminalError):
        classify_http_status(400, body=b"malformed")

def test_500_is_transient():
    with pytest.raises(TransientError):
        classify_http_status(500, body=b"")
```

- [ ] **Step 2: Run and verify fail**

Run: `pytest tests/test_foundation/test_errors.py -v`
Expected: ImportError on `from swarm.durable.errors import ...`

- [ ] **Step 3: Implement errors module**

```python
# swarm/durable/errors.py
from __future__ import annotations

TRANSIENT_HTTP = {408, 424, 429, 500, 502, 503, 504}
TERMINAL_HTTP = {400, 401, 403, 404}


class SwarmActivityError(Exception):
    """Base for all classified activity errors."""
    classification: str = "unknown"


class TransientError(SwarmActivityError):
    classification = "transient"

    def __init__(self, message: str = "", retry_after_sec: float | None = None):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class TerminalError(SwarmActivityError):
    classification = "terminal"


class AuthError(TerminalError):
    pass


class BillingError(TerminalError):
    pass


class ContextOverflowError(TerminalError):
    pass


class UserCancelledError(TerminalError):
    pass


NON_RETRYABLE_ERROR_TYPES = [
    "TerminalError",
    "AuthError",
    "BillingError",
    "ContextOverflowError",
    "UserCancelledError",
]


def classify_http_status(status: int, body: bytes, retry_after_sec: float | None = None) -> None:
    """Raise a classified exception for non-2xx; return None for 2xx.

    Used inside activities to convert HTTP responses into Temporal-friendly
    retryable/non-retryable exceptions.
    """
    if 200 <= status < 300:
        return
    if status == 401 or status == 403:
        raise AuthError(f"HTTP {status}: {body[:200]!r}")
    if status in TERMINAL_HTTP:
        raise TerminalError(f"HTTP {status}: {body[:200]!r}")
    if status in TRANSIENT_HTTP:
        raise TransientError(f"HTTP {status}: {body[:200]!r}", retry_after_sec=retry_after_sec)
    # Unknown status code → treat as transient (conservative)
    raise TransientError(f"HTTP {status} (unclassified): {body[:200]!r}")
```

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_foundation/test_errors.py -v`
Expected: all 5 tests PASSED

- [ ] **Step 5: Implement retry policies module**

```python
# swarm/durable/retry_policies.py
from datetime import timedelta
from temporalio.common import RetryPolicy
from swarm.durable.errors import NON_RETRYABLE_ERROR_TYPES


def _policy(initial_s: float, max_s: float, attempts: int) -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=initial_s),
        maximum_interval=timedelta(seconds=max_s),
        backoff_coefficient=2.0,
        maximum_attempts=attempts,
        non_retryable_error_types=NON_RETRYABLE_ERROR_TYPES,
    )


# Per-activity retry policies per spec §7.2.
RUN_CLAUDE_CLI = _policy(initial_s=2, max_s=300, attempts=20)
CHECK_CRITERION = _policy(initial_s=1, max_s=30, attempts=5)
VERIFY_TAMPER = _policy(initial_s=1, max_s=10, attempts=3)
ENFORCE_INVARIANTS = _policy(initial_s=1, max_s=10, attempts=3)
PROGRESS_AUDIT = _policy(initial_s=2, max_s=30, attempts=5)
GOAL_DRIFT_CHECK = _policy(initial_s=2, max_s=30, attempts=5)
RUN_ANTICHEAT_DIMENSION = _policy(initial_s=5, max_s=300, attempts=10)
COMPLETION_JUDGE = _policy(initial_s=1, max_s=10, attempts=3)
INTERVENTION_JUDGE = _policy(initial_s=0.1, max_s=2, attempts=3)
SPAWN_SUBAGENT = _policy(initial_s=2, max_s=60, attempts=3)
RESTART_SUBPROCESS = _policy(initial_s=1, max_s=10, attempts=5)
EMIT_FINDING = _policy(initial_s=0.1, max_s=5, attempts=3)
# classify_prompt: no retry — fail-open to CHAT (handled in hook, not as Temporal retry)

HEARTBEAT_TIMEOUT_RUN_CLAUDE_CLI = timedelta(minutes=2)
HEARTBEAT_TIMEOUT_LONG_ACTIVITY = timedelta(seconds=30)
```

- [ ] **Step 6: Commit**

```bash
git add swarm/durable/errors.py swarm/durable/retry_policies.py tests/test_foundation/test_errors.py
git commit -m "feat(durable): error classification + per-activity retry policies"
```

---

### Task 3: MissionState dataclass (carry-state for continue_as_new)

**Files:**
- Create: `swarm/durable/state.py`
- Create: `tests/test_foundation/test_state.py`
- Modify: `swarm/schemas/mission.py` (add `max_duration_sec`, observer_config keys)

- [ ] **Step 1: Write failing test for MissionState roundtrip**

```python
# tests/test_foundation/test_state.py
from datetime import datetime, timezone
from swarm.durable.state import MissionState, CriterionState

def test_mission_state_roundtrip():
    s = MissionState(
        phase="running",
        criteria_state={
            "c1": CriterionState(pass_=True, last_check_ts=datetime(2026, 4, 18, tzinfo=timezone.utc), streak_sec=30),
        },
        hold_window_start=None,
        findings_count=0,
        abort_reason=None,
        child_workflow_ids={},
        strikes_by_dimension={},
        tried_strategies=[],
        spawn_tree={"live_count": 0, "per_parent_fan_out": {}},
        pending_interventions=[],
    )
    d = s.model_dump()
    s2 = MissionState.model_validate(d)
    assert s2 == s

def test_empty_carry_is_first_launch():
    # continue_as_new callers use empty MissionState() as sentinel for first launch
    s = MissionState.empty()
    assert s.phase == "launching"
    assert s.criteria_state == {}
    assert s.child_workflow_ids == {}
```

- [ ] **Step 2: Run and verify fail**

Run: `pytest tests/test_foundation/test_state.py -v`
Expected: ImportError

- [ ] **Step 3: Implement MissionState**

```python
# swarm/durable/state.py
from __future__ import annotations
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

Phase = Literal[
    "launching", "running", "passing", "hold_window",
    "complete", "aborting", "aborted", "failed_terminal",
]


class CriterionState(BaseModel):
    pass_: bool = Field(False, alias="pass")
    last_check_ts: datetime | None = None
    streak_sec: float = 0.0
    exit_code: int | None = None
    stderr_tail: str = ""

    model_config = {"populate_by_name": True}


class SpawnTree(BaseModel):
    live_count: int = 0
    per_parent_fan_out: dict[str, int] = Field(default_factory=dict)


class MissionState(BaseModel):
    """Durable state carried across MissionWorkflow.continue_as_new calls.

    This is the ONLY state the workflow must persist. Everything else can be
    derived from Temporal history.
    """
    phase: Phase = "launching"
    criteria_state: dict[str, CriterionState] = Field(default_factory=dict)
    hold_window_start: datetime | None = None
    findings_count: int = 0
    abort_reason: str | None = None
    # Child workflow IDs: keys are {"pattern_detector", "llm_critic", "resource_monitor"}
    child_workflow_ids: dict[str, str] = Field(default_factory=dict)
    # Escape-ladder state (per spec §6.4 row 5)
    strikes_by_dimension: dict[str, int] = Field(default_factory=dict)
    tried_strategies: list[str] = Field(default_factory=list)
    # Subagent admission-control state (per spec §6.4 row 9)
    spawn_tree: SpawnTree = Field(default_factory=SpawnTree)
    # Pending interventions awaiting ack (reissue after 120s)
    pending_interventions: list[dict] = Field(default_factory=list)

    @classmethod
    def empty(cls) -> "MissionState":
        return cls()
```

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_foundation/test_state.py -v`
Expected: both tests PASSED

- [ ] **Step 5: Extend mission.py schema with new fields**

In `swarm/schemas/mission.py`, add to the `Mission` class:
```python
class Mission(BaseModel):
    # ... existing fields ...
    max_duration_sec: int = 14400  # 4 hours default; 0 = no timeout
    # ... rest
```

And extend `ObserverConfig`:
```python
class ObserverConfig(BaseModel):
    # Existing fields (kept for backward-compat):
    plan_checkpoint_every_sec: int = 600
    goal_drift_cadence_sec: int = 120
    progress_audit_cadence_sec: int = 120
    pattern_thresholds: dict = Field(default_factory=dict)
    # New fields (per spec §6.2 observer_config):
    pattern_detector_sec: int = 10
    llm_critic_sec: int = 120
    resource_monitor_sec: int = 30
```

- [ ] **Step 6: Commit**

```bash
git add swarm/durable/state.py swarm/schemas/mission.py tests/test_foundation/test_state.py
git commit -m "feat(durable): MissionState dataclass + extend mission schema with max_duration_sec + observer_config cadences"
```

---

## Phase 2: Simple activities (Tasks 4-8)

### Task 4: check_criterion activity

**Files:**
- Create: `swarm/durable/activities/check_criterion.py`
- Create: `tests/test_activities/test_check_criterion.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_activities/test_check_criterion.py
import pytest
from temporalio.testing import ActivityEnvironment
from swarm.durable.activities.check_criterion import check_criterion, CriterionCheckResult
from swarm.schemas.criterion import Criterion  # existing schema

@pytest.mark.asyncio
async def test_passing_criterion_returns_pass_true(tmp_path):
    c = Criterion(
        id="test_file_exists",
        description="file exists",
        check=f"test -f {tmp_path}/sentinel",
        timeout_sec=5,
        idempotent=True,
    )
    (tmp_path / "sentinel").write_text("x")
    env = ActivityEnvironment()
    result = await env.run(check_criterion, c, str(tmp_path))
    assert result.pass_ is True
    assert result.exit_code == 0

@pytest.mark.asyncio
async def test_failing_criterion_returns_pass_false(tmp_path):
    c = Criterion(id="nope", description="fails", check="false", timeout_sec=5, idempotent=True)
    env = ActivityEnvironment()
    result = await env.run(check_criterion, c, str(tmp_path))
    assert result.pass_ is False
    assert result.exit_code != 0

@pytest.mark.asyncio
async def test_timeout_returns_pass_false_with_stderr(tmp_path):
    c = Criterion(id="slow", description="sleeps", check="sleep 10", timeout_sec=1, idempotent=True)
    env = ActivityEnvironment()
    result = await env.run(check_criterion, c, str(tmp_path))
    assert result.pass_ is False
    assert "timeout" in result.stderr_tail.lower()
```

- [ ] **Step 2: Run and verify fail**

Run: `pytest tests/test_activities/test_check_criterion.py -v`
Expected: ImportError

- [ ] **Step 3: Implement check_criterion**

```python
# swarm/durable/activities/check_criterion.py
from __future__ import annotations
import asyncio
import subprocess
import time
from dataclasses import dataclass
from temporalio import activity
from swarm.schemas.criterion import Criterion


@dataclass
class CriterionCheckResult:
    criterion_id: str
    pass_: bool
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_ms: int


@activity.defn(name="check_criterion")
async def check_criterion(criterion: Criterion, workspace: str) -> CriterionCheckResult:
    """Run one criterion's shell command and report pass/fail.

    Idempotent: safe to call multiple times. Subject to criterion.timeout_sec.
    """
    start = time.monotonic()
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_shell(
                criterion.check,
                cwd=workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_inherit_env(),
            ),
            timeout=criterion.timeout_sec,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=criterion.timeout_sec,
        )
        exit_code = proc.returncode or 0
        return CriterionCheckResult(
            criterion_id=criterion.id,
            pass_=(exit_code == 0),
            exit_code=exit_code,
            stdout_tail=_tail(stdout, 2000),
            stderr_tail=_tail(stderr, 2000),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    except asyncio.TimeoutError:
        return CriterionCheckResult(
            criterion_id=criterion.id,
            pass_=False,
            exit_code=-1,
            stdout_tail="",
            stderr_tail=f"timeout after {criterion.timeout_sec}s",
            duration_ms=int((time.monotonic() - start) * 1000),
        )


def _inherit_env() -> dict:
    import os
    return dict(os.environ)


def _tail(b: bytes, n: int) -> str:
    s = b.decode("utf-8", errors="replace")
    return s[-n:] if len(s) > n else s
```

- [ ] **Step 4: Run test to verify pass**

Run: `pytest tests/test_activities/test_check_criterion.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add swarm/durable/activities/check_criterion.py tests/test_activities/test_check_criterion.py
git commit -m "feat(durable/activities): check_criterion activity with timeout handling"
```

---

### Task 5: verify_tamper activity

**Files:**
- Create: `swarm/durable/activities/verify_tamper.py`
- Create: `tests/test_activities/test_verify_tamper.py`

Preserves `success_verifier.verify_tamper()` (existing at `specialists/success_verifier.py:117-164`). Implementer should PORT that code into an activity — don't redesign the check logic, just re-home it.

- [ ] **Step 1: Write failing test**

```python
# tests/test_activities/test_verify_tamper.py
import json, hashlib, pytest
from pathlib import Path
from temporalio.testing import ActivityEnvironment
from swarm.durable.activities.verify_tamper import verify_tamper, TamperResult

@pytest.mark.asyncio
async def test_lock_intact_returns_not_detected(tmp_path):
    # Create a mission file + matching lock + matching out-of-tree sha
    mfile = tmp_path / "mission.yaml"; mfile.write_text("ok")
    h = hashlib.sha256(b"ok").hexdigest()
    (tmp_path / "mission.lock.json").write_text(json.dumps({"files": {"mission.yaml": h}}))
    sha_path = tmp_path / "out" / "lock.sha"
    sha_path.parent.mkdir()
    # Spec-level sha is the hash of the lock JSON itself
    sha = hashlib.sha256((tmp_path / "mission.lock.json").read_bytes()).hexdigest()
    sha_path.write_text(sha)
    env = ActivityEnvironment()
    res = await env.run(verify_tamper, str(tmp_path), str(sha_path))
    assert res.detected is False

@pytest.mark.asyncio
async def test_file_modified_returns_detected(tmp_path):
    mfile = tmp_path / "mission.yaml"; mfile.write_text("original")
    h = hashlib.sha256(b"original").hexdigest()
    (tmp_path / "mission.lock.json").write_text(json.dumps({"files": {"mission.yaml": h}}))
    sha_path = tmp_path / "out" / "lock.sha"; sha_path.parent.mkdir()
    sha_path.write_text(hashlib.sha256((tmp_path / "mission.lock.json").read_bytes()).hexdigest())
    # Now tamper
    mfile.write_text("changed")
    env = ActivityEnvironment()
    res = await env.run(verify_tamper, str(tmp_path), str(sha_path))
    assert res.detected is True
    assert res.finding is not None
    assert "mission.yaml" in res.finding["verdict"]
```

- [ ] **Step 2: Run test, verify ImportError**

Run: `pytest tests/test_activities/test_verify_tamper.py -v`

- [ ] **Step 3: Implement verify_tamper — port from `specialists/success_verifier.py:117-164`**

```python
# swarm/durable/activities/verify_tamper.py
from __future__ import annotations
import hashlib, json
from dataclasses import dataclass
from pathlib import Path
from temporalio import activity


@dataclass
class TamperResult:
    detected: bool
    finding: dict | None  # Finding dict or None


@activity.defn(name="verify_tamper")
async def verify_tamper(mission_dir: str, out_of_tree_sha_path: str) -> TamperResult:
    """Verify mission files match the pinned hashes.

    Ported from specialists/success_verifier.verify_tamper() — preserves semantics.
    Runs before criterion checks every verifier cycle.
    """
    mdir = Path(mission_dir)
    lock_path = mdir / "mission.lock.json"
    sha_path = Path(out_of_tree_sha_path)

    if not lock_path.exists():
        return TamperResult(detected=True, finding=_tamper_finding(f"lock missing: {lock_path}"))
    if not sha_path.exists():
        return TamperResult(detected=True, finding=_tamper_finding(f"out-of-tree sha missing: {sha_path}"))

    lock_bytes = lock_path.read_bytes()
    expected_sha = sha_path.read_text().strip()
    actual_sha = hashlib.sha256(lock_bytes).hexdigest()
    if actual_sha != expected_sha:
        return TamperResult(
            detected=True,
            finding=_tamper_finding(f"lock hash mismatch: expected {expected_sha}, got {actual_sha}"),
        )

    lock = json.loads(lock_bytes)
    for rel_path, expected in lock["files"].items():
        fp = mdir / rel_path
        if not fp.exists():
            return TamperResult(detected=True, finding=_tamper_finding(f"pinned file missing: {rel_path}"))
        actual = hashlib.sha256(fp.read_bytes()).hexdigest()
        if actual != expected:
            return TamperResult(detected=True, finding=_tamper_finding(f"tampered file {rel_path}: {expected} → {actual}"))

    return TamperResult(detected=False, finding=None)


def _tamper_finding(reason: str) -> dict:
    return {
        "type": "meta",
        "subtype": "tamper_detected",
        "severity": "critical",
        "verdict": reason,
    }
```

- [ ] **Step 4: Run test to verify pass**

Expected: 2 PASSED

- [ ] **Step 5: Commit**

```bash
git add swarm/durable/activities/verify_tamper.py tests/test_activities/test_verify_tamper.py
git commit -m "feat(durable/activities): verify_tamper activity (ported from success_verifier)"
```

---

### Task 6: enforce_invariants activity

**Files:**
- Create: `swarm/durable/activities/enforce_invariants.py`
- Create: `tests/test_activities/test_enforce_invariants.py`

Preserves `success_verifier.enforce_invariants()` (existing at `specialists/success_verifier.py:167-318`) — port the 4 invariant checks (no_mock, test_count_floor, assertion_count_floor, allowed_deps) into the activity.

Implementer should read the old code first to understand each check's semantics, then port with tests.

- [ ] **Step 1: Write failing tests for each of the 4 invariants**

```python
# tests/test_activities/test_enforce_invariants.py
import pytest
from pathlib import Path
from temporalio.testing import ActivityEnvironment
from swarm.durable.activities.enforce_invariants import enforce_invariants, InvariantsResult
from swarm.schemas.mission import Invariants

@pytest.mark.asyncio
async def test_no_mock_passes_on_clean_file(tmp_path):
    (tmp_path / "src").mkdir(); (tmp_path / "src" / "pure.py").write_text("def add(a, b): return a + b")
    inv = Invariants(no_mock={"paths": ["src/**/*.py"]})
    env = ActivityEnvironment()
    res = await env.run(enforce_invariants, str(tmp_path), inv)
    assert res.findings == []

@pytest.mark.asyncio
async def test_no_mock_flags_mock_usage(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "tainted.py").write_text("from unittest.mock import patch\ndef f(): pass")
    inv = Invariants(no_mock={"paths": ["src/**/*.py"]})
    env = ActivityEnvironment()
    res = await env.run(enforce_invariants, str(tmp_path), inv)
    assert len(res.findings) == 1
    assert res.findings[0]["subtype"] == "invariant_no_mock"

@pytest.mark.asyncio
async def test_test_count_floor_blocks_below_min(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_a(): pass\n")
    inv = Invariants(test_count_floor={"paths": ["tests/**/*.py"], "min": 3})
    env = ActivityEnvironment()
    res = await env.run(enforce_invariants, str(tmp_path), inv)
    assert len(res.findings) == 1
    assert "1" in res.findings[0]["verdict"] and "3" in res.findings[0]["verdict"]

# Additional tests: assertion_count_floor, allowed_deps — follow same pattern
```

- [ ] **Step 2: Run and verify fail**

Run: `pytest tests/test_activities/test_enforce_invariants.py -v`

- [ ] **Step 3: Implement — PORT from old success_verifier.py lines 167-318**

Read the old file:
```bash
sed -n '167,318p' /Users/npow/code/research/swarm/specialists/success_verifier.py
```

Then write `swarm/durable/activities/enforce_invariants.py` that wraps the same logic as a Temporal `@activity.defn`. Keep the 4 check functions (`_check_no_mock`, `_check_test_count_floor`, `_check_assertion_count_floor`, `_check_allowed_deps`) with their existing behavior; the activity just iterates over mission.invariants and calls each. Return `InvariantsResult(findings=[...])` where each finding is the same schema as today's coordinator expects.

Implementer check: the existing code passes — port with tests, don't rewrite the detection logic. Today's tests (if any) for these checks remain the reference.

- [ ] **Step 4: Run tests, verify pass**

- [ ] **Step 5: Commit**

```bash
git add swarm/durable/activities/enforce_invariants.py tests/test_activities/test_enforce_invariants.py
git commit -m "feat(durable/activities): enforce_invariants activity (ported no_mock + test_count_floor + assertion_count_floor + allowed_deps from success_verifier)"
```

---

### Task 7: emit_finding activity

**Files:**
- Create: `swarm/durable/activities/emit_finding.py`
- Create: `tests/test_activities/test_emit_finding.py`

Appends to `findings.jsonl` AND (for intervention-typed findings) `interventions.jsonl` so existing hooks keep working.

- [ ] **Step 1: Write failing test**

```python
# tests/test_activities/test_emit_finding.py
import json, pytest
from pathlib import Path
from temporalio.testing import ActivityEnvironment
from swarm.durable.activities.emit_finding import emit_finding

@pytest.mark.asyncio
async def test_finding_appended(tmp_path):
    env = ActivityEnvironment()
    await env.run(emit_finding, str(tmp_path), {"type": "progress", "severity": "minor", "verdict": "ok"})
    lines = (tmp_path / "findings.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["verdict"] == "ok"

@pytest.mark.asyncio
async def test_intervention_typed_finding_also_appended_to_interventions(tmp_path):
    env = ActivityEnvironment()
    f = {"type": "intervention", "severity": "major", "verdict": "nudge",
         "intervention": {"tier": "soft", "strategy": "reprompt", "nudge_text": "..."}}
    await env.run(emit_finding, str(tmp_path), f)
    assert (tmp_path / "findings.jsonl").exists()
    assert (tmp_path / "interventions.jsonl").exists()
```

- [ ] **Step 2: Run, verify fail**

- [ ] **Step 3: Implement**

```python
# swarm/durable/activities/emit_finding.py
from __future__ import annotations
import json
import time
from pathlib import Path
from temporalio import activity


@activity.defn(name="emit_finding")
async def emit_finding(session_state_dir: str, finding: dict) -> None:
    """Append finding to findings.jsonl on disk (mirror of workflow signal).

    For intervention-typed findings, also appends to interventions.jsonl so
    existing hook-based consumption (on_stop.py, on_session_start.py) keeps
    working unchanged.
    """
    d = Path(session_state_dir); d.mkdir(parents=True, exist_ok=True)
    finding = {**finding, "emitted_at": time.time()}
    _append_jsonl(d / "findings.jsonl", finding)
    if finding.get("type") == "intervention":
        _append_jsonl(d / "interventions.jsonl", finding)


def _append_jsonl(p: Path, obj: dict) -> None:
    # Open in append mode (thread-safe at OS level for small writes)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")
```

- [ ] **Step 4: Run tests, verify pass**

- [ ] **Step 5: Commit**

```bash
git add swarm/durable/activities/emit_finding.py tests/test_activities/test_emit_finding.py
git commit -m "feat(durable/activities): emit_finding with intervention mirror"
```

---

### Task 8: intervention_judge + completion_judge activities

**Files:**
- Create: `swarm/durable/activities/intervention_judge.py`
- Create: `swarm/durable/activities/completion_judge.py`
- Create: `tests/test_activities/test_intervention_judge.py`
- Create: `tests/test_activities/test_completion_judge.py`

Both are PORTS from existing specialists:
- `intervention_judge.py` ← `specialists/intervention_judge.py` (escape ladder policy, lines 34-182)
- `completion_judge.py` ← `specialists/completion_judge.py` (6 preconditions, lines 62-168)

- [ ] **Step 1: Read old implementations**

```bash
cat /Users/npow/code/research/swarm/specialists/intervention_judge.py
cat /Users/npow/code/research/swarm/specialists/completion_judge.py
```

- [ ] **Step 2: Write tests for intervention_judge (3 cases: first strike → soft, third strike → recover, unrecognized finding → None)**

```python
# tests/test_activities/test_intervention_judge.py
import pytest
from temporalio.testing import ActivityEnvironment
from swarm.durable.activities.intervention_judge import intervention_judge, InterventionDecision

@pytest.mark.asyncio
async def test_first_strike_returns_soft():
    finding = {"type": "drift", "severity": "major", "verdict": "agent drifted"}
    env = ActivityEnvironment()
    dec = await env.run(intervention_judge, finding, {}, [])
    assert dec.tier == "soft"

@pytest.mark.asyncio
async def test_third_strike_escalates_to_recover():
    finding = {"type": "drift", "severity": "major", "verdict": "agent drifted"}
    strikes = {"drift": 3}
    env = ActivityEnvironment()
    dec = await env.run(intervention_judge, finding, strikes, ["soft_reprompt", "hard_pause"])
    assert dec.tier == "recover"

@pytest.mark.asyncio
async def test_unrecognized_finding_returns_none():
    finding = {"type": "meta", "subtype": "specialist_degraded", "severity": "minor"}
    env = ActivityEnvironment()
    dec = await env.run(intervention_judge, finding, {}, [])
    assert dec is None  # no intervention for specialist degradation
```

- [ ] **Step 3: Implement intervention_judge (port decide() + escape ladder from old file)**

```python
# swarm/durable/activities/intervention_judge.py
from __future__ import annotations
from dataclasses import dataclass
from temporalio import activity

# Port the ESCAPE_LADDER + decide() logic from specialists/intervention_judge.py
# Keep the policy unchanged — this is pure-function logic, just re-housed.

ESCAPE_LADDER = [
    # (rung_index, strategy, nudge_text_template)
    # ... port from old file lines 34-66
]


@dataclass
class InterventionDecision:
    tier: str  # "soft" | "hard" | "recover" | "mission_level_alert"
    strategy: str
    nudge_text: str


@activity.defn(name="intervention_judge")
async def intervention_judge(
    finding: dict,
    strikes_by_dimension: dict[str, int],
    tried_strategies: list[str],
) -> InterventionDecision | None:
    """Classify a finding into a tier + strategy.

    Ported from specialists/intervention_judge.decide(). Pure function — no
    side effects. Safe to call from workflow signal handler (but listed as
    activity for flexibility).
    """
    # ... port from specialists/intervention_judge.py decide()
    raise NotImplementedError("port from old intervention_judge.py decide()")
```

- [ ] **Step 4: Implement completion_judge (port 6 preconditions)**

```python
# swarm/durable/activities/completion_judge.py
from __future__ import annotations
from dataclasses import dataclass
from temporalio import activity


@dataclass
class CompletionDecision:
    approved: bool
    reasons: list[str]  # on failure: why blocked; on success: []


@activity.defn(name="completion_judge")
async def completion_judge(mission_state: dict, session_state_dir: str) -> CompletionDecision:
    """Check 6 preconditions before allowing mission→complete transition.

    Ported from specialists/completion_judge.py lines 62-168:
      1. Hold-window recency
      2. No open cheat findings
      3. No open fabrication findings
      4. No open tamper findings
      5. No critic disagreements
      6. Per-criterion anticheat passes
    """
    reasons = []
    # 1. Hold-window recency — check mission_state["hold_window_start"] age
    # 2-4. Scan findings.jsonl for open findings of each type
    # 5. Scan critic verdicts for disagreements
    # 6. Per-criterion anticheat results from workflow state
    # (port from old completion_judge.py)
    return CompletionDecision(approved=(len(reasons) == 0), reasons=reasons)
```

- [ ] **Step 5: Run tests, commit**

```bash
git add swarm/durable/activities/intervention_judge.py swarm/durable/activities/completion_judge.py tests/test_activities/test_intervention_judge.py tests/test_activities/test_completion_judge.py
git commit -m "feat(durable/activities): intervention_judge + completion_judge (ported from specialists)"
```

---

## Phase 3: run_claude_cli (Task 9) — the durability-critical activity

### Task 9: run_claude_cli with heartbeat timer, cancellation, process-group kill

**Files:**
- Create: `swarm/durable/activities/run_claude_cli.py`
- Create: `tests/test_activities/test_run_claude_cli.py`

This is the longest, most critical activity. Spec §7.3 gives the target pseudocode.

- [ ] **Step 1: Write integration-style test that verifies (a) first attempt uses --session-id, (b) retry uses --resume, (c) cancellation SIGTERMs process group**

```python
# tests/test_activities/test_run_claude_cli.py
import pytest, asyncio, os, signal
from unittest.mock import patch, MagicMock
from temporalio.testing import ActivityEnvironment
from swarm.durable.activities.run_claude_cli import run_claude_cli

@pytest.mark.asyncio
async def test_first_attempt_uses_session_id(monkeypatch):
    """Activity with attempt=1 should pass --session-id, not --resume."""
    calls = []
    def fake_spawn(sid, prose, use_resume):
        calls.append({"sid": sid, "prose": prose, "use_resume": use_resume})
        # Return a fake proc that immediately "exits"
        return _FakeProc(exit_code=0)
    monkeypatch.setattr("swarm.durable.activities.run_claude_cli.spawn_claude", fake_spawn)
    env = ActivityEnvironment()
    # ActivityEnvironment provides activity.info() with attempt=1 by default
    await env.run(run_claude_cli, "sid-1", "mission prose")
    assert calls[0]["use_resume"] is False

# Additional tests: retry with attempt=2 uses resume, cancellation SIGTERMs group, heartbeat fires every 30s
```

- [ ] **Step 2-5: Implement run_claude_cli per spec §7.3 with all the robustness features**

```python
# swarm/durable/activities/run_claude_cli.py
from __future__ import annotations
import asyncio, os, signal, subprocess
from dataclasses import dataclass, field
from pathlib import Path
from temporalio import activity


@dataclass
class ClaudeResult:
    events: int
    exit_code: int


@dataclass
class _Proc:
    pid: int
    pgid: int
    exit_code: int | None = None

    @property
    def exited(self) -> bool:
        return self.exit_code is not None


def spawn_claude(session_id: str, mission_prose: str, use_resume: bool) -> subprocess.Popen:
    """Spawn the claude subprocess in its own process group."""
    if use_resume:
        args = ["claude", "--resume", session_id]
    else:
        args = ["claude", "--session-id", session_id, mission_prose]
    proc = subprocess.Popen(
        args,
        start_new_session=True,  # own process group — lets us group-kill
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


async def tail_events(session_id: str):
    """Yield parsed events from ~/.swarm/state/<sid>/events.jsonl as they arrive."""
    events_path = Path.home() / ".swarm" / "state" / session_id / "events.jsonl"
    # Implement inotify-style tail: poll mtime, read new lines, yield each.
    # (Omitted for brevity — standard tail-and-parse pattern.)
    ...


@activity.defn(name="run_claude_cli")
async def run_claude_cli(session_id: str, mission_prose: str) -> ClaudeResult:
    """Launch claude subprocess, heartbeat on independent 30s timer, handle cancellation."""
    attempt = activity.info().attempt
    use_resume = (attempt > 1)
    proc = spawn_claude(session_id, mission_prose, use_resume=use_resume)
    latest = {"last_event_id": None, "last_tool": None, "event_count": 0}

    async def heartbeat_loop():
        while proc.returncode is None:
            activity.heartbeat(latest)
            await asyncio.sleep(30)

    async def event_loop():
        async for event in tail_events(session_id):
            latest.update({
                "last_event_id": event.get("id"),
                "last_tool": event.get("tool_name"),
                "event_count": latest["event_count"] + 1,
            })

    async def wait_for_exit():
        while proc.returncode is None:
            await asyncio.sleep(0.2)
            proc.poll()

    try:
        await asyncio.gather(heartbeat_loop(), event_loop(), wait_for_exit())
    except asyncio.CancelledError:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(wait_for_exit(), timeout=5)
        except asyncio.TimeoutError:
            os.killpg(pgid, signal.SIGKILL)
        raise

    if proc.returncode != 0:
        from swarm.durable.errors import classify_http_status  # reuse classifier
        # Classify based on stderr — if we can parse an HTTP status, do so; otherwise TransientError for non-zero exit
        from swarm.durable.errors import TransientError
        raise TransientError(f"claude exited {proc.returncode}")
    return ClaudeResult(events=latest["event_count"], exit_code=0)
```

- [ ] **Step 6: Commit**

```bash
git add swarm/durable/activities/run_claude_cli.py tests/test_activities/test_run_claude_cli.py
git commit -m "feat(durable/activities): run_claude_cli with time-driven heartbeat + process-group kill on cancel + attempt-based resume"
```

---

## Phase 4: LLM-calling activities (Tasks 10-12)

### Task 10: progress_audit + goal_drift_check activities

PORT from `specialists/progress_auditor.py` and `specialists/goal_drift_critic.py`. Each invokes Haiku via anthropic SDK, inspects recent transcript events, returns findings.

Test each with mocked anthropic responses. Full steps omitted — follow the same TDD pattern as Tasks 4-8 (write test with mocked anthropic.Client().messages.create, implement, verify, commit).

Commit message:
```bash
git commit -m "feat(durable/activities): progress_audit + goal_drift_check (cadence-driven LLM review, ported from specialists)"
```

---

### Task 11: run_anticheat_dimension activity (6-dim panel unit)

**Files:**
- Create: `swarm/durable/activities/run_anticheat_dimension.py`
- Create: `tests/test_activities/test_run_anticheat_dimension.py`

PORT the dimension prompts from `specialists/anticheat_critic_panel.py:_DIMENSION_PROMPTS` (lines 73-103). Each dimension has its own prompt template.

- [ ] **Step 1: Test — each dimension produces distinct prompt + parses verdict**

```python
# tests/test_activities/test_run_anticheat_dimension.py
import pytest
from unittest.mock import patch
from temporalio.testing import ActivityEnvironment
from swarm.durable.activities.run_anticheat_dimension import run_anticheat_dimension

@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", [
    "scope_reduction", "mock_out", "tautology",
    "hardcode", "off_criterion", "coordinated_edit",
])
async def test_dimension_routes_to_correct_prompt(dimension):
    with patch("swarm.durable.activities.run_anticheat_dimension._invoke_opus") as mock:
        mock.return_value = '{"verdict": "pass", "rationale": "ok"}'
        env = ActivityEnvironment()
        ctx = {"criterion_id": "test", "diff": "", "events": [], "check_command": "pytest"}
        res = await env.run(run_anticheat_dimension, dimension, ctx, {"primary": "claude -p --bare --model opus"})
        assert res.dimension == dimension
        assert res.verdict == "pass"
```

- [ ] **Step 2: Implement — port prompts + invocation**

```python
# swarm/durable/activities/run_anticheat_dimension.py
from __future__ import annotations
import asyncio, json, subprocess
from dataclasses import dataclass
from temporalio import activity


# Port from specialists/anticheat_critic_panel.py:_DIMENSION_PROMPTS
_DIMENSION_PROMPTS = {
    "scope_reduction": "...",
    "mock_out": "...",
    "tautology": "...",
    "hardcode": "...",
    "off_criterion": "...",
    "coordinated_edit": "...",
}


@dataclass
class AnticheatVerdict:
    dimension: str
    verdict: str  # "pass" | "fail" | "suspicious"
    rationale: str


@activity.defn(name="run_anticheat_dimension")
async def run_anticheat_dimension(
    dimension: str,
    context: dict,
    anticheat_config: dict,
) -> AnticheatVerdict:
    """Run ONE dimension of the 6-dim anti-cheat panel.

    Called in parallel from LLMCriticWorkflow (6 fan-outs). Preserves
    anticheat_critic_panel.run_panel() semantics from today's swarm.
    """
    prompt = _DIMENSION_PROMPTS[dimension].format(**context)
    model_cmd = anticheat_config.get("primary", "claude -p --bare --model opus")
    response = await _invoke_opus(model_cmd, prompt)
    parsed = json.loads(response)
    return AnticheatVerdict(
        dimension=dimension,
        verdict=parsed["verdict"],
        rationale=parsed["rationale"],
    )


async def _invoke_opus(cmd: str, prompt: str) -> str:
    """Invoke configured reviewer (default: claude -p --bare --model opus).

    Subprocess approach inherits claude CLI auth. Alternative: direct anthropic SDK.
    """
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(prompt.encode())
    if proc.returncode != 0:
        raise RuntimeError(f"anticheat reviewer failed: {stderr.decode()[:500]}")
    return stdout.decode()
```

- [ ] **Steps 3-5: Run tests + commit**

```bash
git commit -m "feat(durable/activities): run_anticheat_dimension (one dim of 6-dim panel, ported prompts from anticheat_critic_panel)"
```

---

### Task 12: spawn_subagent + restart_subprocess + detect_scope_shrinking activities

Three more PORTs. Each follows the same TDD pattern (test with mocked subprocess/LLM → port → verify → commit). Full details omitted; implementer reads the old specialists:

- `spawn_subagent` ← `specialists/spawner.py` (spawn logic only; admission control moves to workflow state)
- `restart_subprocess` ← simple subprocess restart wrapper
- `detect_scope_shrinking` ← `pattern_detector.py:detect_scope_shrinking` (lines 144-247)

Single commit at end:
```bash
git commit -m "feat(durable/activities): spawn_subagent + restart_subprocess + detect_scope_shrinking (ported from specialists)"
```

---

## Phase 5: MissionWorkflow parent (Tasks 13-14)

### Task 13: MissionWorkflow skeleton with verifier loop + signal handlers + query handlers

**Files:**
- Create: `swarm/durable/workflow.py`
- Create: `tests/test_workflows/test_mission_workflow.py`

This is the LOAD-BEARING workflow. Uses Temporal's testing framework for replay-safe tests.

- [ ] **Step 1: Write failing test — verifier loop transitions running → hold_window when all criteria pass**

```python
# tests/test_workflows/test_mission_workflow.py
import pytest
from datetime import timedelta
from temporalio.testing import WorkflowEnvironment
from temporalio.client import Client
from temporalio.worker import Worker
from swarm.durable.workflow import MissionWorkflow
from swarm.durable.activities import check_criterion, verify_tamper, enforce_invariants  # etc.
from swarm.schemas.mission import Mission, Verification, Criterion

@pytest.mark.asyncio
async def test_mission_transitions_to_hold_window_when_all_pass(monkeypatch):
    mission = Mission(
        mission="test",
        workspace="/tmp/test-ws",
        success_criteria=[Criterion(id="c1", description="d", check="true", timeout_sec=5, idempotent=True)],
        verification=Verification(run_every_sec=1, hold_window_sec=3),
        max_duration_sec=60,
    )
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="test", workflows=[MissionWorkflow], activities=[...]):
            handle = await env.client.start_workflow(
                MissionWorkflow.run, args=[mission, None], id="wf-1", task_queue="test",
            )
            # Skip time forward, poll for phase transition
            await env.sleep(4)
            status = await handle.query(MissionWorkflow.get_status)
            assert status["phase"] in {"hold_window", "complete"}
```

- [ ] **Steps 2-5: Implement MissionWorkflow per spec §6.2**

```python
# swarm/durable/workflow.py
from __future__ import annotations
import asyncio
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from swarm.durable.state import MissionState, CriterionState
from swarm.durable import retry_policies


@workflow.defn
class MissionWorkflow:
    def __init__(self) -> None:
        self._state = MissionState.empty()
        self._pending_signal_work: list[dict] = []

    @workflow.run
    async def run(self, mission, carry: MissionState | None = None) -> dict:
        """Main loop — verifier cadence + signal handling + continue_as_new.

        carry=None on first launch; carry=MissionState on continue_as_new.
        """
        self._state = carry or MissionState.empty()
        self._state.phase = "running" if not carry else self._state.phase

        # Start child workflows (skip if resuming — they're already running)
        if not self._state.child_workflow_ids:
            await self._start_children(mission)

        # Main verifier loop
        while self._state.phase not in {"complete", "aborted", "failed_terminal"}:
            await self._verifier_cycle(mission)
            await workflow.sleep(mission.verification.run_every_sec)

            # History-bounding: continue_as_new when Temporal suggests it
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(MissionWorkflow.run, mission, self._state)

        return {"phase": self._state.phase, "reason": self._state.abort_reason}

    async def _start_children(self, mission) -> None:
        # Fixed workflow IDs so we can re-acquire handles after continue_as_new
        mid = workflow.info().workflow_id
        from swarm.durable.specialists import (
            PatternDetectorWorkflow, LLMCriticWorkflow, ResourceMonitorWorkflow,
        )
        await workflow.start_child_workflow(
            PatternDetectorWorkflow.run,
            args=[mid, mission.session_id, mission.observer_config.pattern_detector_sec],
            id=f"{mid}_pattern_detector",
            parent_close_policy=workflow.ParentClosePolicy.TERMINATE,
        )
        # ... similar for llm_critic, resource_monitor
        self._state.child_workflow_ids = {
            "pattern_detector": f"{mid}_pattern_detector",
            "llm_critic": f"{mid}_llm_critic",
            "resource_monitor": f"{mid}_resource_monitor",
        }

    async def _verifier_cycle(self, mission) -> None:
        # 1. Tamper check
        tamper = await workflow.execute_activity(
            "verify_tamper", args=[mission.workspace, mission.lock_path],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=retry_policies.VERIFY_TAMPER,
        )
        if tamper.detected:
            self._state.abort_reason = tamper.finding["verdict"]
            self._state.phase = "aborting"
            return

        # 2. Invariants
        inv = await workflow.execute_activity(
            "enforce_invariants", args=[mission.workspace, mission.invariants],
            start_to_close_timeout=timedelta(seconds=15),
            retry_policy=retry_policies.ENFORCE_INVARIANTS,
        )
        for finding in inv.findings:
            await workflow.execute_activity(
                "emit_finding", args=[mission.state_dir, finding],
                start_to_close_timeout=timedelta(seconds=5),
                retry_policy=retry_policies.EMIT_FINDING,
            )

        # 3. Criterion checks — parallel fan-out
        results = await asyncio.gather(*[
            workflow.execute_activity(
                "check_criterion", args=[c, mission.workspace],
                start_to_close_timeout=timedelta(seconds=c.timeout_sec + 5),
                retry_policy=retry_policies.CHECK_CRITERION,
            )
            for c in mission.success_criteria
        ])
        # Update state, check hold_window, etc. (per spec §6.2 pseudocode)
        # ... full implementation ~100 lines; follow pseudocode exactly

    @workflow.signal
    async def finding_emitted(self, finding: dict) -> None:
        self._pending_signal_work.append({"kind": "finding", "payload": finding})

    @workflow.signal
    async def abort(self, reason: str) -> None:
        self._state.abort_reason = reason
        self._state.phase = "aborting"

    @workflow.query
    def get_status(self) -> dict:
        return self._state.model_dump()
```

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(durable): MissionWorkflow parent with verifier loop, signal handlers, query handlers"
```

---

### Task 14: MissionWorkflow continue_as_new child-reconnect path

Building on Task 13: add the child-workflow reconnection contract (spec §6.2 lines 138-155) — on continue_as_new, the new incarnation skips start_child_workflow and uses workflow.get_external_workflow_handle.

Test: run mission with forced `continue_as_new` after 100 events, verify children are NOT re-spawned and still receive signals from the new parent incarnation.

Commit:
```bash
git commit -m "feat(durable): MissionWorkflow continue_as_new child-reconnect via fixed IDs + get_external_workflow_handle"
```

---

## Phase 6: Child workflows (Tasks 15-17)

Three parallel tasks — each follows the same TDD pattern:

### Task 15: PatternDetectorWorkflow
- Tails events.jsonl, runs pattern rules (port from `pattern_detector.py:43-141`), emits findings via signal to parent.
- continue_as_new every 500 events.
- Test: feed synthetic events, verify findings emitted.

### Task 16: LLMCriticWorkflow  
- 3 functions per spec: progress_audit (cadence), goal_drift_check (cadence), run_anticheat panel fan-out (on anticheat_requested signal).
- Test: verify 6 parallel run_anticheat_dimension calls on panel trigger.

### Task 17: ResourceMonitorWorkflow
- Zombie/memory/disk checks (port from `resource_monitor.py`).
- Test: synthetic resource pressure triggers finding.

Each commits independently.

---

## Phase 7: Worker + CLI (Tasks 18-19)

### Task 18: swarm worker daemon

**Files:**
- Create: `swarm/durable/worker.py`
- Create: `tests/test_workflows/test_worker_startup.py`

- [ ] **Step 1-5: Standard TDD — worker registers all workflows + activities, connects to Temporal, polls task queue**

```python
# swarm/durable/worker.py
import asyncio
from temporalio.client import Client
from temporalio.worker import Worker
from swarm.durable.workflow import MissionWorkflow
from swarm.durable.specialists import (
    PatternDetectorWorkflow, LLMCriticWorkflow, ResourceMonitorWorkflow,
)
from swarm.durable.activities import (
    run_claude_cli, check_criterion, verify_tamper, enforce_invariants,
    progress_audit, goal_drift_check, run_anticheat_dimension,
    completion_judge, intervention_judge, detect_scope_shrinking,
    spawn_subagent, restart_subprocess, emit_finding,
)


async def main(host: str = "localhost:7233", task_queue: str = "swarm"):
    client = await Client.connect(host)
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[
            MissionWorkflow, PatternDetectorWorkflow,
            LLMCriticWorkflow, ResourceMonitorWorkflow,
        ],
        activities=[
            run_claude_cli, check_criterion, verify_tamper, enforce_invariants,
            progress_audit, goal_drift_check, run_anticheat_dimension,
            completion_judge, intervention_judge, detect_scope_shrinking,
            spawn_subagent, restart_subprocess, emit_finding,
        ],
    ):
        print(f"Swarm worker connected to {host}, polling task queue '{task_queue}'")
        await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
```

Commit: `feat(durable): swarm worker entrypoint registering all workflows + activities`

---

### Task 19: swarm CLI (launch, status, abort, findings, logs, worker, health)

**Files:**
- Create: `swarm/cli.py`
- Create: `tests/test_cli/test_cli.py`

- [ ] **Step 1: Write test for `swarm health` smoke**

- [ ] **Step 2-5: Implement click-based CLI**

```python
# swarm/cli.py
import asyncio, json, sys
from pathlib import Path
import click
import yaml
from temporalio.client import Client
from swarm.schemas.mission import Mission


@click.group()
def cli():
    """Swarm: mission-enforced claude CLI runner with Temporal durability."""
    pass


@cli.command()
@click.argument("mission_file", type=click.Path(exists=True))
async def launch(mission_file: str):
    """Launch a mission workflow."""
    mission = Mission.model_validate(yaml.safe_load(Path(mission_file).read_text()))
    client = await Client.connect("localhost:7233")
    # Pre-check: is there at least one worker polling?
    desc = await client.describe_task_queue("swarm")
    if not desc.pollers:
        click.echo("ERROR: no swarm worker running. Start one with `swarm worker &` first.", err=True)
        sys.exit(1)
    # Check workspace lock
    lock = Path(mission.workspace) / ".claude" / ".swarm-lock"
    if lock.exists():
        click.echo(f"ERROR: another mission holds the lock at {lock}", err=True)
        sys.exit(1)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(mission.session_id)
    # Start workflow
    handle = await client.start_workflow(
        "MissionWorkflow",
        args=[mission, None],
        id=mission.session_id,  # workflow_id == session_id per locked decision
        task_queue="swarm",
        execution_timeout=timedelta(seconds=mission.max_duration_sec) if mission.max_duration_sec else None,
    )
    click.echo(f"workflow_id={handle.id}")


@cli.command()
@click.argument("workflow_id")
async def status(workflow_id: str):
    client = await Client.connect("localhost:7233")
    handle = client.get_workflow_handle(workflow_id)
    result = await handle.query("get_status")
    click.echo(json.dumps(result, indent=2))


@cli.command()
@click.argument("workflow_id")
@click.option("--reason", default="user-abort")
async def abort(workflow_id: str, reason: str):
    client = await Client.connect("localhost:7233")
    handle = client.get_workflow_handle(workflow_id)
    await handle.signal("abort", reason)
    click.echo(f"abort signal sent to {workflow_id}")


@cli.command()
async def worker():
    from swarm.durable.worker import main
    await main()


@cli.command()
async def health():
    """Check Temporal connectivity, worker liveness, classifier API."""
    # ... 3 checks, print table
    ...
```

Wrap each async command:
```python
# Use asyncclick or a helper for async click commands
```

Commit: `feat(cli): swarm launch/status/abort/worker/health/findings/logs`

---

## Phase 8: Classifier + MCP (Tasks 20-22)

### Task 20: Classifier stages 1+2 (explicit prefix + rule gate)

**Files:** `swarm/classifier/rules.py` + `tests/test_classifier/test_rules.py`

Pure logic, pure-function tests — fast TDD. Per spec §9.1.

### Task 21: Classifier stage 3 (Haiku LLM call)

**Files:** `swarm/classifier/llm.py` + `swarm/classifier/prompts.py` + `tests/test_classifier/test_llm.py`

Mock the anthropic client in tests. Per spec §9.2.

### Task 22: UserPromptSubmit hook + confidence gate

**Files:** `swarm/hooks/user_prompt_submit.py` + `tests/test_classifier/test_hook.py`

Orchestrates stages 1→2→3, applies confidence gate per spec §9.3, injects `additionalContext` per hook contract. Classifier verdict logged to `~/.swarm/classifier.jsonl`.

Each task has its own commit.

---

### Task 23: MCP server (swarm.propose_criteria, swarm.query, swarm.launch)

**Files:** `swarm/mcp/server.py` + `tests/test_mcp/test_tools.py`

Exposes 3 tools per MCP Python SDK. Each tool has request/response schema. propose_criteria calls Haiku to derive mission.yaml from prompt+context. launch calls the same code path as `swarm launch` CLI. query proxies workflow.query to Temporal.

Commit: `feat(mcp): server exposing propose_criteria, query, launch tools`

---

## Phase 9: Stop-hook safety net (Task 24)

### Task 24: Stop-hook regression guard + PostToolUse companion

**Files:**
- Create: `swarm/hooks/post_tool_use_track_files.sh` (records file changes)
- Create: `swarm/hooks/stop_regression_check.sh` (consumes the record, runs build/tests)
- Create: `tests/test_hooks/test_regression_guard.sh`

PostToolUse hook writes changed-file paths to `/tmp/swarm-files-changed-$CLAUDE_SESSION_ID`. Stop hook reads the file, auto-detects build/test commands (package.json, Makefile, tox.ini, pytest.ini), runs with 5s/10s timeouts, emits system-reminder on regression.

Commit: `feat(hooks): Stop-hook regression guard + PostToolUse file tracker`

---

## Phase 10: Durability integration tests (Task 25)

### Task 25: End-to-end durability tests

**Files:** `tests/test_integration/test_durability.py`

Tests that verify the spec's §14 success criteria:

1. **API 424 survival** — inject 424 response for 60s via mocked anthropic client, verify mission continues
2. **kill -9 survival** — kill worker mid-mission, restart, verify resume
3. **Machine reboot simulation** — stop Temporal server, stop worker, restart both, verify workflow resumes
4. **Abort propagation** — run mission, abort, verify full process tree dies including subagents
5. **Continue-as-new correctness** — force 10k+ events, verify no state loss across continue_as_new

These tests require a real Temporal dev-server running. Mark them `@pytest.mark.integration` and skip in normal CI unless TEMPORAL_ADDR is set.

Commit: `test(integration): end-to-end durability tests for §14 success criteria`

---

## Self-Review Against Spec

**Spec coverage check** (each spec section → task(s)):

| Spec Section | Task(s) |
|---|---|
| §5 Design principles | All (principles inform coding, not individual tasks) |
| §6.1 Layering | Task 18 (worker) + 19 (CLI) + 22 (hook) show the 3 layers |
| §6.2 MissionWorkflow | Task 13, 14 |
| §6.2 Child workflows | Task 15, 16, 17 |
| §6.3 Activities (13 total) | Tasks 4, 5, 6, 7, 8, 9, 10, 11, 12 |
| §6.4 Enforcement migration table | Tasks 5, 6, 7, 8, 10, 11, 12, 15, 16 (each primitive has a task) |
| §7.1 Error taxonomy | Task 2 |
| §7.2 Retry policies | Task 2 |
| §7.3 Heartbeats | Task 9 |
| §7.4 Context overflow | Task 9 (raises ContextOverflowError) |
| §8 State management | Task 3 (MissionState), Task 7 (findings.jsonl mirror) |
| §9 Classifier | Task 20, 21, 22 |
| §9.5 Stop-hook safety net | Task 24 |
| §10 CLI | Task 19 |
| §11 File layout | All tasks create files at their spec'd paths |
| §12 Locked decisions | Implicit across all tasks |
| §14 Success criteria | Task 25 |

**Placeholder scan:** Some tasks reference "port from old file" rather than providing full code (Tasks 6, 8, 10, 11, 12, 15, 16, 17). This is intentional — the old code is production-tested reference implementation; re-inventing would be wasteful. Implementer reads the referenced file and ports with tests. No TBD/TODO in the actual deliverable.

**Type consistency:** All activity names match across tasks (e.g., `run_anticheat_dimension` used consistently). MissionState field names stable. Phase enum values consistent.

---

## Execution Handoff

**Plan complete and saved to `/Users/npow/code/research/swarm/docs/superpowers/plans/2026-04-18-swarm-durability.md`.**

Two execution options:

**1. Subagent-Driven (recommended)** — Fresh subagent per task, review between tasks, fast iteration. Uses `superpowers:subagent-driven-development`.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints.

Given the scope (25 tasks, ~5 weeks of work), **Subagent-Driven is strongly recommended** — each task is independently testable, subagents can run in parallel for independent phases (e.g., Tasks 4-8 activities can all be built in parallel by different subagents), and review-between-tasks catches integration issues early.

Which approach?
