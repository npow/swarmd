"""Tests for deep-qa-cli — parallel LLM QA critic with severity budgets."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "swarm" / "deep-qa-cli"

_CLEAN_RESPONSE = json.dumps({"findings": []})
_CRITICAL_RESPONSE = json.dumps(
    {"findings": [{"severity": "critical", "defect": "null deref", "location": "line 5", "rationale": "x"}]}
)
_MAJOR_RESPONSE = json.dumps(
    {"findings": [{"severity": "major", "defect": "missing check", "location": "N/A", "rationale": "y"}]}
)


# ---------------------------------------------------------------------------
# Import the CLI module once (in-process) with sys.path patched correctly.
# conftest.py already inserts REPO_ROOT into sys.path[0] so swarm.lib works.
# ---------------------------------------------------------------------------

def _load_cli():
    """Load deep-qa-cli as a module. Evict cache each time for clean state."""
    for key in list(sys.modules):
        if key == "deep_qa_cli":
            del sys.modules[key]
    # spec_from_file_location needs an explicit loader for extension-less files.
    loader = importlib.machinery.SourceFileLoader("deep_qa_cli", str(CLI_PATH))
    spec = importlib.util.spec_from_loader("deep_qa_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# Load once at module level to verify importability.
_cli = _load_cli()


# ---------------------------------------------------------------------------
# Test 1: clean artifact exits 0
# ---------------------------------------------------------------------------

def test_clean_artifact_exits_0(tmp_path):
    artifact = tmp_path / "artifact.py"
    artifact.write_text("def foo(): return 1\n")

    with patch("swarm.lib.llm_client.call", return_value=_CLEAN_RESPONSE):
        code = _cli.run(
            artifact_path=str(artifact),
            artifact_type="code",
            max_critical=0,
            max_major=2,
            json_mode=False,
            model=None,
        )

    assert code == 0


# ---------------------------------------------------------------------------
# Test 2: critical over budget exits 1
# ---------------------------------------------------------------------------

def test_critical_over_budget_exits_1(tmp_path):
    artifact = tmp_path / "artifact.py"
    artifact.write_text("x = None\nx.method()\n")

    with patch("swarm.lib.llm_client.call", return_value=_CRITICAL_RESPONSE):
        code = _cli.run(
            artifact_path=str(artifact),
            artifact_type="code",
            max_critical=0,
            max_major=2,
            json_mode=False,
            model=None,
        )

    assert code == 1


# ---------------------------------------------------------------------------
# Test 3: major within budget exits 0
# ---------------------------------------------------------------------------

def test_major_within_budget_exits_0(tmp_path):
    artifact = tmp_path / "artifact.py"
    artifact.write_text("code here\n")

    # 5 critics for code type; 2 return major, 3 return clean — exactly at budget
    responses = [_MAJOR_RESPONSE, _MAJOR_RESPONSE, _CLEAN_RESPONSE, _CLEAN_RESPONSE, _CLEAN_RESPONSE]
    call_iter = iter(responses)

    with patch("swarm.lib.llm_client.call", side_effect=lambda *a, **kw: next(call_iter)):
        code = _cli.run(
            artifact_path=str(artifact),
            artifact_type="code",
            max_critical=0,
            max_major=2,
            json_mode=False,
            model=None,
        )

    assert code == 0


# ---------------------------------------------------------------------------
# Test 4: major over budget exits 1
# ---------------------------------------------------------------------------

def test_major_over_budget_exits_1(tmp_path):
    artifact = tmp_path / "artifact.py"
    artifact.write_text("code here\n")

    # 3 majors across 3 of 5 critics, budget = 2
    responses = [_MAJOR_RESPONSE, _MAJOR_RESPONSE, _MAJOR_RESPONSE, _CLEAN_RESPONSE, _CLEAN_RESPONSE]
    call_iter = iter(responses)

    with patch("swarm.lib.llm_client.call", side_effect=lambda *a, **kw: next(call_iter)):
        code = _cli.run(
            artifact_path=str(artifact),
            artifact_type="code",
            max_critical=0,
            max_major=2,
            json_mode=False,
            model=None,
        )

    assert code == 1


# ---------------------------------------------------------------------------
# Test 5: --json mode outputs valid JSONL
# ---------------------------------------------------------------------------

def test_json_mode_outputs_valid_jsonl(tmp_path, capsys):
    artifact = tmp_path / "artifact.py"
    artifact.write_text("code here\n")

    with patch("swarm.lib.llm_client.call", return_value=_CRITICAL_RESPONSE):
        code = _cli.run(
            artifact_path=str(artifact),
            artifact_type="code",
            max_critical=10,   # don't fail budget so we get all findings
            max_major=10,
            json_mode=True,
            model=None,
        )

    captured = capsys.readouterr()
    lines = [l for l in captured.out.splitlines() if l.strip()]
    assert len(lines) > 0, "Expected at least one JSON line in output"
    for line in lines:
        obj = json.loads(line)  # raises if invalid JSON
        assert isinstance(obj, dict)


# ---------------------------------------------------------------------------
# Test 6: missing artifact path exits 2
# ---------------------------------------------------------------------------

def test_missing_artifact_path_exits_2():
    code = _cli.run(
        artifact_path="/nonexistent/path/artifact.txt",
        artifact_type="code",
        max_critical=0,
        max_major=2,
        json_mode=False,
        model=None,
    )
    assert code == 2


# ---------------------------------------------------------------------------
# Test 7: majority LLM failure exits 2
# ---------------------------------------------------------------------------

def test_majority_llm_failure_exits_2(tmp_path):
    artifact = tmp_path / "artifact.py"
    artifact.write_text("code here\n")

    from swarmd.lib.llm_client import LLMError

    # >50% of 5 critics (code) fail — 3 failures
    responses: list = [
        LLMError("fail1"),
        LLMError("fail2"),
        LLMError("fail3"),
        _CLEAN_RESPONSE,
        _CLEAN_RESPONSE,
    ]
    call_iter = iter(responses)

    def side_effect(*args, **kwargs):
        val = next(call_iter)
        if isinstance(val, Exception):
            raise val
        return val

    with patch("swarm.lib.llm_client.call", side_effect=side_effect):
        code = _cli.run(
            artifact_path=str(artifact),
            artifact_type="code",
            max_critical=0,
            max_major=2,
            json_mode=False,
            model=None,
        )

    assert code == 2


# ---------------------------------------------------------------------------
# Test 8: --type determines critic count
# ---------------------------------------------------------------------------

def test_type_determines_critic_count(tmp_path):
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("content\n")

    for type_name, expected_count in [("code", 5), ("exploration", 3)]:
        mock_call = MagicMock(return_value=_CLEAN_RESPONSE)
        with patch("swarm.lib.llm_client.call", mock_call):
            _cli.run(
                artifact_path=str(artifact),
                artifact_type=type_name,
                max_critical=0,
                max_major=2,
                json_mode=False,
                model=None,
            )
        assert mock_call.call_count == expected_count, (
            f"Expected {expected_count} critics for {type_name}, got {mock_call.call_count}"
        )
