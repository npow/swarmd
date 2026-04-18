#!/usr/bin/env bash
#
# Swarm v0.2 launcher.
#
# Usage:  ./swarm/launch.sh <mission.yaml>
#
# Sets up a session, pins mission hashes, starts background specialists in
# their own process group, and launches claude with the mission. On exit,
# kills the entire group so no specialist daemon leaks.
#
# v0 limits:
# - No UID separation. Everything runs as $USER.
# - No immutable bits. Lockdown is via Claude Code permission denies.

set -euo pipefail

# v6: raise soft FD limit so daemons don't exhaust the default 256 on macOS
# or 1024 on Linux. 65536 is well under most kernel hard limits.
ulimit -n 65536 2>/dev/null || true

# Run in a dedicated process group so we can kill descendants on exit.
set -m 2>/dev/null || true

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
# Remember our process group (the shell pid) for trap cleanup.
LAUNCHER_PGID=$$

MISSION_SRC="${1:-}"
if [[ -z "$MISSION_SRC" ]]; then
  echo "usage: $0 <mission.yaml>" >&2
  exit 2
fi
if [[ ! -f "$MISSION_SRC" ]]; then
  echo "mission file not found: $MISSION_SRC" >&2
  exit 2
fi

SESSION_ID="$(uuidgen | tr '[:upper:]' '[:lower:]')"
export SESSION_ID

SWARM_ROOT="${SWARM_ROOT:-$HOME/.swarm}"
export SWARM_ROOT
SESSION_STATE="$SWARM_ROOT/state/$SESSION_ID"
MISSION_DIR="$SWARM_ROOT/missions/$SESSION_ID"
LOCK_DIR="${SWARM_CONFIG:-$HOME/.config/swarm}/locks"

echo "Swarm v0.2 launching"
echo "  session_id : $SESSION_ID"
echo "  swarm_root : $SWARM_ROOT"
echo "  mission    : $MISSION_SRC"

# 1. Create session dirs
mkdir -p "$SESSION_STATE" "$SESSION_STATE/health" "$SESSION_STATE/children"
mkdir -p "$MISSION_DIR" "$MISSION_DIR/checks"
mkdir -p "$LOCK_DIR"
touch "$SESSION_STATE/events.jsonl"
touch "$SESSION_STATE/findings.jsonl"
touch "$SESSION_STATE/interventions.jsonl"
touch "$SESSION_STATE/interventions-acked.jsonl"
touch "$SESSION_STATE/tried_strategies.jsonl"
echo '{}' > "$SESSION_STATE/strikes.json"

# 1.5. Write launcher.pid IMMEDIATELY so every specialist spawned in step 7
# can see a valid liveness record from its very first tick. Specialists
# self-terminate if this file is missing or points to a dead pid, which
# is the only defense against orphaned daemons when our trap doesn't fire
# (SIGKILL on this shell, terminal SIGHUP, machine crash). See
# swarm/lib/launcher_liveness.py for the contract.
echo "$$" > "$SESSION_STATE/launcher.pid"

# 2. Copy mission.yaml
cp "$MISSION_SRC" "$MISSION_DIR/mission.yaml"

# Copy check scripts if there's a checks/ dir adjacent to the mission file
MISSION_DIRNAME="$(cd "$(dirname "$MISSION_SRC")" && pwd)"
if [[ -d "$MISSION_DIRNAME/checks" ]]; then
  cp -R "$MISSION_DIRNAME/checks/." "$MISSION_DIR/checks/"
fi

# 3. Validate + extract workspace via a small helper script (no inline-Python
# string interpolation — all paths passed via argv, no shell-injection vector)
WORKSPACE="$(REPO_ROOT="$REPO_ROOT" python3 "$HERE/_launch_helper.py" workspace "$MISSION_DIR/mission.yaml")"
if [[ -z "$WORKSPACE" ]]; then
  echo "failed to extract workspace from mission.yaml" >&2
  exit 2
