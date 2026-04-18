#!/usr/bin/env bash
# Stop hook — Regression guard.
#
# Runs when the Claude Code session is about to terminate. If files were
# written during this session, auto-detect the project's build/test command
# and run it under a tight timeout. If the check fails (non-zero exit within
# the timeout), emit a JSON block decision so the session is held open with
# a system-reminder telling the agent what broke.
#
# Contract:
#   - Input: Claude Code hook JSON on stdin (plus `stop_hook_active` field)
#   - Output: on regression, single JSON object:
#       {"decision":"block","reason":"<short summary>"}
#     on success, nothing
#   - Exit: always 0 (non-zero means hook error, not a block signal)
#
# Key design rules (from spec §9.5):
#   - Hard 15-second total deadline; never wedge the user.
#   - Skip if stop_hook_active is true (prevents infinite loops).
#   - Skip if no scratch file / no changed files.
#   - Skip if no supported project manifest is found.
#   - TIMEOUT is tolerated: best-effort check, not a true signal of breakage.
#     Only a real non-zero exit from the test command blocks.
#
# Note: macOS does not ship `timeout(1)`, so we use a perl-based wrapper.
#
set -uo pipefail
umask 077

# Exit codes used in the script:
#   0       success (no block emitted OR block JSON written to stdout)
#   124     timeout (treated as success — DO NOT block)
#   non-0   failed check (emit block JSON and exit 0)
#
# `set -e` is deliberately OFF; we want to make decisions based on exit codes.

# Fail open: any unexpected error → exit 0.
handle_err() { exit 0; }
trap handle_err ERR

# jq is required for parsing the hook payload.
command -v jq >/dev/null 2>&1 || exit 0

# -----------------------------------------------------------------------------
# Perl-based timeout wrapper (portable across macOS + Linux).
# Usage: _timeout <seconds> <cmd> [args...]
# Exit: 124 on timeout, child exit code otherwise.
# -----------------------------------------------------------------------------
_timeout() {
  local secs="$1"; shift
  perl -e '
    use strict; use warnings;
    my $secs = shift @ARGV;
    my $pid = fork();
    die "fork failed: $!" unless defined $pid;
    if ($pid == 0) {
      # Child: execute the command
      setpgrp(0, 0);
      exec { $ARGV[0] } @ARGV or die "exec failed: $!";
    }
    # Parent: wait with alarm
    local $SIG{ALRM} = sub {
      # Kill whole process group to catch grandchildren
      kill "TERM", -$pid;
      sleep 1;
      kill "KILL", -$pid;
      exit 124;
    };
    alarm $secs;
    waitpid($pid, 0);
    my $status = $?;
    alarm 0;
    if ($status == -1) { exit 1; }
    if ($status & 127) { exit 128 + ($status & 127); }
    exit ($status >> 8);
  ' "$secs" "$@"
}

# -----------------------------------------------------------------------------
# Read hook input.
# -----------------------------------------------------------------------------
input="$(cat 2>/dev/null || true)"
[[ -z "$input" ]] && exit 0

stop_hook_active=$(printf '%s' "$input" | jq -r '.stop_hook_active // false' 2>/dev/null || true)
session_id=$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null || true)

# If the stop hook already fired once and the agent chose to continue, bail
# immediately. This is the infinite-loop breaker per the spec.
[[ "$stop_hook_active" == "true" ]] && exit 0

# Without a session id we don't know which scratch file to inspect.
[[ -z "$session_id" ]] && exit 0

scratch="/tmp/swarm-files-changed-$session_id"
[[ ! -f "$scratch" ]] && exit 0

# Collect unique changed files. Skip if empty.
changed=$(sort -u "$scratch" 2>/dev/null | grep -v '^$' || true)
[[ -z "$changed" ]] && exit 0

# -----------------------------------------------------------------------------
# Find the project root. Look for a manifest in the nearest common ancestor
# of the changed files. For simplicity, walk upward from the first changed
# path until we hit something we recognise.
# -----------------------------------------------------------------------------
first_file=$(printf '%s\n' "$changed" | head -n1)
dir="$(dirname "$first_file")"

# Walk up until we find a project manifest or hit root.
project_root=""
while [[ "$dir" != "/" && -n "$dir" ]]; do
  for marker in pyproject.toml tox.ini package.json Makefile Cargo.toml go.mod; do
    if [[ -f "$dir/$marker" ]]; then
      project_root="$dir"
      break 2
    fi
  done
  dir="$(dirname "$dir")"
