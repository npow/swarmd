"""Tests for the swarm CLI (``swarm.cli``).

Uses ``click.testing.CliRunner`` for command invocation. Temporal,
anthropic, and filesystem lock operations are mocked so tests don't
require a running Temporal server or real anthropic credentials.

Test inventory (mirrors Task 19 spec):

1.  launch happy path — valid YAML, mocked client → stdout has
    ``workflow_id=``, exit 0.
2.  launch invalid YAML → exit 1, stderr has validation error.
3.  launch Temporal unreachable → exit 1.
4.  launch no worker → exit 1 with "no worker running" message.
5.  launch workspace locked → exit 1.
6.  status happy path — mocked query returns dict → pretty JSON.
7.  status workflow not found → exit 1.
8.  abort sends default ``"user-abort"`` reason.
9.  abort custom ``--reason "test"`` → signal called with "test".
10. worker stub invokes ``swarm.durable.worker.main``.
11. health all-pass — mock 3 successful checks → exit 0, 3 PASS rows.
12. health Temporal fail — ``Client.connect`` raises → FAIL row.
13. health worker fail — describe_task_queue empty pollers → FAIL row.
14. findings no session → "no findings yet".
15. findings with --tail — 100 jsonl lines → only tail N printed.
16. logs no file → "no logs yet".

Async commands use ``asyncio.run`` under the hood; the runner invokes
the click callback synchronously so patches on ``Client.connect`` etc.
work transparently.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Shared mocks
# ---------------------------------------------------------------------------


def _make_mock_temporal_client(
    pollers: int = 1,
    query_result: dict | None = None,
    query_raises: Exception | None = None,
    signal_raises: Exception | None = None,
) -> MagicMock:
    """Build a mock ``temporalio.client.Client`` suitable for CliRunner tests.

    Covers all the surfaces the CLI touches:

    * ``describe_task_queue`` → returns a ``MagicMock`` whose ``pollers``
      attribute has the requested length.
    * ``start_workflow`` → AsyncMock returning a handle with ``id``.
    * ``get_workflow_handle(id)`` → MagicMock whose ``query`` and
      ``signal`` methods are AsyncMocks (configurable to raise).
    """
    client = MagicMock()
    client.start_workflow = AsyncMock(
        return_value=MagicMock(id="mission-abc123")
    )

    describe_resp = MagicMock()
    describe_resp.pollers = [MagicMock() for _ in range(pollers)]
    client.describe_task_queue = AsyncMock(return_value=describe_resp)

    handle = MagicMock()
    if query_raises is not None:
        handle.query = AsyncMock(side_effect=query_raises)
    else:
        handle.query = AsyncMock(return_value=query_result or {"phase": "running"})

    if signal_raises is not None:
        handle.signal = AsyncMock(side_effect=signal_raises)
    else:
        handle.signal = AsyncMock()

    client.get_workflow_handle = MagicMock(return_value=handle)
    # Expose the handle so tests can assert on signal/query call args.
    client._test_handle = handle  # type: ignore[attr-defined]
    return client


def _valid_mission_yaml_text(workspace: str) -> str:
    """Build a valid Mission.yaml text rooted at ``workspace``.

    The workspace must be an absolute path (pydantic validator in
    Mission enforces this). Tests pass a tmp_path-derived absolute path.
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


def _write_mission_file(tmp_path: Path, workspace: Path) -> Path:
    """Write mission.yaml to ``tmp_path`` and return the path."""
    mission_path = tmp_path / "mission.yaml"
    mission_path.write_text(_valid_mission_yaml_text(str(workspace)))
    return mission_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    """Standard CliRunner — click 8.3+ separates stdout/stderr by default."""
    return CliRunner()


@pytest.fixture
def cli_module():
    """Import the CLI lazily so test collection doesn't pay for anthropic."""
    from swarmd import cli as cli_mod

    return cli_mod


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------


