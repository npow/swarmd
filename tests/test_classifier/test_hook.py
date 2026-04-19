"""Tests for the UserPromptSubmit classifier hook.

Covers the confidence gate (spec §9.3), log format (spec §9.4), and all
error-path fallbacks. Patches ``classify`` (stage 1+2) with ``MagicMock``
and ``classify_llm`` (stage 3) with ``AsyncMock`` so tests are fast and
hermetic.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from swarmd.classifier import ClassifierResult, ClassifierVerdict
from swarmd.durable.errors import TransientError

# Load the hook module directly by path. We can't use ``import
# swarm.hooks.user_prompt_submit`` here because under pytest the top-level
# ``swarm`` package resolves as a namespace package whose search path also
# includes the repo root — and the repo root has a sibling ``hooks/``
# directory (Task 24's shell-hook support code) with its own ``__init__.py``.
# That sibling shadows our ``swarm/hooks/`` Python subpackage. Load by file
# location instead, so the test exercises the exact module under test.
_HOOK_PATH = (
    Path(__file__).resolve().parents[2]
    / "swarmd"
    / "hooks"
    / "user_prompt_submit.py"
)
_spec = importlib.util.spec_from_file_location(
    "swarm_user_prompt_submit_under_test", str(_HOOK_PATH)
)
hook_mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = hook_mod
_spec.loader.exec_module(hook_mod)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_hook(
    stdin_payload,
    log_path: Path,
    rules_result: ClassifierResult | None = None,
    llm_result: ClassifierResult | None = None,
    llm_exception: BaseException | None = None,
):
    """Run hook_mod.main() with patched IO and classifier seams.

    Returns a tuple ``(return_code, stdout_text, log_lines)``.
    """
    stdin_text = (
        stdin_payload
        if isinstance(stdin_payload, str)
        else json.dumps(stdin_payload)
    )

    if rules_result is None:
        rules_mock = MagicMock(
            return_value=ClassifierResult(
                verdict=ClassifierVerdict.UNCERTAIN,
                stage=2,
                confidence=0.0,
                reason="no signals",
            )
        )
    else:
        rules_mock = MagicMock(return_value=rules_result)

    if llm_exception is not None:
        llm_mock = AsyncMock(side_effect=llm_exception)
    elif llm_result is not None:
        llm_mock = AsyncMock(return_value=llm_result)
    else:
        llm_mock = AsyncMock()

    fake_stdout = io.StringIO()
    fake_stdin = io.StringIO(stdin_text)

    with (
        patch.object(hook_mod, "LOG_PATH", log_path),
        patch.object(hook_mod, "classify_rules_sync", rules_mock),
        patch.object(hook_mod, "classify_llm", llm_mock),
        patch.object(sys, "stdin", fake_stdin),
        patch.object(sys, "stdout", fake_stdout),
    ):
        rc = hook_mod.main()

    stdout_text = fake_stdout.getvalue()
    if log_path.exists():
        log_lines = [
            line for line in log_path.read_text().splitlines() if line.strip()
        ]
    else:
        log_lines = []
    return rc, stdout_text, log_lines


def _mk(verdict: ClassifierVerdict, confidence: float, stage: int = 2, reason: str = "test"):
    return ClassifierResult(
        verdict=verdict, stage=stage, confidence=confidence, reason=reason
    )


# ---------------------------------------------------------------------------
# 1. Strong MISSION → nudging context
# ---------------------------------------------------------------------------


def test_strong_mission_emits_nudge_context(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"prompt": "fix the bug in auth.py", "session_id": "s1", "cwd": "/tmp"},
        log,
        rules_result=_mk(ClassifierVerdict.MISSION, 0.9, stage=2, reason="imperative"),
    )
    assert rc == 0
    payload = json.loads(stdout)
    text = payload["additionalContext"]
    assert "MISSION" in text
    assert "high confidence" in text
    assert "swarm launch" in text
    assert "confidence=0.90" in text

    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["verdict"] == "mission"
    assert entry["stage"] == 2
    assert entry["confidence"] == pytest.approx(0.9)
    assert entry["session_id"] == "s1"
    assert entry["prompt_head"] == "fix the bug in auth.py"
    assert "error" not in entry


# ---------------------------------------------------------------------------
# 2. Medium MISSION → neutral context
# ---------------------------------------------------------------------------


def test_medium_mission_emits_neutral_context(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"prompt": "maybe update this", "session_id": "s2", "cwd": "/x"},
        log,
        rules_result=_mk(ClassifierVerdict.MISSION, 0.65, reason="weak imperative"),
    )
    assert rc == 0
    payload = json.loads(stdout)
    text = payload["additionalContext"]
    # Neutral context must NOT instruct the user to run swarm launch.
    assert "mission-shaped" in text
    assert "swarm launch" not in text
    assert "high confidence" not in text
    assert "confidence=0.65" in text

    assert len(log_lines) == 1


# ---------------------------------------------------------------------------
# 3. Low confidence + stage 3 returns UNCERTAIN → no context
# ---------------------------------------------------------------------------


def test_low_confidence_uncertain_no_context(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"prompt": "something", "session_id": "s3", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.MISSION, 0.5, reason="weak"),
        # Stage 3 fires because 0.5 < MEDIUM_GATE; returns UNCERTAIN 0.4.
        llm_result=_mk(ClassifierVerdict.UNCERTAIN, 0.4, stage=3, reason="ambiguous"),
    )
    assert rc == 0
    # stdout is empty when no context is injected.
    assert stdout == ""
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    # LLM result had higher confidence (0.4 > 0.5 is False!) → rule stays.
    # Actually 0.4 < 0.5, so we keep the rule result.
    assert entry["verdict"] == "mission"
    assert entry["stage"] == 2


# ---------------------------------------------------------------------------
# 4. UNCERTAIN from stage 1+2 → stage 3 fires → strong MISSION nudge
# ---------------------------------------------------------------------------


def test_uncertain_triggers_stage3_strong_nudge(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"prompt": "make it fast", "session_id": "s4", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.UNCERTAIN, 0.3, reason="mixed"),
        llm_result=_mk(
            ClassifierVerdict.MISSION, 0.85, stage=3, reason="llm: imperative"
        ),
    )
    assert rc == 0
    payload = json.loads(stdout)
    text = payload["additionalContext"]
    assert "MISSION" in text
    assert "swarm launch" in text
    assert "stage=3" in text

    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["stage"] == 3
    assert entry["verdict"] == "mission"
    assert entry["confidence"] == pytest.approx(0.85)


# ---------------------------------------------------------------------------
# 5. CHAT verdict → no context (log entry still written)
# ---------------------------------------------------------------------------


def test_chat_verdict_no_context(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"prompt": "what is metaflow?", "session_id": "s5", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.CHAT, 0.9, reason="interrogative"),
    )
    assert rc == 0
    assert stdout == ""
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["verdict"] == "chat"


# ---------------------------------------------------------------------------
# 6. META verdict → no context
# ---------------------------------------------------------------------------


def test_meta_verdict_no_context(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"prompt": "what does swarm do?", "session_id": "s6", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.META, 0.9, reason="reflective"),
    )
    assert rc == 0
    assert stdout == ""
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["verdict"] == "meta"


# ---------------------------------------------------------------------------
# 7. Stage 3 timeout → fall back to stage 1+2 result (UNCERTAIN → no context)
# ---------------------------------------------------------------------------


def test_stage3_timeout_falls_back(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"prompt": "um", "session_id": "s7", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.UNCERTAIN, 0.5, reason="low"),
        llm_exception=asyncio.TimeoutError("boom"),
    )
    assert rc == 0
    # UNCERTAIN → no context is emitted.
    assert stdout == ""
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["error"] == "TimeoutError"
    assert entry["verdict"] == "uncertain"
    assert entry["stage"] == 2


# ---------------------------------------------------------------------------
# 8. Stage 3 raises TransientError → same fallback
# ---------------------------------------------------------------------------


def test_stage3_transient_error_falls_back(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"prompt": "hmm", "session_id": "s8", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.UNCERTAIN, 0.5, reason="low"),
        llm_exception=TransientError("upstream hiccup"),
    )
    assert rc == 0
    assert stdout == ""
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert entry["error"] == "TransientError"
    assert entry["verdict"] == "uncertain"


# ---------------------------------------------------------------------------
# 9. Malformed stdin → exit 0, no log, no stdout
# ---------------------------------------------------------------------------


def test_malformed_stdin_noop(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        "{not json",
        log,
        rules_result=_mk(ClassifierVerdict.MISSION, 0.9),
    )
    assert rc == 0
    assert stdout == ""
    assert log_lines == []
    assert not log.exists()


# ---------------------------------------------------------------------------
# 10. Empty prompt → exit 0, no log, no stdout
# ---------------------------------------------------------------------------


def test_empty_prompt_noop(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"prompt": "", "session_id": "s10", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.MISSION, 0.9),
    )
    assert rc == 0
    assert stdout == ""
    assert log_lines == []


def test_whitespace_only_prompt_noop(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"prompt": "   \n\t ", "session_id": "s10b", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.MISSION, 0.9),
    )
    assert rc == 0
    assert stdout == ""
    assert log_lines == []


# ---------------------------------------------------------------------------
# 11. Missing prompt field → exit 0, no log, no stdout
# ---------------------------------------------------------------------------


def test_missing_prompt_field_noop(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"session_id": "s11"},  # no "prompt"
        log,
        rules_result=_mk(ClassifierVerdict.MISSION, 0.9),
    )
    assert rc == 0
    assert stdout == ""
    assert log_lines == []


# ---------------------------------------------------------------------------
# 12. Long prompt → prompt_head truncated to 200 chars in log
# ---------------------------------------------------------------------------


def test_long_prompt_truncated_in_log(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    long_prompt = "x" * 5000
    rc, stdout, log_lines = _run_hook(
        {"prompt": long_prompt, "session_id": "s12", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.MISSION, 0.9),
    )
    assert rc == 0
    assert len(log_lines) == 1
    entry = json.loads(log_lines[0])
    assert len(entry["prompt_head"]) == 200
    assert entry["prompt_head"] == "x" * 200


# ---------------------------------------------------------------------------
# 13. Log appended across multiple invocations
# ---------------------------------------------------------------------------


def test_log_is_appended(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    _run_hook(
        {"prompt": "first prompt", "session_id": "a", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.MISSION, 0.9),
    )
    _run_hook(
        {"prompt": "second prompt", "session_id": "b", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.CHAT, 0.9),
    )
    assert log.exists()
    lines = [l for l in log.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    entries = [json.loads(l) for l in lines]
    assert entries[0]["session_id"] == "a"
    assert entries[0]["verdict"] == "mission"
    assert entries[1]["session_id"] == "b"
    assert entries[1]["verdict"] == "chat"


# ---------------------------------------------------------------------------
# 14. Log parent dir created on demand
# ---------------------------------------------------------------------------


def test_log_parent_dir_created(tmp_path):
    log = tmp_path / "nested" / "deeper" / ".swarm" / "classifier.jsonl"
    assert not log.parent.exists()
    rc, stdout, log_lines = _run_hook(
        {"prompt": "do the thing", "session_id": "sdir", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.MISSION, 0.9),
    )
    assert rc == 0
    assert log.parent.exists()
    assert len(log_lines) == 1


# ---------------------------------------------------------------------------
# 15. LLM result with higher confidence replaces rule result
# ---------------------------------------------------------------------------


def test_llm_higher_confidence_wins(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"prompt": "do stuff", "session_id": "s_win", "cwd": ""},
        log,
        rules_result=_mk(ClassifierVerdict.MISSION, 0.5, reason="weak rule"),
        llm_result=_mk(ClassifierVerdict.CHAT, 0.9, stage=3, reason="llm: chat"),
    )
    assert rc == 0
    # LLM said CHAT w/ 0.9 → replaces rule result → no context.
    assert stdout == ""
    entry = json.loads(log_lines[0])
    assert entry["verdict"] == "chat"
    assert entry["stage"] == 3
    assert entry["confidence"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# 16. LLM result with lower confidence is ignored
# ---------------------------------------------------------------------------


def test_llm_lower_confidence_ignored(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        {"prompt": "go", "session_id": "s_lose", "cwd": ""},
        log,
        # 0.5 is below MEDIUM_GATE → stage 3 fires.
        rules_result=_mk(ClassifierVerdict.MISSION, 0.5, reason="ok rule"),
        llm_result=_mk(ClassifierVerdict.CHAT, 0.3, stage=3, reason="llm unsure"),
    )
    assert rc == 0
    # Rule result kept (stage=2, MISSION, 0.5). Below MEDIUM_GATE so no context.
    assert stdout == ""
    entry = json.loads(log_lines[0])
    assert entry["stage"] == 2
    assert entry["verdict"] == "mission"
    assert entry["confidence"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# 17. High-confidence stage-1+2 (≥ MEDIUM_GATE and not UNCERTAIN) → no LLM call
# ---------------------------------------------------------------------------


def test_high_confidence_rule_result_skips_llm(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"

    llm_mock = AsyncMock(
        return_value=_mk(ClassifierVerdict.CHAT, 0.99, stage=3)
    )
    fake_stdout = io.StringIO()
    fake_stdin = io.StringIO(
        json.dumps(
            {"prompt": "fix the bug", "session_id": "skip", "cwd": ""}
        )
    )
    rules_mock = MagicMock(
        return_value=_mk(ClassifierVerdict.MISSION, 0.9, stage=2, reason="strong")
    )

    with (
        patch.object(hook_mod, "LOG_PATH", log),
        patch.object(hook_mod, "classify_rules_sync", rules_mock),
        patch.object(hook_mod, "classify_llm", llm_mock),
        patch.object(sys, "stdin", fake_stdin),
        patch.object(sys, "stdout", fake_stdout),
    ):
        rc = hook_mod.main()

    assert rc == 0
    # LLM should NOT be called.
    assert llm_mock.await_count == 0
    # Stage 2 strong MISSION → nudging context.
    payload = json.loads(fake_stdout.getvalue())
    assert "swarm launch" in payload["additionalContext"]


# ---------------------------------------------------------------------------
# 18. Non-dict JSON payload (list, string) → exit 0, no log, no stdout
# ---------------------------------------------------------------------------


def test_non_dict_json_payload_noop(tmp_path):
    log = tmp_path / ".swarm" / "classifier.jsonl"
    rc, stdout, log_lines = _run_hook(
        ["not", "a", "dict"],
        log,
        rules_result=_mk(ClassifierVerdict.MISSION, 0.9),
    )
    assert rc == 0
    assert stdout == ""
    assert log_lines == []
