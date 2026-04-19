"""Stage 3 of the swarm prompt classifier — Haiku LLM fallback.

Invoked when stages 1+2 (``swarm.classifier.rules``) return UNCERTAIN. The
LLM decides among the four verdicts and returns its own confidence. The
caller is responsible for applying the ≥0.6 confidence gate (spec §9.3,
Task 22) — this module's job is to run the LLM, parse the response, and
raise the correct classified exception on failure.

Error taxonomy (shared with the durable activity layer):

- ``AuthError``       — 401/403 from anthropic; credentials bad.
- ``TransientError``  — 408/424/429/5xx or asyncio timeout; safe to retry.
- ``TerminalError``   — 400/404, malformed JSON, bad verdict; retry won't fix.

Timeout: ``_TIMEOUT_SEC`` (10 seconds) around the anthropic call. Stage 3
exists to reduce ambiguity fast — if Haiku hangs, we fail gracefully as
TransientError and the caller treats the result as UNCERTAIN (fail-open,
per spec §5 principle 3).

The ``_invoke_haiku`` helper is a single narrow seam for tests: patching it
lets tests control the response text without mocking the whole anthropic
SDK, while the HTTP error translation path is exercised directly by
patching the SDK client (same pattern as ``progress_audit.py``).

Implementation notes:

- The model ID is pinned (``claude-haiku-4-5-20251001``) so behavior stays
  predictable across SDK updates.
- The anthropic SDK exposes a sync client; we call it from within a
  thread via ``asyncio.to_thread`` so ``asyncio.wait_for`` actually
  interrupts a hanging HTTP call.
- JSON parsing tolerates a ``` or ```json fence (Haiku occasionally adds
  one even when instructed not to). Beyond that, garbage → TerminalError.
- Confidence is clamped to [0.0, 1.0] silently — out-of-range values are
  usually the model's rounding, not a contract violation.
- Reason is capped at 500 chars (truncate, don't error); missing reason
  → ``"(no reason provided)"``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from anthropic import Anthropic, APIStatusError

from swarmd.classifier.prompts import CLASSIFIER_PROMPT
from swarmd.classifier.rules import ClassifierResult, ClassifierVerdict
from swarmd.durable.errors import (
    TerminalError,
    TransientError,
    classify_http_status,
)

# Haiku 4.5 — fast (~500ms) and cheap. Pin the exact model ID per spec §9.4.
_HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Hard cap on response tokens. The contract is a single-line JSON object;
# 512 tokens is generous headroom for malformed outputs that cascade into
# prose before we reject them.
_MAX_TOKENS = 512

# Asyncio timeout. Stage 3 must be fast. If Haiku hangs we fail open to
# TransientError; the caller treats transient failures as UNCERTAIN.
_TIMEOUT_SEC = 10.0

# Max length of the ``reason`` field we keep on the returned result.
_MAX_REASON_CHARS = 500

# Valid verdict strings (lowercase, matches the enum values).
_VALID_VERDICTS = {v.value for v in ClassifierVerdict}


async def classify_llm(
    prompt: str, context: dict[str, Any] | None = None
) -> ClassifierResult:
    """Stage 3 — Haiku LLM classification.

    Invokes Haiku with ``CLASSIFIER_PROMPT``, the user prompt, and optional
    context (cwd, recent files, etc.). Returns a ``ClassifierResult`` with
    ``stage=3``.

    Errors:
        AuthError: 401/403 from anthropic — credentials bad/expired.
        TransientError: 408/424/429/5xx or asyncio timeout — safe to retry.
        TerminalError: 400/404, malformed JSON, bad verdict — retry won't fix.
    """
    context_str = _format_context(context)
    prompt_text = CLASSIFIER_PROMPT.format(
        user_prompt=prompt, context=context_str
    )

    try:
        raw = await asyncio.wait_for(
            _invoke_haiku(prompt_text), timeout=_TIMEOUT_SEC
        )
    except asyncio.TimeoutError as exc:
        raise TransientError(
            f"classify_llm: Haiku timed out after {_TIMEOUT_SEC}s"
        ) from exc

    verdict, confidence, reason = _parse_response(raw)
    return ClassifierResult(
        verdict=verdict, stage=3, confidence=confidence, reason=reason
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _invoke_haiku(prompt_text: str) -> str:
    """Call Haiku with ``prompt_text`` and return the raw response text.

    Wraps the sync anthropic client in ``asyncio.to_thread`` so the caller's
    ``asyncio.wait_for`` actually interrupts a hanging HTTP call. Translates
    anthropic ``APIStatusError`` into the swarm's classified taxonomy via
    ``classify_http_status`` (401 → AuthError, 429/424/5xx → TransientError,
    etc.).

    Tests patch this helper directly to control the returned text without
    mocking the SDK. Tests that want to exercise the HTTP translation
    layer patch ``Anthropic`` instead.
    """
    return await asyncio.to_thread(_invoke_haiku_sync, prompt_text)


def _invoke_haiku_sync(prompt_text: str) -> str:
    """Blocking Haiku call — runs on a worker thread via ``to_thread``.

    Factored out of ``_invoke_haiku`` so the async/sync boundary is
    explicit. Don't call this directly from async code.
    """
    client = Anthropic()
    try:
        response = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt_text}],
        )
    except APIStatusError as exc:
        # The SDK's APIStatusError carries the HTTP response. Translate the
        # status into our taxonomy — 429 → TransientError, 401 → AuthError,
        # etc. Match the durable activity layer convention.
        status = exc.response.status_code
        body = (exc.response.content or b"")[:200]
        classify_http_status(status, body)
        # classify_http_status always raises for non-2xx; this raise keeps
        # the type checker honest.
        raise
    return _extract_text(response)


def _extract_text(response: Any) -> str:
    """Pull the plain-text body out of an anthropic Message.

    Anthropic's ``Message.content`` is a list of content blocks; for a plain
    text response the first block carries ``.text``. Defensive against an
    empty content list (unusual but possible on abnormal completions) —
    returns empty string so ``_parse_response`` raises a consistent
    TerminalError rather than this helper blowing up with IndexError.
    """
    try:
        block = response.content[0]
    except (IndexError, AttributeError):
        return ""
    return getattr(block, "text", "") or ""


def _format_context(context: dict[str, Any] | None) -> str:
    """Render the optional context dict into a compact string for the prompt.

    None → ``"(none)"``. Empty dict → same. Otherwise render as ``key: value``
    lines. We don't use ``json.dumps`` because the prompt treats the context
    as human-readable metadata, not a structured payload for the model.
    """
    if not context:
        return "(none)"
    try:
        lines = [f"{k}: {v}" for k, v in context.items()]
    except Exception:
        # Defensive — if iteration blows up, surface something parseable
        # rather than crashing the classifier.
        return "(unparseable context)"
    return "\n".join(lines) if lines else "(none)"


def _strip_fence(text: str) -> str:
    """Remove a leading ```json or ``` fence and trailing ``` from ``text``.

    Haiku sometimes wraps JSON in a fence despite being instructed not to.
    Strip at most one fence; don't try to be clever about nested markdown.
    """
    text = text.strip()
    for fence in ("```json", "```"):
        if text.startswith(fence):
            text = text[len(fence):].strip()
            break
    if text.endswith("```"):
        text = text[:-3].strip()
    return text


