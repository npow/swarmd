"""Tests for the ``detect_scope_shrinking`` Temporal activity.

Per plan Task 12:

    detect_scope_shrinking(context) -> ScopeShrinkingResult

    Pure-computation activity (no LLM, no subprocess). Reads recent
    criterion check results + code diffs and flags scope-shrinking
    behavior (dropped criteria, weakened assertions).

The heuristics are ported from ``specialists/pattern_detector.py``.
Tests drive the activity directly via ``ActivityEnvironment``.
"""

from __future__ import annotations

import pytest
from temporalio.testing import ActivityEnvironment

from swarmd.durable.activities.detect_scope_shrinking import (
    ScopeShrinkingResult,
    detect_scope_shrinking,
)
from swarmd.durable.errors import TerminalError


# ---------------------------------------------------------------------------
# 1. Empty context → no detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_context_returns_not_detected():
    """No criteria, no diffs, no history → nothing to shrink → detected=False."""
    ctx = {
        "criterion_history": [],
        "recent_diffs": "",
        "original_criteria": [],
    }
    env = ActivityEnvironment()
    result = await env.run(detect_scope_shrinking, ctx)

    assert isinstance(result, ScopeShrinkingResult)
    assert result.detected is False
    assert result.finding is None


@pytest.mark.asyncio
async def test_no_diffs_no_dropped_criteria_returns_not_detected():
    """Criteria still all present, no diff evidence → detected=False."""
    ctx = {
        "criterion_history": [
            {"criterion_id": "c1", "status": "pass"},
            {"criterion_id": "c2", "status": "pass"},
        ],
        "recent_diffs": "",
        "original_criteria": ["c1", "c2"],
    }
    env = ActivityEnvironment()
    result = await env.run(detect_scope_shrinking, ctx)

    assert result.detected is False
    assert result.finding is None


# ---------------------------------------------------------------------------
# 2. Criterion dropped from original list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_criterion_dropped_from_history():
    """A criterion from ``original_criteria`` never appears in history →
    detected=True with type=scope_shrinking."""
    ctx = {
        "criterion_history": [
            {"criterion_id": "c1", "status": "pass"},
            # c2 is missing — agent dropped it.
        ],
        "recent_diffs": "",
        "original_criteria": ["c1", "c2"],
    }
    env = ActivityEnvironment()
    result = await env.run(detect_scope_shrinking, ctx)

    assert result.detected is True
    assert result.finding is not None
    assert result.finding["type"] == "scope_shrinking"
    # Rationale should name the dropped criterion.
    assert "c2" in result.rationale


@pytest.mark.asyncio
async def test_multiple_criteria_dropped():
    """Two dropped criteria → both named in rationale."""
    ctx = {
        "criterion_history": [
            {"criterion_id": "c1", "status": "pass"},
        ],
        "recent_diffs": "",
        "original_criteria": ["c1", "c2", "c3"],
    }
    env = ActivityEnvironment()
    result = await env.run(detect_scope_shrinking, ctx)

    assert result.detected is True
    assert "c2" in result.rationale
    assert "c3" in result.rationale


# ---------------------------------------------------------------------------
# 3. Diff removes assertions from a test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diff_removes_assertion_detected():
    """Diff with net-removed ``assert`` lines → detected=True, rationale
    mentions assertions."""
    diff = """\
--- a/test_foo.py
+++ b/test_foo.py
@@ -10,5 +10,2 @@
 def test_something():
     x = foo()
-    assert x == 42
-    assert x.valid
-    assert x.size > 0
     return x
"""
    ctx = {
        "criterion_history": [],
        "recent_diffs": diff,
        "original_criteria": [],
    }
    env = ActivityEnvironment()
    result = await env.run(detect_scope_shrinking, ctx)

    assert result.detected is True
    assert "assert" in result.rationale.lower()


@pytest.mark.asyncio
async def test_diff_list_of_hunks():
    """``recent_diffs`` can be a list of diff hunks (strings)."""
    hunks = [
        """--- a/t.py
+++ b/t.py
@@
-    assert x == 1
-    assert y == 2
""",
    ]
    ctx = {
        "criterion_history": [],
        "recent_diffs": hunks,
        "original_criteria": [],
    }
    env = ActivityEnvironment()
    result = await env.run(detect_scope_shrinking, ctx)

    assert result.detected is True


# ---------------------------------------------------------------------------
# 4. Diff ADDS assertions — reverse signal, NOT scope shrinking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diff_adds_assertions_not_detected():
    """Diff with net-ADDED ``assert`` lines → detected=False."""
    diff = """\
--- a/test_foo.py
+++ b/test_foo.py
@@ -10,2 +10,5 @@
 def test_something():
     x = foo()
+    assert x == 42
+    assert x.valid
+    assert x.size > 0
     return x
"""
    ctx = {
        "criterion_history": [],
        "recent_diffs": diff,
        "original_criteria": [],
    }
    env = ActivityEnvironment()
    result = await env.run(detect_scope_shrinking, ctx)

    assert result.detected is False
    assert result.finding is None


@pytest.mark.asyncio
async def test_diff_equal_add_remove_not_detected():
    """Diff with one remove and one add (net-zero) → NOT detected."""
    diff = """\
--- a/t.py
+++ b/t.py
@@
-    assert x == 42
+    assert x == 43
"""
    ctx = {
        "criterion_history": [],
        "recent_diffs": diff,
        "original_criteria": [],
    }
    env = ActivityEnvironment()
    result = await env.run(detect_scope_shrinking, ctx)

    assert result.detected is False


# ---------------------------------------------------------------------------
# 5. Malformed context → TerminalError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_criterion_history_raises_terminal():
    """``criterion_history`` must be a list. A non-list (e.g. dict) is a
    contract violation → TerminalError, retries won't help."""
    ctx = {
        "criterion_history": {"not": "a list"},
        "recent_diffs": "",
        "original_criteria": [],
    }
    env = ActivityEnvironment()
    with pytest.raises(TerminalError):
        await env.run(detect_scope_shrinking, ctx)


@pytest.mark.asyncio
async def test_malformed_original_criteria_raises_terminal():
    """``original_criteria`` must be a list."""
    ctx = {
        "criterion_history": [],
        "recent_diffs": "",
        "original_criteria": "c1, c2",  # should be list, not string
    }
    env = ActivityEnvironment()
    with pytest.raises(TerminalError):
        await env.run(detect_scope_shrinking, ctx)


@pytest.mark.asyncio
async def test_malformed_recent_diffs_raises_terminal():
    """``recent_diffs`` must be str or list[str]."""
    ctx = {
        "criterion_history": [],
        "recent_diffs": 42,  # numeric — not str or list
        "original_criteria": [],
    }
    env = ActivityEnvironment()
    with pytest.raises(TerminalError):
        await env.run(detect_scope_shrinking, ctx)