class TestLaunchHappyPath:
    def test_launch_prints_workflow_id_and_exits_zero(
        self, runner, cli_module, tmp_path
    ):
        """Valid YAML + mocked client → stdout starts with workflow_id=."""
        ws = tmp_path / "ws"
        ws.mkdir()
        mission_path = _write_mission_file(tmp_path, ws)

        mock_client = _make_mock_temporal_client(pollers=1)

        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            result = runner.invoke(cli_module.cli, ["launch", str(mission_path)])

        assert result.exit_code == 0, result.output + (result.stderr or "")
        assert "workflow_id=" in result.output
        # The ``mission-`` prefix comes from _launch_mission's uuid4 id gen.
        assert "mission-" in result.output


class TestLaunchInvalidYaml:
    def test_invalid_yaml_exits_one_with_validation_error(
        self, runner, cli_module, tmp_path
    ):
        """Mission missing required fields → exit 1 with error in stderr."""
        mission_path = tmp_path / "mission.yaml"
        # Missing ``workspace`` — pydantic will reject.
        mission_path.write_text(
            yaml.safe_dump({"mission": "x", "success_criteria": []})
        )

        # No Temporal interaction should happen on validation failure,
        # but patch connect so a bug that reaches it would loudly fail
        # rather than pass silently.
        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(side_effect=AssertionError("should not connect")),
        ):
            result = runner.invoke(cli_module.cli, ["launch", str(mission_path)])

        assert result.exit_code == 1
        stderr = result.stderr or ""
        assert "validation failed" in stderr or "workspace" in stderr


class TestLaunchTemporalUnreachable:
    def test_connect_refused_exits_one(
        self, runner, cli_module, tmp_path
    ):
        """Client.connect raising → exit 1."""
        ws = tmp_path / "ws"
        ws.mkdir()
        mission_path = _write_mission_file(tmp_path, ws)

        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(side_effect=OSError("connection refused")),
        ):
            result = runner.invoke(cli_module.cli, ["launch", str(mission_path)])

        assert result.exit_code == 1
        assert "error" in (result.stderr or "")


class TestLaunchNoWorker:
    def test_zero_pollers_prints_actionable_message(
        self, runner, cli_module, tmp_path
    ):
        """describe_task_queue pollers=0 → exit 1 with 'no worker running'."""
        ws = tmp_path / "ws"
        ws.mkdir()
        mission_path = _write_mission_file(tmp_path, ws)

        mock_client = _make_mock_temporal_client(pollers=0)

        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            result = runner.invoke(cli_module.cli, ["launch", str(mission_path)])

        assert result.exit_code == 1
        assert "no worker running" in (result.stderr or "")


