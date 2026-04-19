"""Tests for the swarm MCP server tool implementations.

Mocks ``Anthropic``, ``temporalio.client.Client``, and filesystem lock
operations so tests don't require a running Temporal server or real
anthropic credentials. Patches the ``_*_impl`` functions directly — the
FastMCP decorator wrappers are thin passthroughs and don't need their
own test coverage.

Test inventory (mirrors Task 23 spec):

1. propose_criteria happy path
2. propose_criteria malformed response → TerminalError
3. propose_criteria 429 → TransientError
4. propose_criteria context reaches the prompt
5. launch happy path
6. launch invalid YAML → TerminalError
7. launch no worker → error result, no raise
8. launch workspace locked → TerminalError
9. launch workspace override → mission.workspace overridden
10. launch creates lock file
11. query happy path
12. query workflow not found → TerminalError
13. query Temporal unreachable → TransientError
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml
from anthropic import APIStatusError, AuthenticationError, RateLimitError

from swarmd.durable.errors import AuthError, TerminalError, TransientError
from swarmd.mcp.server import (
    _launch_impl,
    _propose_criteria_impl,
    _query_impl,
)


# ---------------------------------------------------------------------------
# Helpers — anthropic + temporal mocks
# ---------------------------------------------------------------------------


def _make_mock_anthropic(text: str) -> MagicMock:
    """Build an Anthropic() factory that returns ``text`` as content[0].text.

    Mirrors ``tests/test_activities/test_progress_audit.py`` so the
    anthropic mock shape is consistent across the swarm test suite.
    """
    mock_client = MagicMock()
    mock_response = MagicMock()
    content_block = MagicMock()
    content_block.text = text
    mock_response.content = [content_block]
    mock_client.messages.create.return_value = mock_response
    return MagicMock(return_value=mock_client)


def _make_api_status_error(status_code: int, cls=APIStatusError) -> Exception:
    """Build an anthropic APIStatusError (or subclass) with the given status.

    Mirrors the helper in ``test_classifier/test_llm.py`` — the SDK
    constructs these internally, we mimic the shape our code relies on
    (``exc.response.status_code``).
    """
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code=status_code, request=request)
    return cls(message=f"HTTP {status_code}", response=response, body=None)


def _canonical_mission_json(**overrides) -> str:
    """Build the canonical happy-path mission JSON Haiku would return.

    Tests that don't care about the exact content use this default;
    tests that need specific keys can pass ``**overrides`` to merge.
    """
    base = {
        "mission": "Fix the flaky test in test_auth.py",
        "workspace": "/tmp/test-ws",
        "success_criteria": [
            {
                "id": "tests_pass",
                "description": "pytest exits 0",
                "check": "pytest -q",
                "timeout_sec": 60,
            },
            {
                "id": "no_skip_added",
                "description": "no skip markers",
                "check": "! grep -E 'skip' test_auth.py",
                "timeout_sec": 10,
            },
        ],
        "verification": {"run_every_sec": 30, "hold_window_sec": 120},
        "invariants": {"test_count_floor": 1},
        "summary": "Repair the flaky assertion in test_auth.py.",
        "warnings": [],
    }
    base.update(overrides)
    return json.dumps(base)


def _valid_mission_yaml(workspace: str) -> str:
    """Build a valid Mission.yaml text rooted at ``workspace``.

    The workspace must be an absolute path (Mission schema enforces this)
    and must exist so the lock dir creation works. Tests pass a
    tmp_path-derived absolute path.
    """
    data = {
        "mission": "test mission",
        "workspace": workspace,
        "success_criteria": [
            {
                "id": "c1",
                "description": "first",
                "check": "true",
                "timeout_sec": 5,
            }
        ],
        "verification": {"run_every_sec": 1, "hold_window_sec": 2},
    }
    return yaml.safe_dump(data, sort_keys=False)


class _MockTemporalClient:
    """Stand-in for ``temporalio.client.Client``.

    Matches just the surface we exercise: ``start_workflow``,
    ``workflow_service.describe_task_queue``, ``get_workflow_handle``.
    The ``pollers`` attribute controls the worker-present probe; the
    ``not_found`` flag controls whether the handle's query raises a
    not-found-style error.
    """

    def __init__(
        self,
        pollers: int = 1,
        query_result: dict | None = None,
        query_raises: Exception | None = None,
    ) -> None:
        self.pollers = pollers
        self.query_result = query_result or {"phase": "running"}
        self.query_raises = query_raises
        self.namespace = "default"
        self.start_workflow = AsyncMock(return_value=MagicMock())
        # describe_task_queue lives on client.workflow_service since
        # temporalio>=1.26; the high-level shortcut was removed.
        describe_resp = MagicMock()
        describe_resp.pollers = [MagicMock()] * pollers
        self.workflow_service = MagicMock()
        self.workflow_service.describe_task_queue = AsyncMock(
            return_value=describe_resp
        )

    def get_workflow_handle(self, workflow_id: str) -> MagicMock:
        handle = MagicMock()
        if self.query_raises is not None:
            handle.query = AsyncMock(side_effect=self.query_raises)
        else:
            handle.query = AsyncMock(return_value=self.query_result)
        return handle


# ---------------------------------------------------------------------------
# propose_criteria
# ---------------------------------------------------------------------------


class TestProposeCriteriaHappyPath:
    async def test_returns_yaml_and_preview(self):
        """Haiku returns valid JSON → dict with mission_yaml + summary +
        criteria_preview + warnings. The YAML round-trips through yaml
        back to the original dict (minus the metadata keys)."""
        raw = _canonical_mission_json()
        mock_ctor = _make_mock_anthropic(raw)

        with patch("swarmd.mcp.server.Anthropic", mock_ctor):
            result = await _propose_criteria_impl(
                "fix the flaky test in test_auth.py"
            )

        # The wire-level shape
        assert set(result.keys()) == {
            "mission_yaml",
            "summary",
            "criteria_preview",
            "warnings",
        }
        assert isinstance(result["mission_yaml"], str)
        assert "Repair the flaky assertion" in result["summary"]
        assert isinstance(result["criteria_preview"], list)
        assert len(result["criteria_preview"]) == 2

        # Preview strips to just id/description/check
        first = result["criteria_preview"][0]
        assert first["id"] == "tests_pass"
        assert first["check"] == "pytest -q"
        assert "description" in first

        # The YAML round-trips (minus ``summary``/``warnings`` which the
        # impl strips before serialization).
        parsed = yaml.safe_load(result["mission_yaml"])
        assert "summary" not in parsed
        assert "warnings" not in parsed
        assert parsed["mission"] == "Fix the flaky test in test_auth.py"
        assert parsed["workspace"] == "/tmp/test-ws"
        assert len(parsed["success_criteria"]) == 2


class TestProposeCriteriaErrors:
    async def test_malformed_response_raises_terminal(self):
        """Plain prose → TerminalError. Retries won't turn prose into JSON."""
        mock_ctor = _make_mock_anthropic("I think this is a mission")
        with patch("swarmd.mcp.server.Anthropic", mock_ctor):
            with pytest.raises(TerminalError):
                await _propose_criteria_impl("fix X")

    async def test_missing_required_key_raises_terminal(self):
        """JSON with no success_criteria → TerminalError."""
        raw = json.dumps({"mission": "x", "workspace": "/tmp"})
        mock_ctor = _make_mock_anthropic(raw)
        with patch("swarmd.mcp.server.Anthropic", mock_ctor):
            with pytest.raises(TerminalError):
                await _propose_criteria_impl("fix X")

    async def test_empty_criteria_raises_terminal(self):
        """success_criteria must be non-empty."""
        raw = json.dumps(
            {"mission": "x", "workspace": "/tmp", "success_criteria": []}
        )
        mock_ctor = _make_mock_anthropic(raw)
        with patch("swarmd.mcp.server.Anthropic", mock_ctor):
            with pytest.raises(TerminalError):
                await _propose_criteria_impl("fix X")

    async def test_rate_limit_raises_transient(self):
        """anthropic RateLimitError (429) → TransientError."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _make_api_status_error(
            429, cls=RateLimitError
        )
        mock_ctor = MagicMock(return_value=mock_client)
        with patch("swarmd.mcp.server.Anthropic", mock_ctor):
            with pytest.raises(TransientError):
                await _propose_criteria_impl("fix X")

    async def test_auth_error_raises_auth(self):
        """anthropic AuthenticationError (401) → AuthError (TerminalError
        subclass). Ensures bad creds aren't masked as retryable."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = _make_api_status_error(
            401, cls=AuthenticationError
        )
        mock_ctor = MagicMock(return_value=mock_client)
        with patch("swarmd.mcp.server.Anthropic", mock_ctor):
            with pytest.raises(AuthError):
                await _propose_criteria_impl("fix X")


