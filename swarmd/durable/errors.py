"""Error taxonomy for durable Temporal activities.

Activities raise classified exceptions so Temporal knows whether to retry
(transient) or fail fast (terminal). `classify_http_status` is a helper that
translates HTTP response codes from upstream services (Anthropic, Claude CLI
sidecars, MCP HTTP bridges, etc.) into the right exception type.

The class-name strings exported via ``NON_RETRYABLE_ERROR_TYPES`` are used
by `temporalio.common.RetryPolicy.non_retryable_error_types` so that Temporal
aborts retries without waiting for `maximum_attempts` when the error is
fundamentally non-recoverable (bad auth, bad request, etc.).

Per spec §7.1.
"""

from __future__ import annotations

# HTTP status codes that indicate a transient failure — safe to retry.
TRANSIENT_HTTP = {408, 424, 429, 500, 502, 503, 504}

# HTTP status codes that indicate a terminal failure — retries will not help.
TERMINAL_HTTP = {400, 401, 403, 404}


class SwarmActivityError(Exception):
    """Base class for all classified activity errors."""

    classification: str = "unknown"


class TransientError(SwarmActivityError):
    """Retryable error — e.g. upstream 5xx, rate-limited, transient network."""

    classification = "transient"

    def __init__(self, message: str = "", retry_after_sec: float | None = None):
        super().__init__(message)
        # When an upstream returns `Retry-After`, callers (or Temporal retry
        # policies) can honor the hint instead of the default backoff.
        self.retry_after_sec = retry_after_sec


class TerminalError(SwarmActivityError):
    """Non-retryable error — retries will not help (bad input, etc.)."""

    classification = "terminal"


class AuthError(TerminalError):
    """401 / 403 — credentials are wrong or expired."""

    pass


class BillingError(TerminalError):
    """Payment/quota exhausted; retries won't resolve."""

    pass


class ContextOverflowError(TerminalError):
    """Model context window exceeded for the current request shape."""

    pass


class UserCancelledError(TerminalError):
    """User explicitly cancelled; don't retry."""

    pass


# Class names (strings) that Temporal should treat as non-retryable.
# Matches the `non_retryable_error_types` kwarg of `RetryPolicy`.
NON_RETRYABLE_ERROR_TYPES = [
    "TerminalError",
    "AuthError",
    "BillingError",
    "ContextOverflowError",
    "UserCancelledError",
]


def classify_http_status(
    status: int, body: bytes, retry_after_sec: float | None = None
) -> None:
    """Raise a classified exception for non-2xx; return None for 2xx.

    Used inside activities to convert HTTP responses into Temporal-friendly
    retryable/non-retryable exceptions.

    Arguments:
        status: HTTP status code from the response.
        body: Raw response body, truncated to 200 bytes for the exception
            message (so we don't leak huge error blobs into Temporal history).
        retry_after_sec: If the upstream returned a `Retry-After` hint, pass
            it here so the resulting `TransientError` carries it as an
            attribute.

    Behavior:
        - 2xx → return None.
        - 401/403 → AuthError (terminal).
        - Other TERMINAL_HTTP → TerminalError.
        - TRANSIENT_HTTP → TransientError (carries retry_after_sec).
        - Unknown status → TransientError (conservative — assume the caller
          can retry rather than fail the whole mission on an unusual code).
    """
    if 200 <= status < 300:
        return
    if status == 401 or status == 403:
        raise AuthError(f"HTTP {status}: {body[:200]!r}")
    if status in TERMINAL_HTTP:
        raise TerminalError(f"HTTP {status}: {body[:200]!r}")
    if status in TRANSIENT_HTTP:
        raise TransientError(
            f"HTTP {status}: {body[:200]!r}", retry_after_sec=retry_after_sec
        )
    # Unknown status code -> treat as transient (conservative).
    raise TransientError(f"HTTP {status} (unclassified): {body[:200]!r}")
