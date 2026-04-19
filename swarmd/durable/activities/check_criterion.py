"""``check_criterion`` Temporal activity — runs one criterion's shell check.

Per spec §6.3 row ``check_criterion``:

    check_criterion(criterion) → {pass, exit_code, stdout_tail, stderr_tail, duration_ms}

    Short (seconds). Subject to ``criterion.timeout_sec``. Idempotent.

Implementation contract (from plan Task 4):

* Signature: ``async def check_criterion(criterion, workspace) -> CriterionCheckResult``.
* Runs ``criterion.check`` as a shell subprocess with ``cwd=workspace`` and
  the current process environment inherited.
* If the subprocess exceeds ``criterion.timeout_sec`` seconds, the activity
  returns ``pass_=False, exit_code=-1, stderr_tail="timeout after Ns"``
  instead of raising. The Temporal retry layer handles real infra failures;
  criterion timeouts are ordinary information for the mission workflow.
* ``stdout`` and ``stderr`` are captured in full but only the last 2000
  characters are returned, so a runaway check can't blow up Temporal history.
* Pure function, stdlib only (``asyncio.create_subprocess_shell``). No
  Temporal primitives beyond the ``@activity.defn`` decorator.

The activity is declared ``idempotent`` at the schema level
(``criterion.idempotent``), so the mission workflow is free to re-run it
after a worker crash.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

from temporalio import activity

from swarmd.schemas.criterion import Criterion

# Maximum number of characters of stdout/stderr kept on the result. 2000 is a
# deliberate tradeoff: enough to debug most failed checks from the Temporal
# UI, short enough that even thousands of criterion checks cannot bloat the
# workflow history to an unreasonable size.
_TAIL_CHARS = 2000


@dataclass
class CriterionCheckResult:
    """One criterion's check result, serialized back to the workflow.

    Fields match the spec §6.3 row (``{pass, exit_code, stdout_tail,
    stderr_tail, duration_ms}``) plus ``criterion_id`` so the workflow can
    correlate parallel fan-out results to criteria without relying on list
    order. ``pass_`` (with trailing underscore) is the Python attribute;
    ``pass`` is a reserved word.
    """

    criterion_id: str
    pass_: bool
    exit_code: int
    stdout_tail: str
    stderr_tail: str
    duration_ms: int


@activity.defn(name="check_criterion")
async def check_criterion(
    criterion: Criterion, workspace: str
) -> CriterionCheckResult:
    """Run one criterion's shell command and report pass/fail.

    Idempotent: safe to call multiple times. Subject to
    ``criterion.timeout_sec``. Returns a :class:`CriterionCheckResult`
    rather than raising for normal criterion failures (non-zero exit,
    timeout). Only raises if the subprocess cannot be created at all.
    """
    start = time.monotonic()
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_shell(
            criterion.check,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_inherit_env(),
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=criterion.timeout_sec,
        )
        exit_code = proc.returncode if proc.returncode is not None else 0
        return CriterionCheckResult(
            criterion_id=criterion.id,
            pass_=(exit_code == 0),
            exit_code=exit_code,
            stdout_tail=_tail(stdout, _TAIL_CHARS),
            stderr_tail=_tail(stderr, _TAIL_CHARS),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
    except asyncio.TimeoutError:
        # Timeout is a normal criterion outcome — don't raise, just report it.
        # Terminate the runaway subprocess so we don't leak it; ignore failure
        # because by the time we kill it it may already be a zombie.
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        return CriterionCheckResult(
            criterion_id=criterion.id,
            pass_=False,
            exit_code=-1,
            stdout_tail="",
            stderr_tail=f"timeout after {criterion.timeout_sec}s",
            duration_ms=int((time.monotonic() - start) * 1000),
        )


def _inherit_env() -> dict[str, str]:
    """Return a fresh copy of the current environment for the subprocess.

    Returned as a plain ``dict`` so the caller can mutate without affecting
    the parent process. Factored into a helper so tests can monkeypatch it.
    """

    return dict(os.environ)


def _tail(b: bytes, n: int) -> str:
    """Decode ``b`` and return at most ``n`` characters from the tail.

    Uses ``errors="replace"`` so arbitrary subprocess output (including
    non-UTF-8 bytes like a compiled binary's stderr) never raises.
    """

    s = b.decode("utf-8", errors="replace")
    return s[-n:] if len(s) > n else s