class TestProposeCriteriaContextWiring:
    async def test_context_reaches_prompt(self):
        """Context dict must render into the prompt text Haiku sees."""
        raw = _canonical_mission_json()
        captured: list[str] = []

        def capturing_create(**kwargs):
            captured.append(kwargs["messages"][0]["content"])
            mock_response = MagicMock()
            mock_response.content = [MagicMock()]
            mock_response.content[0].text = raw
            return mock_response

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = capturing_create
        mock_ctor = MagicMock(return_value=mock_client)

        context = {"cwd": "/tmp/project", "recent_file": "auth.py"}
        with patch("swarmd.mcp.server.Anthropic", mock_ctor):
            await _propose_criteria_impl("fix X", context=context)

        assert len(captured) == 1
        assert "/tmp/project" in captured[0]
        assert "auth.py" in captured[0]

    async def test_none_context_renders_as_none_literal(self):
        """context=None should render as ``(none)`` in the prompt."""
        raw = _canonical_mission_json()
        captured: list[str] = []

        def capturing_create(**kwargs):
            captured.append(kwargs["messages"][0]["content"])
            mock_response = MagicMock()
            mock_response.content = [MagicMock()]
            mock_response.content[0].text = raw
            return mock_response

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = capturing_create
        mock_ctor = MagicMock(return_value=mock_client)

        with patch("swarmd.mcp.server.Anthropic", mock_ctor):
            await _propose_criteria_impl("fix X", context=None)

        assert "(none)" in captured[0]


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------


