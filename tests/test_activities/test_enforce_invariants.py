"""Tests for the ``enforce_invariants`` Temporal activity.

Per plan Task 6 and spec §6.3 (the ``enforce_invariants`` row):

    enforce_invariants(workspace, invariants) → {findings: list[dict]}

    Short (seconds). Observational; safe to re-run. Idempotent given the same
    workspace state.

These tests drive the activity through ``temporalio.testing.ActivityEnvironment``
so we do not need a running Temporal server.

The activity is a port of the ``enforce_invariants`` function in
``specialists/success_verifier.py`` lines 167-318. Semantics preserved:

* ``no_mock`` — directories under which any ``unittest.mock`` / ``mock.patch``
  usage flags a finding.
* ``test_count_floor`` — total ``def test_...`` count across the workspace
  must be ≥ floor.
* ``assertion_count_floor`` — per-file floor on number of ``assert`` statements
  (including ``self.assert*`` method calls).
* ``allowed_deps`` — installed ``pip freeze`` packages must be a subset of
  the allowlist.

Unlike the original, this activity:

* Does not depend on the old ``Finding`` schema. Each finding is a plain dict
  with ``type``, ``subtype``, ``severity``, ``verdict``.
* Always uses the ``meta`` type and ``invariant_<name>`` subtype so the
  workflow layer can route them.
* Takes ``workspace`` and ``invariants`` explicitly — no hidden globals.
* ``_pip_freeze`` is split out so tests can mock the subprocess hop without
  creating a real venv per run.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest
from temporalio.testing import ActivityEnvironment

from swarmd.durable.activities.enforce_invariants import (
    InvariantsResult,
    enforce_invariants,
)
from swarmd.schemas.mission import Invariants

# The activity module and its ``enforce_invariants`` function share a dotted
# name: the ``__init__.py`` for ``swarm.durable.activities`` re-exports the
# function, which shadows the submodule on attribute lookup. That breaks
# ``patch("swarmd.durable.activities.enforce_invariants._pip_freeze")`` —
# mock resolves the dotted path to the FUNCTION object and then fails because
# functions have no ``_pip_freeze`` attribute. Reaching into ``sys.modules``
# sidesteps the shadow: the real module object is still registered there.
_MODULE = sys.modules["swarmd.durable.activities.enforce_invariants"]


@pytest.mark.asyncio
async def test_empty_invariants_returns_no_findings(tmp_path):
    """A default ``Invariants()`` with all optional fields absent must be a
    no-op — no checks run, no findings. This guards against a future refactor
    accidentally making a check fire on default config."""
    inv = Invariants()

    env = ActivityEnvironment()
    result = await env.run(enforce_invariants, str(tmp_path), inv)

    assert isinstance(result, InvariantsResult)
    assert result.findings == []


@pytest.mark.asyncio
async def test_no_mock_passes_on_clean_file(tmp_path):
    """A file under a no_mock-protected directory that contains no
    ``unittest.mock`` / ``mock.patch`` references must not produce a finding."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "pure.py").write_text(
        "def add(a, b):\n    return a + b\n"
    )
    inv = Invariants(no_mock=["src"])

    env = ActivityEnvironment()
    result = await env.run(enforce_invariants, str(tmp_path), inv)

    assert result.findings == []


@pytest.mark.asyncio
async def test_no_mock_flags_mock_usage(tmp_path):
    """A file under a no_mock directory that imports ``unittest.mock`` must
    produce exactly one finding with subtype ``invariant_no_mock`` and a
    verdict that identifies the offending file."""
    (tmp_path / "src").mkdir()
    tainted = tmp_path / "src" / "tainted.py"
    tainted.write_text(
        "from unittest.mock import patch\n"
        "def f():\n    return patch\n"
    )
    inv = Invariants(no_mock=["src"])

    env = ActivityEnvironment()
    result = await env.run(enforce_invariants, str(tmp_path), inv)

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding["type"] == "meta"
    assert finding["subtype"] == "invariant_no_mock"
    assert finding["severity"] == "critical"
    assert "tainted.py" in finding["verdict"]


@pytest.mark.asyncio
async def test_test_count_floor_blocks_below_min(tmp_path):
    """Total ``def test_...`` count across the workspace is below the floor →
    exactly one finding with subtype ``invariant_test_count_floor``."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_a():\n    pass\n")
    inv = Invariants(test_count_floor=3)

    env = ActivityEnvironment()
    result = await env.run(enforce_invariants, str(tmp_path), inv)

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding["subtype"] == "invariant_test_count_floor"
    assert finding["severity"] == "critical"
    # Verdict should carry the actual count and floor so operators can read it.
    assert "1" in finding["verdict"]
    assert "3" in finding["verdict"]


@pytest.mark.asyncio
async def test_test_count_floor_passes_when_met(tmp_path):
    """If the count meets or exceeds the floor, no finding."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_a():\n    pass\n"
        "def test_b():\n    pass\n"
        "def test_c():\n    pass\n"
    )
    inv = Invariants(test_count_floor=3)

    env = ActivityEnvironment()
    result = await env.run(enforce_invariants, str(tmp_path), inv)

    assert result.findings == []


@pytest.mark.asyncio
async def test_assertion_count_floor_flags_weak_file(tmp_path):
    """A protected test file below its per-file assertion floor produces one
    finding with subtype ``invariant_assertion_count_floor``."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_y.py").write_text(
        "def test_a():\n    assert True\n"
    )
    inv = Invariants(assertion_count_floor={"tests/test_y.py": 3})

    env = ActivityEnvironment()
    result = await env.run(enforce_invariants, str(tmp_path), inv)

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding["subtype"] == "invariant_assertion_count_floor"
    assert finding["severity"] == "critical"
    assert "tests/test_y.py" in finding["verdict"]


@pytest.mark.asyncio
async def test_allowed_deps_flags_disallowed(tmp_path):
    """An installed package not present in the allowlist must surface as a
    finding with subtype ``invariant_allowed_deps`` and a verdict naming the
    offending dep. ``_pip_freeze`` is patched so the test does not depend on
    the ambient environment."""
    inv = Invariants(allowed_deps=["good-pkg"])

    env = ActivityEnvironment()
    with patch.object(_MODULE, "_pip_freeze") as mock_freeze:
        mock_freeze.return_value = ["good-pkg==1.0", "bad-pkg==2.0"]
        result = await env.run(enforce_invariants, str(tmp_path), inv)

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding["subtype"] == "invariant_allowed_deps"
    assert finding["severity"] == "critical"
    assert "bad-pkg" in finding["verdict"]
