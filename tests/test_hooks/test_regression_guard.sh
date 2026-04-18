#!/usr/bin/env bash
# Test harness for swarm Stop-hook regression guard + PostToolUse file tracker.
#
# Exercises both hooks with synthetic inputs and asserts behavior.
#
# Usage:
#   bash tests/test_hooks/test_regression_guard.sh
#
# Exit 0 if all tests pass, 1 otherwise.
#
set -u

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
hook_dir="$script_dir/../../swarm/hooks"
post_tool_hook="$hook_dir/post_tool_use_track_files.sh"
stop_hook="$hook_dir/stop_regression_check.sh"

pass=0
fail=0
test_tmp="$(mktemp -d -t swarm-hook-tests.XXXXXX)"

cleanup() {
  rm -rf "$test_tmp"
  # Clean up any per-test scratch files we generated.
  for sid in sid_edit sid_non_write sid_multiedit sid_write sid_malformed \
             sid_stop_active sid_stop_nofile sid_stop_noroot \
             sid_stop_pass sid_stop_fail sid_stop_timeout sid_write_notebook; do
    rm -f "/tmp/swarm-files-changed-$sid"
  done
}
trap cleanup EXIT

check() {
  local label="$1"; shift
  if "$@"; then
    echo "PASS: $label"
    pass=$((pass + 1))
  else
    echo "FAIL: $label"
    fail=$((fail + 1))
  fi
}

# -----------------------------------------------------------------------------
# Test helpers
# -----------------------------------------------------------------------------

# Run the post-tool-use hook with JSON input; return its exit code.
run_post() {
  local json="$1"
  printf '%s' "$json" | bash "$post_tool_hook"
}

# Run the stop hook with JSON input; capture stdout + exit code.
# Sets $stop_stdout and $stop_exit.
run_stop() {
  local json="$1"
  stop_stdout=$(printf '%s' "$json" | bash "$stop_hook" 2>/dev/null)
  stop_exit=$?
}

# -----------------------------------------------------------------------------
# TEST 1: Edit tool + session_id + file_path → scratch records the path
# -----------------------------------------------------------------------------
test_edit_records_path() {
  local sid="sid_edit"
  local scratch="/tmp/swarm-files-changed-$sid"
  rm -f "$scratch"

  local json
  json=$(cat <<EOF
{"tool_name":"Edit","tool_input":{"file_path":"/abs/path/foo.py","old_string":"a","new_string":"b"},"session_id":"$sid"}
EOF
)
  run_post "$json"
  local rc=$?

  [[ $rc -eq 0 ]] || return 1
  [[ -f "$scratch" ]] || return 1
  grep -qxF "/abs/path/foo.py" "$scratch" || return 1
  return 0
}

# -----------------------------------------------------------------------------
# TEST 2: Non-write tool (Grep) → scratch file is NOT created
# -----------------------------------------------------------------------------
test_non_write_tool_ignored() {
  local sid="sid_non_write"
  local scratch="/tmp/swarm-files-changed-$sid"
  rm -f "$scratch"

  local json
  json=$(cat <<EOF
{"tool_name":"Grep","tool_input":{"pattern":"TODO"},"session_id":"$sid"}
EOF
)
  run_post "$json"
  local rc=$?

  [[ $rc -eq 0 ]] || return 1
  [[ ! -f "$scratch" ]] || return 1
  return 0
}

# -----------------------------------------------------------------------------
# TEST 3: Missing session_id → exit 0, no scratch file
# -----------------------------------------------------------------------------
test_missing_session_id() {
  # Enumerate any stray scratch files before.
  local before_count
  before_count=$(find /tmp -maxdepth 1 -name 'swarm-files-changed-*' 2>/dev/null | wc -l | tr -d ' ')

  local json
  json=$(cat <<EOF
{"tool_name":"Edit","tool_input":{"file_path":"/abs/path/x.py"}}
EOF
)
  run_post "$json"
  local rc=$?

  local after_count
  after_count=$(find /tmp -maxdepth 1 -name 'swarm-files-changed-*' 2>/dev/null | wc -l | tr -d ' ')

  [[ $rc -eq 0 ]] || return 1
  [[ "$before_count" == "$after_count" ]] || return 1
  return 0
}

# -----------------------------------------------------------------------------
# TEST 4: MultiEdit → path recorded ONCE (not once per edit)
# -----------------------------------------------------------------------------
test_multiedit_single_path() {
  local sid="sid_multiedit"
  local scratch="/tmp/swarm-files-changed-$sid"
  rm -f "$scratch"

  local json
  json=$(cat <<EOF
{"tool_name":"MultiEdit","tool_input":{"file_path":"/abs/path/bar.py","edits":[{"old_string":"a","new_string":"b"},{"old_string":"c","new_string":"d"},{"old_string":"e","new_string":"f"}]},"session_id":"$sid"}
EOF
)
  run_post "$json"
  local rc=$?

  [[ $rc -eq 0 ]] || return 1
  [[ -f "$scratch" ]] || return 1
  # Exactly one line in the scratch file.
  local lines
  lines=$(wc -l < "$scratch" | tr -d ' ')
  [[ "$lines" -eq 1 ]] || return 1
  grep -qxF "/abs/path/bar.py" "$scratch" || return 1
  return 0
}