class TestLaunchHappyPath:
    async def test_valid_yaml_starts_workflow(self, tmp_path):
        """Valid YAML → Client.connect called, start_workflow called,
        return dict has workflow_id + task_queue."""
        ws = tmp_path / "ws"
        ws.mkdir()
        yaml_text = _valid_mission_yaml(str(ws))

        mock_client = _MockTemporalClient(pollers=1)
        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            result = await _launch_impl(yaml_text)

        assert "workflow_id" in result
        assert result["workflow_id"].startswith("mission-")
        assert result["task_queue"] == "swarm"
        # start_workflow must have been called with the workflow name,
        # task queue, and an id matching the return value.
        mock_client.start_workflow.assert_awaited_once()
        args, kwargs = mock_client.start_workflow.call_args
        assert args[0] == "MissionWorkflow"
        assert kwargs["task_queue"] == "swarm"
        assert kwargs["id"] == result["workflow_id"]


class TestLaunchValidation:
    async def test_invalid_yaml_raises_terminal(self, tmp_path):
        """Mission missing required fields → TerminalError with pydantic
        details in the message."""
        # This YAML is well-formed but fails Mission.model_validate —
        # no workspace field.
        bad_yaml = yaml.safe_dump({"mission": "x", "success_criteria": []})

        # We don't even get to Temporal — the validation error is raised
        # before connect is attempted. Patch Client.connect so if the
        # code mistakenly tries to connect, we'll see the test hang
        # rather than silently passing.
        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(side_effect=AssertionError("should not connect")),
        ):
            with pytest.raises(TerminalError, match="validation failed"):
                await _launch_impl(bad_yaml)

    async def test_malformed_yaml_raises_terminal(self):
        """Garbled YAML → TerminalError before touching Temporal."""
        with pytest.raises(TerminalError):
            await _launch_impl("this: is: not: yaml: [")


class TestLaunchNoWorker:
    async def test_no_pollers_returns_error_result(self, tmp_path):
        """describe_task_queue reports 0 pollers → error result, not raise."""
        ws = tmp_path / "ws"
        ws.mkdir()
        yaml_text = _valid_mission_yaml(str(ws))

        mock_client = _MockTemporalClient(pollers=0)
        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            result = await _launch_impl(yaml_text)

        assert "error" in result
        assert "no worker" in result["error"]
        assert result["pollers"] == 0
        # start_workflow should NOT have been called in the no-worker path.
        mock_client.start_workflow.assert_not_called()


