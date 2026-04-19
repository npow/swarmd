#!/usr/bin/env bash
# PostToolUse hook — Track files written/edited in the current session.
#
# Records any file paths written/edited by this tool call to a per-session
# scratch file at /tmp/swarm-files-changed-$CLAUDE_SESSION_ID. The paired
# Stop hook (stop_regression_check.sh) reads this to decide whether to run
# a build/test regression check before letting the session terminate.
#
# Contract:
#   - Input: Claude Code hook JSON on stdin
#       { "tool_name": "Edit", "tool_input": {...}, "session_id": "abc" }
#   - Output: nothing on success (the script is side-effecting only)
#   - Exit: always 0 (never block a session)
#
# Design notes:
#   - Only care about file-writing tools (Edit, Write, NotebookEdit, MultiEdit)
#   - Ignore every other tool call (Grep, Read, Bash, etc.)
#   - umask 077 so scratch files are owner-only
#   - Must be fast (<50ms) — runs after every tool call
#
set -uo pipefail
umask 077

# Fail open: if anything unexpected goes wrong, exit 0 silently.
# Sessions must never be blocked by this hook.
handle_err() { exit 0; }
trap handle_err ERR

# If jq is unavailable, we can't parse the JSON; fail open.
command -v jq >/dev/null 2>&1 || exit 0

# Read the hook payload from stdin.
input="$(cat 2>/dev/null || true)"
[[ -z "$input" ]] && exit 0

session_id=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)
tool_name=$(printf '%s' "$input" | jq -r '.tool_name // empty' 2>/dev/null || true)

# Without a session id we can't choose a scratch file; without a tool name
# we don't know whether this is a writer.
[[ -z "$session_id" ]] && exit 0
[[ -z "$tool_name" ]] && exit 0

# Only file-writing tools are of interest. Everything else is a no-op.
case " Edit Write NotebookEdit MultiEdit " in
  *" $tool_name "*) ;;
  *) exit 0 ;;
esac

scratch="/tmp/swarm-files-changed-$session_id"

# Extract the file path(s). MultiEdit has edits[].file_path but they all
# point at the same file per call — take tool_input.file_path once so we
# don't write the same path repeatedly.
case "$tool_name" in
  MultiEdit)
    # MultiEdit guarantees a single file_path for the whole call — use
    # that instead of iterating over edits[].file_path.
    fp=$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)
    ;;
  Edit|Write|NotebookEdit)
    # NotebookEdit uses notebook_path; Edit/Write use file_path.
    fp=$(printf '%s' "$input" \
      | jq -r '.tool_input.file_path // .tool_input.notebook_path // empty' \
        2>/dev/null || true)
    ;;
  *)
    fp=""
    ;;
esac

# Skip empty paths — we don't want blank lines polluting the scratch file.
[[ -z "$fp" ]] && exit 0

# Append one line per tool call. Ignore write failures (read-only /tmp etc.).
{ printf '%s\n' "$fp" >> "$scratch"; } 2>/dev/null || true

exit 0
