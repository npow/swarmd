"""Unit tests for ``swarm.durable.worker`` — the Task 18 worker daemon.

These tests exercise the registration manifests (``WORKFLOWS`` /
``ACTIVITIES``), the ``main`` coroutine's connection + Worker wiring, and
the ``cli_main`` synchronous entry point. None of them spin up a real
Temporal server — ``Client.connect`` and the ``Worker`` context manager
are mocked so the tests run in milliseconds.

The existing workflow integration tests under
``tests/test_workflows/test_mission_workflow.py`` already drive a real
``WorkflowEnvironment`` with these workflow classes + activity functions
registered, so the "does this actually work end to end?" check is covered
elsewhere. These tests focus on the bootstrap contract: the right types
land in the right arg slots, env vars are respected, and shutdown signals
don't crash the process.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from temporalio.activity import _Definition as _ActivityDefinition
from temporalio.contrib.pydantic import pydantic_data_converter

from swarmd.durable import worker as worker_mod
from swarmd.durable.specialists import (
    LLMCriticWorkflow,
    PatternDetectorWorkflow,
    ResourceMonitorWorkflow,
)
from swarmd.durable.worker import (
    ACTIVITIES,
    DEFAULT_HOST,
    DEFAULT_TASK_QUEUE,
    WORKFLOWS,
    cli_main,
    main,
)
from swarmd.durable.workflow import MissionWorkflow


# ---------------------------------------------------------------------------
# Registration manifests
# ---------------------------------------------------------------------------


class TestWorkflowRegistration:
    """The ``WORKFLOWS`` constant covers all four @workflow.defn classes."""

    def test_contains_four_workflows(self) -> None:
        assert len(WORKFLOWS) == 4

    def test_contains_expected_classes(self) -> None:
        # Use a set of names (not identity) because re-imports in other
        # tests can shuffle object identity when conftest.py evicts cached
        # ``swarm.*`` modules from ``sys.modules``.
        names = {cls.__name__ for cls in WORKFLOWS}
        assert names == {
            "MissionWorkflow",
            "PatternDetectorWorkflow",
            "LLMCriticWorkflow",
            "ResourceMonitorWorkflow",
        }

    def test_all_are_workflow_defs(self) -> None:
        # ``@workflow.defn`` stamps the class with ``__temporal_workflow_definition``.
        # Assert it's present on every registered class so we catch the case
        # where somebody mistakenly lists a plain class.
        for cls in WORKFLOWS:
            assert hasattr(
                cls, "__temporal_workflow_definition"
            ), f"{cls.__name__} is not a @workflow.defn class"


class TestActivityRegistration:
    """The ``ACTIVITIES`` constant covers every worker-side activity."""

    EXPECTED_ACTIVITY_NAMES = frozenset(
        {
            "check_criterion",
            "verify_tamper",
            "enforce_invariants",
            "emit_finding",
            "intervention_judge",
            "completion_judge",
            "run_claude_cli",
            "progress_audit",
            "goal_drift_check",
            "run_anticheat_dimension",
            "spawn_subagent",
            "restart_subprocess",
            "detect_scope_shrinking",
            "read_recent_events",
            "check_zombies",
            "check_memory",
            "check_disk",
        }
    )

    def test_contains_seventeen_activities(self) -> None:
        assert len(ACTIVITIES) == 17

    def test_all_are_callable(self) -> None:
        for a in ACTIVITIES:
            assert callable(a), f"{a!r} is not callable"

    def test_all_have_activity_defn(self) -> None:
        """Each registered function must carry an ``@activity.defn`` mark.

        Without the decorator the Worker would silently refuse to route to
        the function; Temporal raises only at runtime when a workflow tries
        to execute an unregistered activity. Testing at registration time
        catches the error one layer earlier.
        """
        for a in ACTIVITIES:
            defn = _ActivityDefinition.from_callable(a)
            assert (
                defn is not None
            ), f"{a.__name__} is not decorated with @activity.defn"

    def test_activity_names_match_workflow_contract(self) -> None:
        """The activity.defn ``name=`` strings match the known workflow callers.

        ``MissionWorkflow._verifier_cycle`` and the three observer specialists
        invoke activities by string name (``workflow.execute_activity("x")``).
        If the names diverge from this set, the workflow raises a
        NotFoundError at runtime — caught here as an AssertionError instead.
        """
        names = {
            _ActivityDefinition.from_callable(a).name for a in ACTIVITIES
        }
        assert names == self.EXPECTED_ACTIVITY_NAMES

    def test_no_duplicate_activities(self) -> None:
        """Each activity is registered exactly once.

        Temporal would raise on Worker startup if two @activity.defn calls
        shared a name, but catching it as a static check keeps the error
        message pointed at the right place (the ACTIVITIES list, not the
        Worker constructor).
        """
        seen_ids = set()
        for a in ACTIVITIES:
            assert id(a) not in seen_ids, f"duplicate activity: {a.__name__}"
            seen_ids.add(id(a))


# ---------------------------------------------------------------------------
# main() — coroutine entry point
# ---------------------------------------------------------------------------


class TestMainConnectsAndStartsWorker:
    """``main`` calls Client.connect + constructs a Worker correctly."""

    @pytest.mark.asyncio
    async def test_connect_uses_pydantic_converter(self) -> None:
        """``main`` passes ``pydantic_data_converter`` to ``Client.connect``.

        This is load-bearing: the workflow tests assume pydantic models
        round-trip, and swapping the converter breaks that silently.
        """
        mock_client = MagicMock()

        # Patch the Worker class so entering the context manager is a no-op
        # — we don't actually want to start a worker or block on the
        # asyncio.Event() in main().
        mock_worker_instance = MagicMock()
        mock_worker_instance.__aenter__ = AsyncMock(return_value=mock_worker_instance)
        mock_worker_instance.__aexit__ = AsyncMock(return_value=None)
        mock_worker_ctor = MagicMock(return_value=mock_worker_instance)

        with patch.object(
            worker_mod.Client, "connect", AsyncMock(return_value=mock_client)
        ) as mock_connect, patch.object(
            worker_mod, "Worker", mock_worker_ctor
        ), patch.object(
            # Make the ``await asyncio.Event().wait()`` return immediately
            # by cancelling the underlying wait. Easier: patch
            # ``asyncio.Event`` to return an event that's pre-set.
            worker_mod.asyncio,
            "Event",
            _PreSetEvent,
        ):
            await main(host="my.temporal:7233", task_queue="custom-queue")

        mock_connect.assert_awaited_once()
        # Positional arg (host) and kw data_converter.
        args, kwargs = mock_connect.await_args
        assert args == ("my.temporal:7233",)
        assert kwargs["data_converter"] is pydantic_data_converter

    @pytest.mark.asyncio
    async def test_worker_constructed_with_workflows_and_activities(self) -> None:
        mock_client = MagicMock()
        mock_worker_instance = MagicMock()
        mock_worker_instance.__aenter__ = AsyncMock(return_value=mock_worker_instance)
        mock_worker_instance.__aexit__ = AsyncMock(return_value=None)
        mock_worker_ctor = MagicMock(return_value=mock_worker_instance)

        with patch.object(
            worker_mod.Client, "connect", AsyncMock(return_value=mock_client)
        ), patch.object(
            worker_mod, "Worker", mock_worker_ctor
        ), patch.object(
            worker_mod.asyncio, "Event", _PreSetEvent
        ):
            await main(host="h:1", task_queue="q")

        mock_worker_ctor.assert_called_once()
        args, kwargs = mock_worker_ctor.call_args
        # Positional arg[0] is the client.
        assert args == (mock_client,)
        assert kwargs["task_queue"] == "q"
        assert kwargs["workflows"] == WORKFLOWS
        assert kwargs["activities"] == ACTIVITIES
        assert "max_concurrent_workflow_tasks" in kwargs
        assert "max_concurrent_activities" in kwargs

    @pytest.mark.asyncio
    async def test_defaults_used_when_args_omitted(self) -> None:
        """``main()`` with no args uses DEFAULT_HOST / DEFAULT_TASK_QUEUE.

        The CLI wiring in ``swarm.cli.worker`` calls ``main()`` with no
        positional args, so the defaults must match the constants.
        """
        mock_client = MagicMock()
        mock_worker_instance = MagicMock()
        mock_worker_instance.__aenter__ = AsyncMock(return_value=mock_worker_instance)
        mock_worker_instance.__aexit__ = AsyncMock(return_value=None)
        mock_worker_ctor = MagicMock(return_value=mock_worker_instance)

        with patch.object(
            worker_mod.Client, "connect", AsyncMock(return_value=mock_client)
        ) as mock_connect, patch.object(
            worker_mod, "Worker", mock_worker_ctor
        ), patch.object(
            worker_mod.asyncio, "Event", _PreSetEvent
        ):
            await main()

        args, _kwargs = mock_connect.await_args
        assert args == (DEFAULT_HOST,)

        _args, kwargs = mock_worker_ctor.call_args
        assert kwargs["task_queue"] == DEFAULT_TASK_QUEUE


# ---------------------------------------------------------------------------
# cli_main() — synchronous entry point
# ---------------------------------------------------------------------------


class TestCliMain:
    """``cli_main`` is the ``python -m swarm.durable.worker`` entry."""

    def test_respects_env_vars(self, monkeypatch) -> None:
        """TEMPORAL_ADDRESS / SWARM_TASK_QUEUE override the defaults."""
        monkeypatch.setenv("TEMPORAL_ADDRESS", "env.temporal:9999")
        monkeypatch.setenv("SWARM_TASK_QUEUE", "env-queue")

        captured: dict = {}

        async def fake_main(
            host: str = DEFAULT_HOST,
            task_queue: str = DEFAULT_TASK_QUEUE,
            **_kwargs: object,
        ) -> None:
            captured["host"] = host
            captured["task_queue"] = task_queue

        with patch.object(worker_mod, "main", fake_main):
            cli_main()

        assert captured == {
            "host": "env.temporal:9999",
            "task_queue": "env-queue",
        }

    def test_uses_defaults_when_env_unset(self, monkeypatch) -> None:
        """Missing env vars → fall back to DEFAULT_HOST / DEFAULT_TASK_QUEUE."""
        monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
        monkeypatch.delenv("SWARM_TASK_QUEUE", raising=False)

        captured: dict = {}

        async def fake_main(
            host: str = DEFAULT_HOST,
            task_queue: str = DEFAULT_TASK_QUEUE,
            **_kwargs: object,
        ) -> None:
            captured["host"] = host
            captured["task_queue"] = task_queue

        with patch.object(worker_mod, "main", fake_main):
            cli_main()

        assert captured == {"host": DEFAULT_HOST, "task_queue": DEFAULT_TASK_QUEUE}

    def test_handles_keyboard_interrupt(self, monkeypatch) -> None:
        """Ctrl+C surfaces as a clean exit with a log line, not a traceback.

        We patch ``asyncio.run`` to raise ``KeyboardInterrupt`` — that's
        what CPython raises in the main thread when the SIGINT handler
        fires during ``run``'s event-loop pump.
        """
        # Need a patched main so cli_main picks env-vars cleanly and we don't
        # accidentally try to construct a real Client on the way in.
        monkeypatch.delenv("TEMPORAL_ADDRESS", raising=False)
        monkeypatch.delenv("SWARM_TASK_QUEUE", raising=False)

        def raise_kbint(_coro) -> None:
            # Close the coroutine so we don't get a "was never awaited"
            # warning from asyncio.
            if hasattr(_coro, "close"):
                _coro.close()
            raise KeyboardInterrupt

        with patch.object(worker_mod.asyncio, "run", raise_kbint):
            # Should NOT raise. The test asserts by simply returning.
            cli_main()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PreSetEvent:
    """Stand-in for ``asyncio.Event`` whose ``wait()`` returns immediately.

    Used in the ``main()`` tests so the "block forever" line doesn't hang
    the test. Real ``asyncio.Event`` blocks until ``set()`` is called; our
    tests care about what happened *before* the block, so we short-circuit.
    """

    def __init__(self) -> None:
        pass

    async def wait(self) -> None:  # pragma: no cover - trivial
        return None
