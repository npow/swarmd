"""Shared fixtures + pytest config for end-to-end durability tests (Task 25).

These tests exercise spec §14 success criteria for the durable swarm redesign.
They are SLOW (seconds to minutes per test) and REQUIRE a Temporal server —
either the bundled time-skipping dev server (``start_time_skipping``) or a
local Temporal CLI instance exposed via ``$TEMPORAL_ADDR``.

Pytest marker / gating contract
-------------------------------

Every test in this package is implicitly marked ``integration`` via the
``pytestmark`` guard inside ``test_durability.py``. By default these tests
are SKIPPED unless the caller opts in via EITHER:

* ``pytest --run-integration``  (explicit opt-in flag), OR
* ``TEMPORAL_ADDR=localhost:7233 pytest``  (env var presence is the signal)

Plain ``pytest tests/`` MUST NOT run them — they take minutes and can hang
if Temporal is unreachable. The skip path is exercised in the regression
verification phase of Task 25 by running the tests without the flag/env.

Fixtures
--------

``temporal_env`` starts a ``WorkflowEnvironment.start_time_skipping`` with
the ``pydantic_data_converter`` so ``Mission`` / ``MissionState`` / etc.
round-trip as pydantic models inside the workflow (matches production and
matches the unit test harness in ``tests/test_workflows/conftest.py``).

Session-scoped so all five tests in the suite share ONE environment — the
time-skipping test server is expensive to start (~2-3s each) and the tests
are independent in Temporal history terms (each runs its own workflow on
its own task queue).
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment


# ---------------------------------------------------------------------------
# Pytest plumbing — marker declaration + opt-in gate
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--run-integration`` CLI flag.

    Without this flag, integration tests are skipped at collection time
    via the ``pytest_collection_modifyitems`` hook below.
    """
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help=(
            "Run end-to-end durability integration tests (slow; require "
            "a Temporal dev server). Implied by TEMPORAL_ADDR env var."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    """Declare the ``integration`` marker so ``pytest --strict-markers`` is
    happy and ``-v`` output carries the label."""
    config.addinivalue_line(
        "markers",
        "integration: end-to-end durability test (slow; requires Temporal)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip ``integration`` tests unless the caller opted in.

    Opt-in is EITHER ``--run-integration`` flag OR the ``TEMPORAL_ADDR``
    env var. The env-var path lets CI configure opt-in once at the job
    level without editing per-invocation pytest args.
    """
    run_integration = (
        config.getoption("--run-integration", default=False)
        or bool(os.environ.get("TEMPORAL_ADDR"))
    )
    if run_integration:
        return

    skip_marker = pytest.mark.skip(
        reason=(
            "integration test; pass --run-integration or set TEMPORAL_ADDR "
            "to enable."
        )
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_marker)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="function")
async def temporal_env():
    """Start a time-skipping WorkflowEnvironment with the pydantic converter.

    Function-scoped (NOT session-scoped): tests register activities on
    Workers with ``@activity.defn(name=...)`` and the Python activity
    registry complains if the same activity name is registered twice in
    the same process. Fresh env + fresh worker per test avoids the
    double-registration pitfall at the cost of a ~2-3s env start per test.

    The pydantic data converter is load-bearing: the workflow code inside
    ``MissionWorkflow.run`` relies on pydantic attribute access on the
    ``Mission`` arg, and without this converter the workflow receives a
    plain dict that ``_coerce_mission`` has to rebuild — works, but
    brittle if the mission schema grows.
    """
    env = await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter,
    )
    try:
        yield env
    finally:
        await env.shutdown()
