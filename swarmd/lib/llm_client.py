"""Anthropic LLM client for the swarm project.

Wraps the Anthropic SDK with env-var config, exponential backoff retries,
and a simple text-extraction interface.

Configure via env vars:
- ``ANTHROPIC_API_KEY``   — required unless ``ANTHROPIC_BASE_URL`` points at a
                            proxy that injects auth.
- ``ANTHROPIC_BASE_URL``  — optional override for SDK base URL (proxies, etc).
                            Unset → SDK default (api.anthropic.com).
- ``ANTHROPIC_MODEL``     — optional model override; default is the Sonnet
                            identifier below.
"""

from __future__ import annotations

import os
import time

import anthropic

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Empty string → SDK uses its own default (api.anthropic.com).
_DEFAULT_BASE_URL = ""
_DEFAULT_API_KEY = ""
_DEFAULT_MODEL = "claude-sonnet-4-6"

# Retry config: max 3 retries = 4 total attempts; backoff 1s, 2s, 4s
_MAX_RETRIES = 3
_BACKOFF_SECONDS = [1, 2, 4]

# Errors that are retryable (by type)
_RETRYABLE_TYPES = (
    anthropic.RateLimitError,
    anthropic.APIConnectionError,
)

# Errors that must NOT be retried
_FATAL_TYPES = (
    anthropic.BadRequestError,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Raised on unrecoverable LLM call failures."""


def call(
    prompt: str,
    *,
    model: str = "",
    timeout: float = 30.0,
    max_tokens: int = 4096,
    system: str | None = None,
) -> str:
    """Send prompt to the gateway. Return plain text response.

    Raises LLMError on unrecoverable failures (after retries).
    """
    # Resolve config — each call re-reads env vars so tests can monkeypatch.
    base_url = os.environ.get("ANTHROPIC_BASE_URL", _DEFAULT_BASE_URL)
    api_key = os.environ.get("ANTHROPIC_API_KEY", _DEFAULT_API_KEY)
    resolved_model = model or os.environ.get("SWARM_LLM_MODEL", _DEFAULT_MODEL)

    # Pass kwargs only when non-empty so the SDK falls back to its own defaults.
    client_kwargs: dict = {}
    if base_url:
        client_kwargs["base_url"] = base_url
    if api_key:
        client_kwargs["api_key"] = api_key
    client = anthropic.Anthropic(**client_kwargs)

    kwargs: dict = {
        "model": resolved_model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "timeout": timeout,
    }
    if system is not None:
        kwargs["system"] = system

    last_exc: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = client.messages.create(**kwargs)
            # Extract text from all content blocks that have a .text attribute.
            parts = [block.text for block in response.content if hasattr(block, "text")]
            return "".join(parts)
        except _FATAL_TYPES as exc:
            raise LLMError(str(exc)) from exc
        except _RETRYABLE_TYPES as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_SECONDS[attempt])
        except anthropic.APIStatusError as exc:
            # Retry on 5xx and 429 by status code for errors not already caught above.
            if exc.status_code in (429, 500, 502, 503, 504):
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    time.sleep(_BACKOFF_SECONDS[attempt])
            else:
                raise LLMError(str(exc)) from exc
        except Exception as exc:
            raise LLMError(str(exc)) from exc

    raise LLMError(f"LLM call failed after {_MAX_RETRIES + 1} attempts") from last_exc