class TestLaunchLock:
    async def test_existing_lock_raises_terminal(self, tmp_path):
        """A pre-existing .swarm-lock → TerminalError naming the holder."""
        ws = tmp_path / "ws"
        ws.mkdir()
        lock_dir = ws / ".claude"
        lock_dir.mkdir()
        (lock_dir / ".swarm-lock").write_text("other-workflow-id")

        yaml_text = _valid_mission_yaml(str(ws))
        mock_client = _MockTemporalClient(pollers=1)

        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            with pytest.raises(TerminalError, match="locked"):
                await _launch_impl(yaml_text)
        # No workflow should have been started on the locked path.
        mock_client.start_workflow.assert_not_called()

    async def test_creates_lock_file_with_workflow_id(self, tmp_path):
        """On happy path, lock file exists under ws/.claude/.swarm-lock
        and contains the workflow_id."""
        ws = tmp_path / "ws"
        ws.mkdir()
        yaml_text = _valid_mission_yaml(str(ws))

        mock_client = _MockTemporalClient(pollers=1)
        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            result = await _launch_impl(yaml_text)

        lock_path = ws / ".claude" / ".swarm-lock"
        assert lock_path.exists()
        assert lock_path.read_text() == result["workflow_id"]


class TestLaunchWorkspaceOverride:
    async def test_workspace_arg_overrides_mission(self, tmp_path):
        """A ``workspace`` arg takes precedence over mission.workspace."""
        original_ws = tmp_path / "original"
        original_ws.mkdir()
        override_ws = tmp_path / "override"
        override_ws.mkdir()

        yaml_text = _valid_mission_yaml(str(original_ws))

        mock_client = _MockTemporalClient(pollers=1)
        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            await _launch_impl(yaml_text, workspace=str(override_ws))

        # The lock file must land under the override workspace, not the
        # original — the most direct evidence the override took effect.
        override_lock = override_ws / ".claude" / ".swarm-lock"
        assert override_lock.exists()
        original_lock = original_ws / ".claude" / ".swarm-lock"
        assert not original_lock.exists()

        # The mission dict passed to start_workflow also has the override.
        args, kwargs = mock_client.start_workflow.call_args
        mission_dict = kwargs["args"][0]
        assert mission_dict["workspace"] == str(override_ws)


class TestLaunchTemporalUnreachable:
    async def test_connect_failure_raises_transient(self, tmp_path):
        """Client.connect raising → TransientError (retryable)."""
        ws = tmp_path / "ws"
        ws.mkdir()
        yaml_text = _valid_mission_yaml(str(ws))

        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(side_effect=OSError("connection refused")),
        ):
            with pytest.raises(TransientError):
                await _launch_impl(yaml_text)


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------


class TestQueryHappyPath:
    async def test_returns_status_wrapped(self):
        """get_status → {"status": <dict>} — keep the wrapper the spec
        requires so clients can add sibling fields later (findings, etc)."""
        expected = {
            "phase": "running",
            "findings_count": 3,
            "criteria_state": {"c1": {"pass": True, "streak_sec": 12}},
        }
        mock_client = _MockTemporalClient(query_result=expected)
        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            result = await _query_impl("mission-abc123")

        assert result == {"status": expected}


class TestQueryErrors:
    async def test_workflow_not_found_raises_terminal(self):
        """An error whose message contains 'not found' → TerminalError.

        The helper ``_is_not_found_error`` is heuristic by design (SDK
        versions vary); here we give it an exception whose message
        matches the literal phrase."""
        mock_client = _MockTemporalClient(
            query_raises=RuntimeError("workflow not found"),
        )
        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            with pytest.raises(TerminalError, match="not found"):
                await _query_impl("mission-missing")

    async def test_temporal_unreachable_raises_transient(self):
        """Client.connect raising → TransientError."""
        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(side_effect=OSError("connection refused")),
        ):
            with pytest.raises(TransientError):
                await _query_impl("mission-abc")

    async def test_other_query_failure_raises_transient(self):
        """An exception whose message does NOT match 'not found' → treated
        as transient (network blip, worker startup race, etc.)."""
        mock_client = _MockTemporalClient(
            query_raises=RuntimeError("deadline exceeded"),
        )
        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            with pytest.raises(TransientError):
                await _query_impl("mission-abc")