done

# No recognised project root → nothing to check.
[[ -z "$project_root" ]] && exit 0

# -----------------------------------------------------------------------------
# Auto-detect build + test command. First hit wins (highest priority at top).
# -----------------------------------------------------------------------------
cmd=""
cmd_label=""
build_cmd=""
build_label=""

if [[ -f "$project_root/pyproject.toml" && -f "$project_root/tox.ini" ]]; then
  cmd_label="tox"
  cmd="tox -q -e py310 -- -x"
elif [[ -f "$project_root/pyproject.toml" ]]; then
  # Prefer pytest when declared in deps.
  if grep -qE "(^|[^a-z])pytest" "$project_root/pyproject.toml" 2>/dev/null; then
    cmd_label="pytest"
    cmd="pytest -q -x"
  fi
elif [[ -f "$project_root/package.json" ]]; then
  if jq -e '.scripts.test' "$project_root/package.json" >/dev/null 2>&1; then
    cmd_label="npm test"
    cmd="npm test --silent"
  fi
elif [[ -f "$project_root/Makefile" ]]; then
  if grep -qE '^test:' "$project_root/Makefile" 2>/dev/null; then
    cmd_label="make test"
    cmd="make -C '$project_root' test"
  fi
elif [[ -f "$project_root/Cargo.toml" ]]; then
  cmd_label="cargo test"
  cmd="cargo test --quiet"
elif [[ -f "$project_root/go.mod" ]]; then
  cmd_label="go test"
  cmd="go test ./..."
fi

# Nothing we can run → bail.
[[ -z "$cmd" ]] && exit 0

# -----------------------------------------------------------------------------
# Optional: quick build / type check (5s). Currently: tsc --noEmit if
# tsconfig.json exists. mypy is left out by default because running it fast
# is tricky; best-effort only.
# -----------------------------------------------------------------------------
if [[ -f "$project_root/tsconfig.json" ]] && command -v tsc >/dev/null 2>&1; then
  build_label="tsc --noEmit"
  build_cmd="tsc --noEmit -p '$project_root'"
fi

# -----------------------------------------------------------------------------
# Helper: emit JSON block and exit 0.
# -----------------------------------------------------------------------------
emit_block() {
  local reason="$1"
  # jq handles escaping cleanly.
  jq -nc --arg r "$reason" '{decision:"block",reason:$r}'
  exit 0
}

# Short file list for the reason message (first 3 files).
file_list=$(printf '%s\n' "$changed" | head -n3 | tr '\n' ' ' | sed 's/ $//')
file_count=$(printf '%s\n' "$changed" | wc -l | tr -d ' ')

# -----------------------------------------------------------------------------
# Run build check (5s), if applicable. Capture output for the reason message.
# -----------------------------------------------------------------------------
if [[ -n "$build_cmd" ]]; then
  build_out_file="$(mktemp -t swarm-regguard-build.XXXXXX)"
  # shellcheck disable=SC2086
  (cd "$project_root" && _timeout 5 sh -c "$build_cmd") >"$build_out_file" 2>&1
  build_rc=$?
  if [[ $build_rc -ne 0 && $build_rc -ne 124 ]]; then
    head_lines=$(head -n5 "$build_out_file" | tr '\n' ' ' | cut -c1-500)
    rm -f "$build_out_file"
    emit_block "build check ($build_label) failed after edits to $file_count file(s) [$file_list]: $head_lines"
  fi
  rm -f "$build_out_file"
fi

# -----------------------------------------------------------------------------
# Run test check (10s). Capture output; block on non-zero non-timeout exit.
# -----------------------------------------------------------------------------
test_out_file="$(mktemp -t swarm-regguard-test.XXXXXX)"
# shellcheck disable=SC2086
(cd "$project_root" && _timeout 10 sh -c "$cmd") >"$test_out_file" 2>&1
test_rc=$?

if [[ $test_rc -ne 0 && $test_rc -ne 124 ]]; then
  head_lines=$(head -n5 "$test_out_file" | tr '\n' ' ' | cut -c1-500)
  rm -f "$test_out_file"
  emit_block "regression: $cmd_label failed after edits to $file_count file(s) [$file_list]: $head_lines"
fi

rm -f "$test_out_file"
exit 0