# -----------------------------------------------------------------------------
# TEST 5: Empty / malformed JSON → exit 0, no crash
# -----------------------------------------------------------------------------
test_malformed_json() {
  # Empty stdin
  printf '' | bash "$post_tool_hook"
  [[ $? -eq 0 ]] || return 1

  # Not JSON at all
  printf 'this is not json' | bash "$post_tool_hook"
  [[ $? -eq 0 ]] || return 1

  # Partially valid JSON with missing fields
  printf '{"garbage":true}' | bash "$post_tool_hook"
  [[ $? -eq 0 ]] || return 1

  return 0
}

# -----------------------------------------------------------------------------
# TEST 5b: Write tool also recorded
# -----------------------------------------------------------------------------
test_write_tool_recorded() {
  local sid="sid_write"
  local scratch="/tmp/swarm-files-changed-$sid"
  rm -f "$scratch"

  local json
  json=$(cat <<EOF
{"tool_name":"Write","tool_input":{"file_path":"/abs/path/new.py","content":"print(1)"},"session_id":"$sid"}
EOF
)
  run_post "$json"
  local rc=$?

  [[ $rc -eq 0 ]] || return 1
  grep -qxF "/abs/path/new.py" "$scratch" || return 1
  return 0
}

# -----------------------------------------------------------------------------
# TEST 5c: NotebookEdit uses notebook_path
# -----------------------------------------------------------------------------
test_notebook_edit_recorded() {
  local sid="sid_write_notebook"
  local scratch="/tmp/swarm-files-changed-$sid"
  rm -f "$scratch"

  local json
  json=$(cat <<EOF
{"tool_name":"NotebookEdit","tool_input":{"notebook_path":"/abs/path/nb.ipynb","new_source":"print(1)"},"session_id":"$sid"}
EOF
)
  run_post "$json"
  local rc=$?

  [[ $rc -eq 0 ]] || return 1
  grep -qxF "/abs/path/nb.ipynb" "$scratch" || return 1
  return 0
}

# -----------------------------------------------------------------------------
# STOP HOOK TESTS
# -----------------------------------------------------------------------------

# TEST 6: stop_hook_active=true → exit 0 immediately, no output
test_stop_hook_active() {
  local sid="sid_stop_active"
  local scratch="/tmp/swarm-files-changed-$sid"
  # Seed with a fake file so we can confirm the hook didn't use it.
  printf '/nonexistent/x.py\n' > "$scratch"

  local json
  json=$(cat <<EOF
{"session_id":"$sid","stop_hook_active":true}
EOF
)
  run_stop "$json"

  [[ $stop_exit -eq 0 ]] || return 1
  [[ -z "$stop_stdout" ]] || return 1
  return 0
}

# TEST 7: No scratch file → exit 0, no output
test_stop_no_scratch() {
  local sid="sid_stop_nofile"
  rm -f "/tmp/swarm-files-changed-$sid"

  local json
  json=$(cat <<EOF
{"session_id":"$sid","stop_hook_active":false}
EOF
)
  run_stop "$json"

  [[ $stop_exit -eq 0 ]] || return 1
  [[ -z "$stop_stdout" ]] || return 1
  return 0
}

# TEST 8: Scratch file with no detectable project root → exit 0
test_stop_no_project_root() {
  local sid="sid_stop_noroot"
  local scratch="/tmp/swarm-files-changed-$sid"
  # Path under /tmp — no pyproject.toml up the tree.
  local orphan_dir
  orphan_dir="$(mktemp -d -t swarm-orphan.XXXXXX)"
  printf '%s/loose.py\n' "$orphan_dir" > "$scratch"

  local json
  json=$(cat <<EOF
{"session_id":"$sid","stop_hook_active":false}
EOF
)
  run_stop "$json"
  local rc=$stop_exit
  local out="$stop_stdout"

  rm -rf "$orphan_dir"

  [[ $rc -eq 0 ]] || return 1
  [[ -z "$out" ]] || return 1
  return 0
}

# -----------------------------------------------------------------------------
# Helpers for synthetic Python projects
# -----------------------------------------------------------------------------

# Build a temp python project with pyproject.toml and a single pytest.
# Args: $1 = passing | failing | slow
# Writes project dir to $REPLY.
make_python_project() {
  local kind="$1"
  local root
  root="$(mktemp -d -t swarm-py-$kind.XXXXXX)"

  # Minimal pyproject.toml declaring pytest.
  cat > "$root/pyproject.toml" <<'EOF'
[project]
name = "regression-guard-test"
version = "0.0.1"
requires-python = ">=3.8"

[tool.pytest.ini_options]
addopts = "-q"
EOF

  # Tell the stop hook to pick pytest (pytest is in pyproject).
  mkdir -p "$root/tests"

  case "$kind" in
    passing)
      cat > "$root/tests/test_smoke.py" <<'EOF'