fi
if [[ ! -d "$WORKSPACE" ]]; then
  echo "workspace dir does not exist: $WORKSPACE" >&2
  exit 2
fi

# 4. Hash-pin mission files (out-of-tree lock + in-tree lock)
REPO_ROOT="$REPO_ROOT" python3 "$HERE/_launch_helper.py" hash-pin "$SESSION_ID" "$MISSION_DIR" "$LOCK_DIR/$SESSION_ID.sha"

# 5. Write WORKSPACE's .claude/settings.json (NOT the cwd's). Idempotent: only
#    write if the destination does not already exist; otherwise back the existing
#    file up and write the swarm one. Either way, leave a marker so we restore
#    on exit.
SETTINGS_DST="$WORKSPACE/.claude/settings.json"
SETTINGS_BAK="$WORKSPACE/.claude/settings.json.swarm-bak.$SESSION_ID"
mkdir -p "$WORKSPACE/.claude"
if [[ -f "$SETTINGS_DST" ]]; then
  cp "$SETTINGS_DST" "$SETTINGS_BAK"
fi
sed "s|~/code/research|$REPO_ROOT|g" "$HERE/settings.json.template" > "$SETTINGS_DST"
echo "wrote $SETTINGS_DST (backup at $SETTINGS_BAK if pre-existing)"

# 6. Mission prose (for the worker prompt)
MISSION_PROSE="$(REPO_ROOT="$REPO_ROOT" python3 "$HERE/_launch_helper.py" prose "$MISSION_DIR/mission.yaml")"

# 7. Spawn specialists in OUR process group; we'll kill the whole group on exit.
# v3 adds supervisor (heartbeat watchdog) and llm_loop (runs LLM critics).
# spawner is a library used by swarm-spawn CLI; no daemon needed in v3 yet
# but the module is exercised here for parity.
pids=()
for d in pattern_detector success_verifier coordinator supervisor llm_loop spawner resource_monitor; do
  if [[ "$d" == "spawner" ]]; then
    # Spawner has no main loop in v3; smoke-import it so the daemon list is complete
    PYTHONPATH="$REPO_ROOT" python3 -c "import swarm.specialists.spawner" \
      > "$SESSION_STATE/$d.log" 2>&1 || true
    continue
  fi
  PYTHONPATH="$REPO_ROOT" python3 -m "swarm.specialists.$d" "$SESSION_ID" \
    > "$SESSION_STATE/$d.log" 2>&1 &
  pids+=($!)
  echo "  started $d (pid=$!)"
done

# 8. Trap: kill specialists, restore settings.json, on any exit
cleanup() {
  echo ""
  echo "Swarm: tearing down session $SESSION_ID"
  # Step 1: SIGTERM tracked PIDs so they exit gracefully
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  sleep 1
  # Step 2: SIGKILL any still-alive tracked PIDs
  for pid in "${pids[@]}"; do
    kill -9 "$pid" 2>/dev/null || true
  done
  # Step 3: SIGKILL any untracked descendants by killing the process group.
  # `kill -- -$PGID` sends the signal to the whole group. This catches any
  # subagents, grandchildren, or orphans that inherited our pgid.
  kill -- -$LAUNCHER_PGID 2>/dev/null || true
  # Restore settings.json
  if [[ -f "$SETTINGS_BAK" ]]; then
    mv "$SETTINGS_BAK" "$SETTINGS_DST"
    echo "restored original $SETTINGS_DST"
  else
    rm -f "$SETTINGS_DST"
    echo "removed swarm-installed $SETTINGS_DST"
  fi
}
trap cleanup EXIT INT TERM

# 9. Launch claude — NOT exec'd, so the trap above runs when claude exits
echo ""
echo "Launching claude with mission..."
echo "---"
cd "$WORKSPACE"
PYTHONPATH="$REPO_ROOT" SESSION_ID="$SESSION_ID" SWARM_ROOT="$SWARM_ROOT" \
  claude --session-id "$SESSION_ID" "$MISSION_PROSE"