def _parse_response(raw: str) -> tuple[ClassifierVerdict, float, str]:
    """Parse the model's JSON response → ``(verdict, confidence, reason)``.

    Raises:
        TerminalError: empty response, unparseable JSON, or a verdict
            string outside ``_VALID_VERDICTS``. None of these can be fixed
            by retrying.
    """
    if not raw or not raw.strip():
        raise TerminalError("classify_llm: empty model output")

    text = _strip_fence(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TerminalError(
            f"classify_llm: unparseable model output: {text[:200]!r}"
        ) from exc

    if not isinstance(data, dict):
        raise TerminalError(
            f"classify_llm: expected JSON object, got {type(data).__name__}"
        )

    verdict_raw = data.get("verdict")
    if not isinstance(verdict_raw, str):
        raise TerminalError(
            f"classify_llm: verdict missing or non-string: {verdict_raw!r}"
        )
    verdict_key = verdict_raw.strip().lower()
    if verdict_key not in _VALID_VERDICTS:
        raise TerminalError(
            f"classify_llm: verdict not in {_VALID_VERDICTS}: {verdict_raw!r}"
        )
    verdict = ClassifierVerdict(verdict_key)

    confidence = _parse_confidence(data.get("confidence"))
    reason = _parse_reason(data.get("reason"))

    return verdict, confidence, reason


def _parse_confidence(value: Any) -> float:
    """Coerce ``value`` to a float and clamp to [0.0, 1.0].

    Out-of-range but parseable values are clamped silently — this is almost
    always the model rounding (``1.0`` returned as ``1.01``), not a contract
    violation. Unparseable values raise TerminalError.
    """
    if value is None:
        raise TerminalError("classify_llm: confidence missing")
    try:
        conf = float(value)
    except (TypeError, ValueError) as exc:
        raise TerminalError(
            f"classify_llm: confidence not a number: {value!r}"
        ) from exc
    if conf < 0.0:
        return 0.0
    if conf > 1.0:
        return 1.0
    return conf


def _parse_reason(value: Any) -> str:
    """Normalize the ``reason`` field.

    - Missing / empty → placeholder ``"(no reason provided)"``.
    - Too long → truncate at ``_MAX_REASON_CHARS``.
    - Non-string → coerce via ``str()`` then apply the same rules.
    """
    if value is None:
        return "(no reason provided)"
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    if not value:
        return "(no reason provided)"
    if len(value) > _MAX_REASON_CHARS:
        return value[:_MAX_REASON_CHARS]
    return value


__all__ = ["classify_llm"]
