"""Notifier specialist — tails findings and interventions, emits OS
notifications for critical events.

Wired into launch.sh's spawn list. Subject to exit_if_launcher_dead() just
like every other specialist; when the launcher is gone, it exits cleanly."""

from __future__ import annotations

import logging
import os
import sys
import time

from swarm.lib.heartbeat import beat
from swarm.lib.launcher_liveness import exit_if_launcher_dead
from swarm.lib.notify_os import notify as default_notify
from swarm.lib.paths import (
    ensure_session_dirs,
    findings_path,
    interventions_path,
    session_dir,
)
from swarm.schemas.finding import Finding
from swarm.schemas.intervention import Intervention

LOG = logging.getLogger("swarm.notifier")

NOTIFIER_CURSOR_FILENAME = "notifier.cursor"
_INTERVENTIONS_CURSOR_FILENAME = "notifier.interventions.cursor"

_NOTIFY_SUBTYPES = {
    "tamper_detected",
    "hold_window_met",
    "mission_level_alert_pending",
    "specialist_degraded",
    "mock_out",
    "scope_reduction",
    "dep_violation",
}


def should_notify_finding(f: Finding) -> tuple[bool, str]:
    """Return (yes_no, title_hint)."""
    if getattr(f, "severity", None) == "critical":
        return True, f"swarm critical: {f.subtype}"
    if f.subtype in _NOTIFY_SUBTYPES:
        return True, f"swarm: {f.subtype}"
    return False, ""


def should_notify_intervention(iv: Intervention) -> tuple[bool, str]:
    if iv.tier == "mission_complete":
        return True, "swarm: mission complete"
    return False, ""


def format_notification(f_or_iv) -> tuple[str, str]:
    """Return (title, body). Body capped at 400 chars."""
    if isinstance(f_or_iv, Finding):
        _, title = should_notify_finding(f_or_iv)
        body = (f_or_iv.verdict or f_or_iv.subtype or "")[:400]
    elif isinstance(f_or_iv, Intervention):
        _, title = should_notify_intervention(f_or_iv)
        body = (f_or_iv.reason or "")[:400]
    else:
        title = "swarm"
        body = str(f_or_iv)[:400]
    return title, body


def _read_cursor(session_id: str, filename: str) -> int:
    path = session_dir(session_id) / filename
    if not path.exists():
        return 0
    try:
        return int(path.read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def _write_cursor(session_id: str, filename: str, offset: int) -> None:
    path = session_dir(session_id) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{offset}\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _scan_jsonl_from_cursor(
    file_path, cursor: int
) -> tuple[list[str], int]:
    """Read lines after `cursor`. Partial trailing line (no newline) is NOT
    included and cursor is NOT advanced past it. Returns (complete_lines, new_cursor)."""
    if not file_path.exists():
        return [], cursor
    try:
        with file_path.open("rb") as f:
            f.seek(cursor)
            chunk = f.read()
    except OSError:
        return [], cursor
    if not chunk:
        return [], cursor
    # Find last newline; everything up to and including it is complete
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        # Entire chunk is a partial line — nothing complete yet
        return [], cursor
    complete_bytes = chunk[: last_nl + 1]
    new_cursor = cursor + len(complete_bytes)
    try:
        text = complete_bytes.decode("utf-8", errors="replace")
    except Exception:
        return [], cursor
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return lines, new_cursor


def process_new_findings(session_id: str, *, notify_fn=None) -> int:
    """Read new findings since cursor, notify, advance cursor. Returns count sent."""
    notify_fn = notify_fn or default_notify
    fp = findings_path(session_id)
    cursor = _read_cursor(session_id, NOTIFIER_CURSOR_FILENAME)
    lines, new_cursor = _scan_jsonl_from_cursor(fp, cursor)
    quiet = os.environ.get("SWARM_QUIET") == "1"
    sent = 0
    for line in lines:
        try:
            f = Finding.model_validate_json(line)
        except Exception:
            continue
        yes, _ = should_notify_finding(f)
        if not yes:
            continue
        if quiet:
            continue
        title, body = format_notification(f)
        try:
            notify_fn(title, body)
            sent += 1
        except Exception as e:
            LOG.warning("notify_fn failed: %s", e)
    # Always advance cursor — even when quiet, we don't want to re-fire on un-quiet
    _write_cursor(session_id, NOTIFIER_CURSOR_FILENAME, new_cursor)
    return sent


def process_new_interventions(session_id: str, *, notify_fn=None) -> int:
    notify_fn = notify_fn or default_notify
    ip = interventions_path(session_id)
    cursor = _read_cursor(session_id, _INTERVENTIONS_CURSOR_FILENAME)
    lines, new_cursor = _scan_jsonl_from_cursor(ip, cursor)
    quiet = os.environ.get("SWARM_QUIET") == "1"
    sent = 0
    for line in lines:
        try:
            iv = Intervention.model_validate_json(line)
        except Exception:
            continue
        yes, _ = should_notify_intervention(iv)
        if not yes:
            continue
        if quiet:
            continue
        title, body = format_notification(iv)
        try:
            notify_fn(title, body)
            sent += 1
        except Exception as e:
            LOG.warning("notify_fn failed: %s", e)
    _write_cursor(session_id, _INTERVENTIONS_CURSOR_FILENAME, new_cursor)
    return sent


# -------- daemon --------


def main(session_id: str, period_sec: float = 3.0) -> None:
    ensure_session_dirs(session_id)
    exit_if_launcher_dead(session_id, LOG)
    cycles = 0
    LOG.info("notifier starting for session=%s", session_id)
    while True:
        exit_if_launcher_dead(session_id, LOG)
        try:
            process_new_findings(session_id)
            process_new_interventions(session_id)
        except Exception as e:
            LOG.warning("notifier tick failed: %s", e)
        cycles += 1
        beat(session_id, "notifier", cycles)
        time.sleep(period_sec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if len(sys.argv) < 2:
        print("usage: notifier.py <session_id>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
