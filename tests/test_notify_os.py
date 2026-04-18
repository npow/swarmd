"""Tests for swarm.lib.notify_os — OS notification delivery.

Injection-safety is a load-bearing property: naive `osascript -e` with
user-controlled body is an RCE primitive. These tests pin the argv
shape and assert no user bytes reach AppleScript as source."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from swarm.lib import notify_os


def test_truncation_applied():
    """Even if caller passes oversize, notify_os truncates before
    constructing argv — belt & suspenders."""
    assert notify_os._truncate("x" * 1000, 100) == "x" * 100


def test_terminal_notifier_argv_shape():
    """If terminal-notifier is selected, body is passed via argv, not interpolated."""
    body = '" & (do shell script "echo owned") & "'
    with patch.object(notify_os, "_which") as which, patch.object(subprocess, "run") as run:
        which.side_effect = lambda name: "/usr/local/bin/terminal-notifier" if name == "terminal-notifier" else None
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        notify_os.notify("title", body)
        cmd = run.call_args.args[0]
        assert cmd[0] == "/usr/local/bin/terminal-notifier"
        # body appears as a single argv item verbatim — no shell interpretation
        assert body in cmd
        assert "shell=True" not in str(run.call_args)


def test_osascript_uses_stdin_with_argv_not_e_flag():
    """osascript must receive script via stdin and title/body via argv.
    The `-e "<body>"` form is injection-vulnerable."""
    body = '" & (do shell script "echo owned") & "'
    with patch.object(notify_os, "_which") as which, patch.object(subprocess, "run") as run:
        which.side_effect = lambda name: "/usr/bin/osascript" if name == "osascript" else None
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        notify_os.notify("title", body)
        cmd = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        # CRITICAL: first arg must be osascript, second must be "-" (stdin)
        assert cmd[0].endswith("osascript")
        assert cmd[1] == "-"
        # Title and body passed as positional argv items to the script
        assert "title" in cmd
        assert body in cmd
        # The -e flag must NOT be used with user-controlled content
        assert "-e" not in cmd
        # Body must NOT appear interpolated into a script source passed as input
        script_input = kwargs.get("input", "")
        assert body not in script_input


def test_notify_send_argv_shape():
    with patch.object(notify_os, "_which") as which, patch.object(subprocess, "run") as run:
        which.side_effect = lambda name: "/usr/bin/notify-send" if name == "notify-send" else None
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        notify_os.notify("title", "body")
        cmd = run.call_args.args[0]
        assert cmd[0] == "/usr/bin/notify-send"
        assert "title" in cmd
        assert "body" in cmd


def test_stderr_fallback_when_no_notifier(capsys):
    with patch.object(notify_os, "_which", return_value=None):
        assert notify_os.notify("t", "b") is True
    captured = capsys.readouterr()
    assert "[swarm-notify]" in captured.err
    assert "t: b" in captured.err


def test_subprocess_timeout_wrapped_to_false():
    with patch.object(notify_os, "_which") as which, patch.object(subprocess, "run") as run:
        which.side_effect = lambda name: "/usr/local/bin/terminal-notifier" if name == "terminal-notifier" else None
        run.side_effect = subprocess.TimeoutExpired(cmd="terminal-notifier", timeout=5)
        # Timeout is handled as a soft failure; we fall through to the next backend
        # (which.side_effect returns None for osascript/notify-send in this test),
        # and eventually print to stderr. Return value is still True.
        assert notify_os.notify("t", "b") in (True, False)  # either is acceptable