class TestLaunchWorkspaceLocked:
    def test_pre_existing_lock_exits_one(
        self, runner, cli_module, tmp_path
    ):
        """.swarm-lock already present → TerminalError → exit 1."""
        ws = tmp_path / "ws"
        ws.mkdir()
        lock_dir = ws / ".claude"
        lock_dir.mkdir()
        (lock_dir / ".swarm-lock").write_text("other-workflow")

        mission_path = _write_mission_file(tmp_path, ws)

        mock_client = _make_mock_temporal_client(pollers=1)

        with patch(
            "swarmd.mcp.server.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            result = runner.invoke(cli_module.cli, ["launch", str(mission_path)])

        assert result.exit_code == 1
        assert "locked" in (result.stderr or "")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestStatusHappyPath:
    def test_prints_pretty_json_and_exits_zero(self, runner, cli_module):
        """get_status returns dict → json.dumps(..., indent=2) in stdout."""
        expected = {
            "phase": "running",
            "findings_count": 3,
            "criteria_state": {"c1": {"pass": True, "streak_sec": 12}},
        }
        mock_client = _make_mock_temporal_client(query_result=expected)

        with patch(
            "swarmd.cli.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            result = runner.invoke(
                cli_module.cli, ["status", "mission-abc123"]
            )

        assert result.exit_code == 0, result.output + (result.stderr or "")
        # json.dumps indent=2 — stdout has the phase and the counts.
        assert '"phase": "running"' in result.output
        assert '"findings_count": 3' in result.output


class TestStatusNotFound:
    def test_not_found_exits_one(self, runner, cli_module):
        """Query raising "workflow not found" → exit 1."""
        mock_client = _make_mock_temporal_client(
            query_raises=RuntimeError("workflow not found")
        )

        with patch(
            "swarmd.cli.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            result = runner.invoke(
                cli_module.cli, ["status", "mission-missing"]
            )

        assert result.exit_code == 1
        assert "not found" in (result.stderr or "")


# ---------------------------------------------------------------------------
# abort
# ---------------------------------------------------------------------------


class TestAbortDefaultReason:
    def test_default_reason_is_user_abort(self, runner, cli_module):
        """No --reason → signal called with 'user-abort'."""
        mock_client = _make_mock_temporal_client()

        with patch(
            "swarmd.cli.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            result = runner.invoke(cli_module.cli, ["abort", "mission-abc"])

        assert result.exit_code == 0, result.output + (result.stderr or "")
        assert "abort signal sent" in result.output
        handle = mock_client._test_handle
        handle.signal.assert_awaited_once_with("abort", "user-abort")


class TestAbortCustomReason:
    def test_reason_is_forwarded_to_signal(self, runner, cli_module):
        """--reason test → signal called with 'test'."""
        mock_client = _make_mock_temporal_client()

        with patch(
            "swarmd.cli.Client.connect",
            AsyncMock(return_value=mock_client),
        ):
            result = runner.invoke(
                cli_module.cli,
                ["abort", "mission-abc", "--reason", "test"],
            )

        assert result.exit_code == 0
        handle = mock_client._test_handle
        handle.signal.assert_awaited_once_with("abort", "test")


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


class TestWorkerCommand:
    def test_invokes_worker_main(self, runner, cli_module):
        """swarm worker → calls swarm.durable.worker.main via asyncio.run."""
        # ``main`` is an async def; the CLI does ``asyncio.run(main())``.
        # We replace it with an AsyncMock so the runner doesn't actually
        # launch a worker.
        with patch("swarmd.durable.worker.main", AsyncMock()) as mock_main:
            result = runner.invoke(cli_module.cli, ["worker"])

        assert result.exit_code == 0, result.output + (result.stderr or "")
        mock_main.assert_awaited_once()


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


class TestHealthAllPass:
    def test_three_pass_rows(self, runner, cli_module):
        """All three checks succeed → exit 0, 3 PASS lines."""
        mock_client = _make_mock_temporal_client(pollers=2)

        # anthropic probe — patch Anthropic constructor to return a
        # client whose messages.create returns normally.
        mock_anthropic_instance = MagicMock()
        mock_anthropic_instance.messages.create = MagicMock(
            return_value=MagicMock()
        )
        mock_anthropic_ctor = MagicMock(return_value=mock_anthropic_instance)

        with patch(
            "swarmd.cli.Client.connect",
            AsyncMock(return_value=mock_client),
        ), patch("anthropic.Anthropic", mock_anthropic_ctor):
            result = runner.invoke(cli_module.cli, ["health"])

        assert result.exit_code == 0, result.output + (result.stderr or "")
        assert result.output.count("PASS") == 3
        assert "FAIL" not in result.output


class TestHealthTemporalFail:
    def test_temporal_connect_raises(self, runner, cli_module):
        """Client.connect raises → Temporal row FAIL, others still run."""
        mock_anthropic_instance = MagicMock()
        mock_anthropic_instance.messages.create = MagicMock(
            return_value=MagicMock()
        )
        mock_anthropic_ctor = MagicMock(return_value=mock_anthropic_instance)

        with patch(
            "swarmd.cli.Client.connect",
            AsyncMock(side_effect=OSError("connection refused")),
        ), patch("anthropic.Anthropic", mock_anthropic_ctor):
            result = runner.invoke(cli_module.cli, ["health"])

        # Temporal FAIL, worker FAIL (dependent), classifier PASS.
        assert "FAIL" in result.output
        assert "Temporal reachable" in result.output
        assert "Worker liveness" in result.output
        assert "Classifier API" in result.output
        # Classifier row should still pass even though Temporal is down.
        assert "Classifier API" in result.output
        # Extract the line for the classifier row to verify PASS.
        classifier_line = next(
            line for line in result.output.splitlines()
            if "Classifier API" in line
        )
        assert "PASS" in classifier_line


class TestHealthWorkerFail:
    def test_zero_pollers_fails_worker_row(self, runner, cli_module):
        """describe_task_queue returns no pollers → worker row FAIL."""
        mock_client = _make_mock_temporal_client(pollers=0)

        mock_anthropic_instance = MagicMock()
        mock_anthropic_instance.messages.create = MagicMock(
            return_value=MagicMock()
        )
        mock_anthropic_ctor = MagicMock(return_value=mock_anthropic_instance)

        with patch(
            "swarmd.cli.Client.connect",
            AsyncMock(return_value=mock_client),
        ), patch("anthropic.Anthropic", mock_anthropic_ctor):
            result = runner.invoke(cli_module.cli, ["health"])

        # Temporal PASS, worker FAIL, classifier PASS.
        temporal_line = next(
            line for line in result.output.splitlines()
            if "Temporal reachable" in line
        )
        worker_line = next(
            line for line in result.output.splitlines()
            if "Worker liveness" in line
        )
        assert "PASS" in temporal_line
        assert "FAIL" in worker_line
        assert "no pollers" in worker_line


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


class TestFindingsNoSession:
    def test_missing_file_prints_no_findings_yet(
        self, runner, cli_module, tmp_path, monkeypatch
    ):
        """Non-existent state file → 'no findings yet' + exit 0."""
        # Redirect HOME so the resolver's fallback path lands in tmp_path
        # and is guaranteed to be empty.
        monkeypatch.setenv("HOME", str(tmp_path))

        # Make the status-query branch fail so the resolver falls back
        # to ~/.swarm/state/<id>/findings.jsonl, which doesn't exist.
        with patch(
            "swarmd.cli.Client.connect",
            AsyncMock(side_effect=OSError("no temporal")),
        ):
            result = runner.invoke(
                cli_module.cli, ["findings", "nonexistent-mission"]
            )

        assert result.exit_code == 0, result.output + (result.stderr or "")
        assert "no findings yet" in result.output


class TestFindingsWithTail:
    def test_tail_n_limits_output(
        self, runner, cli_module, tmp_path, monkeypatch
    ):
        """100 jsonl lines + --tail 10 → only 10 lines printed."""
        monkeypatch.setenv("HOME", str(tmp_path))

        # Seed the fallback state dir with 100 findings.
        wf_id = "mission-tail-test"
        state_dir = tmp_path / ".swarm" / "state" / wf_id
        state_dir.mkdir(parents=True)
        findings_path = state_dir / "findings.jsonl"

        with findings_path.open("w") as fh:
            for i in range(100):
                fh.write(
                    json.dumps(
                        {
                            "ts": f"2026-04-18T00:00:{i:02d}Z",
                            "type": "meta",
                            "verdict": f"verdict-{i}",
                            "rationale": f"rationale-{i}",
                        }
                    )
                    + "\n"
                )

        with patch(
            "swarmd.cli.Client.connect",
            AsyncMock(side_effect=OSError("no temporal")),
        ):
            result = runner.invoke(
                cli_module.cli, ["findings", wf_id, "--tail", "10"]
            )

        assert result.exit_code == 0, result.output + (result.stderr or "")
        # 10 lines → 10 rationale tokens — none from indices 0-89 (which
        # would be in the first 90), only 90-99 survive the tail.
        assert "rationale-99" in result.output
        assert "rationale-90" in result.output
        assert "rationale-89" not in result.output
        # And exactly 10 output lines.
        non_empty = [l for l in result.output.splitlines() if l.strip()]
        assert len(non_empty) == 10


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


class TestLogsNoFile:
    def test_missing_log_file_prints_no_logs_yet(
        self, runner, cli_module, tmp_path, monkeypatch
    ):
        """Non-existent mission.log → 'no logs yet' + exit 0."""
        monkeypatch.setenv("HOME", str(tmp_path))

        with patch(
            "swarmd.cli.Client.connect",
            AsyncMock(side_effect=OSError("no temporal")),
        ):
            result = runner.invoke(
                cli_module.cli, ["logs", "nonexistent-mission"]
            )

        assert result.exit_code == 0, result.output + (result.stderr or "")
        assert "no logs yet" in result.output
