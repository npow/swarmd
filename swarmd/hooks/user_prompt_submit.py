"""Claude Code UserPromptSubmit hook — classifier + confidence gate.

When the user submits a prompt, the harness pipes a JSON payload to this
hook on stdin. We:

1. Run stages 1+2 (rules/prefix) synchronously — cheap, deterministic.
2. If the result is UNCERTAIN or confidence < MEDIUM_GATE, fire stage 3
   (Haiku) with a 10 s timeout. Keep the higher-confidence result.
3. Apply the confidence gate (spec §9.3) to decide whether to emit
   ``additionalContext`` that nudges the main session toward ``swarm launch``.
4. Log every classification to ``~/.swarm/classifier.jsonl`` — one JSON
   line per decision, for auditability and offline evaluation.

The hook never launches swarm itself. It injects context that the main
session reads; keeping the architecture loosely coupled and auditable.
Exits 0 always (non-blocking); errors are logged, never raised.

See spec §9 for the classifier cascade, §9.3 for the gate policy, §9.4
for the log format.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from swarmd.classifier import (
    ClassifierResult,
    ClassifierVerdict,
    classify as classify_rules_sync,
    classify_llm,
)


# Log destination. Per-user, append-only JSONL. Parent dir created lazily.
LOG_PATH = Path("~/.swarm/classifier.jsonl").expanduser()

# Hard cap on how long we'll wait for Haiku. Matches the timeout inside
# classify_llm itself — the outer wait_for is belt-and-braces in case the
# inner timeout is bypassed.
STAGE3_TIMEOUT_SEC = 10.0

# Confidence gates per spec §9.3.
STRONG_GATE = 0.8   # verdict + nudging context if ≥ this
MEDIUM_GATE = 0.6   # neutral context if ≥ this; stage 3 fires if below

# Cap the prompt excerpt we retain in the log. Full prompts can be huge
# and we don't want the log file to balloon.
PROMPT_HEAD_CHARS = 200


def main() -> int:
    """Entry point: read stdin, emit ``additionalContext`` if applicable.

    Always returns 0; errors are captured in the log, never propagated.
    """
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        # Malformed stdin → silently no-op. The hook must be non-blocking.
        return 0
    if not isinstance(payload, dict):
        return 0

    prompt = payload.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    session_id = payload.get("session_id") or "unknown"
    cwd = payload.get("cwd") or ""

    started_at = time.time()

    # --- Stage 1+2 (sync) ---
    try:
        result = classify_rules_sync(prompt)
    except Exception as exc:  # pragma: no cover — defensive
        _log(session_id, prompt, None, started_at, error=type(exc).__name__)
        return 0

    # --- Stage 3 (async) — only if uncertain or low confidence ---
    if (
        result.verdict == ClassifierVerdict.UNCERTAIN
        or result.confidence < MEDIUM_GATE
    ):
        try:
            llm_result = asyncio.run(
                asyncio.wait_for(
                    classify_llm(prompt, context={"cwd": cwd}),
                    timeout=STAGE3_TIMEOUT_SEC,
                )
            )
            if llm_result.confidence > result.confidence:
                result = llm_result
        except BaseException as exc:
            # Stage 3 failure (timeout, TransientError, TerminalError, auth,
            # anything). Fall back to the stage-1+2 result. Log the error
            # but keep the user's prompt flow unblocked.
            _log(
                session_id,
                prompt,
                result,
                started_at,
                error=type(exc).__name__,
            )
            # Early return: we've already logged; don't log again below.
            context_text = _build_context(result)
            if context_text:
                sys.stdout.write(
                    json.dumps({"additionalContext": context_text})
                )
            return 0

    # --- Apply gate → maybe inject context ---
    context_text = _build_context(result)
    _log(session_id, prompt, result, started_at)

    if context_text:
        sys.stdout.write(json.dumps({"additionalContext": context_text}))
    return 0


def _build_context(result: ClassifierResult) -> str | None:
    """Return injected context text, or None for no injection.

    Policy (spec §9.3):
      - MISSION + confidence ≥ STRONG_GATE → strong nudge toward ``swarm launch``.
      - MISSION + confidence ≥ MEDIUM_GATE → neutral classifier note.
      - Anything else (CHAT/META/UNCERTAIN, or low-confidence MISSION) → None.
    """
    if result.verdict == ClassifierVerdict.MISSION:
        if result.confidence >= STRONG_GATE:
            return (
                "This prompt appears to be a MISSION (high confidence). "
                "Consider whether to invoke the swarm harness with "
                "`swarm launch` — see /swarm for explicit handling. "
                f"(classifier: verdict=mission, confidence={result.confidence:.2f}, "
                f"stage={result.stage}, reason={result.reason})"
            )
        if result.confidence >= MEDIUM_GATE:
            return (
                "This prompt may be mission-shaped. "
                f"(classifier: verdict=mission, confidence={result.confidence:.2f}, "
                f"stage={result.stage}, reason={result.reason})"
            )
    # CHAT / META / UNCERTAIN / low-confidence MISSION → no context.
    return None


def _log(
    session_id: str,
    prompt: str,
    result: ClassifierResult | None,
    started_at: float,
    error: str | None = None,
) -> None:
    """Append a single JSON line to LOG_PATH. Never raises.

    Spec §9.4 log fields: ts, session_id, prompt_head, verdict, stage,
    confidence, reason, elapsed_ms, and optional error.
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, object] = {
            "ts": time.time(),
            "session_id": session_id,
            "prompt_head": prompt[:PROMPT_HEAD_CHARS],
            "elapsed_ms": int((time.time() - started_at) * 1000),
        }
        if result is not None:
            verdict_value = (
                result.verdict.value
                if hasattr(result.verdict, "value")
                else str(result.verdict)
            )
            entry.update(
                {
                    "verdict": verdict_value,
                    "stage": result.stage,
                    "confidence": result.confidence,
                    "reason": result.reason,
                }
            )
        if error:
            entry["error"] = error
        with LOG_PATH.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        # Logging must never take down the hook. Swallow and move on.
        pass


if __name__ == "__main__":
    sys.exit(main())