def test_ok():
    assert 1 + 1 == 2
EOF
      ;;
    failing)
      cat > "$root/tests/test_smoke.py" <<'EOF'
def test_broken():
    assert 1 + 1 == 3, "intentional failure for regression guard test"
EOF
      ;;
    slow)
      cat > "$root/tests/test_smoke.py" <<'EOF'
import time
def test_slow():
    # Sleep longer than the 10s test timeout so the hook must treat it
    # as a timeout (tolerated) and not a failure.
    time.sleep(30)
    assert True
EOF
      ;;
  esac

  REPLY="$root"
}

# TEST 9: Python project with passing tests → exit 0, no block
test_stop_python_passing() {
  command -v pytest >/dev/null 2>&1 || { echo "    (skipped: pytest not installed)"; return 0; }

  local sid="sid_stop_pass"
  local scratch="/tmp/swarm-files-changed-$sid"
  make_python_project "passing"
  local root="$REPLY"

  printf '%s/tests/test_smoke.py\n' "$root" > "$scratch"

  local json
  json=$(cat <<EOF
{"session_id":"$sid","stop_hook_active":false}
EOF
)
  run_stop "$json"
  local rc=$stop_exit
  local out="$stop_stdout"

  rm -rf "$root"

  [[ $rc -eq 0 ]] || return 1
  # No block JSON should be emitted on a pass.
  [[ "$out" != *'"decision":"block"'* ]] || return 1
  return 0
}

# TEST 10: Python project with failing tests → block JSON on stdout
test_stop_python_failing() {
  command -v pytest >/dev/null 2>&1 || { echo "    (skipped: pytest not installed)"; return 0; }

  local sid="sid_stop_fail"
  local scratch="/tmp/swarm-files-changed-$sid"
  make_python_project "failing"
  local root="$REPLY"

  printf '%s/tests/test_smoke.py\n' "$root" > "$scratch"

  local json
  json=$(cat <<EOF
{"session_id":"$sid","stop_hook_active":false}
EOF
)
  run_stop "$json"
  local rc=$stop_exit
  local out="$stop_stdout"

  rm -rf "$root"

  [[ $rc -eq 0 ]] || return 1
  # stdout must contain the block decision.
  [[ "$out" == *'"decision":"block"'* ]] || { echo "    stdout: $out"; return 1; }
  # And a reason field with useful content.
  [[ "$out" == *'"reason"'* ]] || return 1
  return 0
}

# TEST 11: Timeout during tests → exit 0, NO block (timeout is tolerated)
test_stop_python_timeout() {
  command -v pytest >/dev/null 2>&1 || { echo "    (skipped: pytest not installed)"; return 0; }

  local sid="sid_stop_timeout"
  local scratch="/tmp/swarm-files-changed-$sid"
  make_python_project "slow"
  local root="$REPLY"

  printf '%s/tests/test_smoke.py\n' "$root" > "$scratch"

  local json
  json=$(cat <<EOF
{"session_id":"$sid","stop_hook_active":false}
EOF
)
  run_stop "$json"
  local rc=$stop_exit
  local out="$stop_stdout"

  rm -rf "$root"

  [[ $rc -eq 0 ]] || return 1
  # Timeout must NOT emit a block.
  if [[ "$out" == *'"decision":"block"'* ]]; then
    echo "    stdout (unexpected block): $out"
    return 1
  fi
  return 0
}

# -----------------------------------------------------------------------------
# Run all tests
# -----------------------------------------------------------------------------

# Sanity check: hook files exist and are executable.
check "hook files exist and executable" \
  bash -c "[[ -x \"$post_tool_hook\" && -x \"$stop_hook\" ]]"

# PostToolUse tests
check "Edit tool records file path"            test_edit_records_path
check "Non-write tool (Grep) is ignored"        test_non_write_tool_ignored
check "Missing session_id exits 0 silently"     test_missing_session_id
check "MultiEdit records path exactly once"     test_multiedit_single_path
check "Malformed/empty JSON does not crash"     test_malformed_json
check "Write tool records file path"            test_write_tool_recorded
check "NotebookEdit records notebook_path"      test_notebook_edit_recorded

# Stop hook tests
check "Stop: stop_hook_active=true exits 0"     test_stop_hook_active
check "Stop: no scratch file exits 0"           test_stop_no_scratch
check "Stop: no project root exits 0"           test_stop_no_project_root
check "Stop: passing tests do not block"        test_stop_python_passing
check "Stop: failing tests emit block JSON"     test_stop_python_failing
check "Stop: test timeout does NOT block"       test_stop_python_timeout

echo "--- $pass passed, $fail failed ---"
[[ $fail -eq 0 ]]
