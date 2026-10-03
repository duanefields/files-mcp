#!/usr/bin/env bash
#
# Poll a running files-mcp server and report to a dead-man's-switch.
#
# Intended for cron:
#   */10 * * * * /Users/USERNAME/Code/files-mcp/scripts/healthcheck.sh >> /Users/USERNAME/.files-mcp/check.log 2>&1
#
# Configuration comes from ~/.files-mcp/check.env, if it exists:
#
#   HEALTH_URL=http://127.0.0.1:18794/health
#   PING_URL=https://hc-ping.com/your-uuid-here
#   KUMA_PUSH_URL=http://127.0.0.1:3001/api/push/your-token-here
#   EXPECTED_PYTHON='~/.local/share/uv/python/cpython-3.12.12-macos-aarch64-none/bin/python3.12'
#
# Quote EXPECTED_PYTHON: /health reports a literal ~, and an unquoted ~ in an
# assignment is expanded when this file is sourced, so it would never match.
#
# Either or both of PING_URL (healthchecks.io) and KUMA_PUSH_URL (an Uptime
# Kuma push monitor) can be set. Kuma gets status=up or status=down with the
# problems as the message, and no start ping: a run that hangs shows up as a
# missed heartbeat. Give the monitor a heartbeat interval a little longer than
# the cron interval, so one slow run isn't an outage.
#
# chmod 600 that file: both URLs are capabilities, not just addresses. Use a
# different check or monitor from every other service on the host, or one
# service's silence gets masked by another's pings.
#
# Deliberately not `set -e`: the point is to collect every problem and still
# report, rather than dying on the first one and pinging nothing.
set -uo pipefail

CONFIG="${FILES_MCP_CHECK_ENV:-$HOME/.files-mcp/check.env}"
# shellcheck disable=SC1090
[[ -f "$CONFIG" ]] && source "$CONFIG"

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:18794/health}"
PING_URL="${PING_URL:-}"
KUMA_PUSH_URL="${KUMA_PUSH_URL:-}"
EXPECTED_PYTHON="${EXPECTED_PYTHON:-}"

stamp() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(stamp)] $*"; }

ping_hc() {
  [[ -z "$PING_URL" ]] && return 0
  local suffix="$1" body="${2:-}"
  curl -fsS -m 10 --data-raw "$body" "${PING_URL}${suffix}" >/dev/null 2>&1 || true
}

push_kuma() {
  [[ -z "$KUMA_PUSH_URL" ]] && return 0
  curl -fsS -m 10 -G --data-urlencode "status=$1" --data-urlencode "msg=$2" \
    "$KUMA_PUSH_URL" >/dev/null 2>&1 || true
}

ping_hc "/start"

problems=()

body="$(curl -fsS -m 15 "$HEALTH_URL" 2>/dev/null)"

if [[ -z "$body" ]]; then
  problems+=("no response from $HEALTH_URL (down, or wedged)")
else
  # Parsed with plutil, which ships with macOS. Deliberately not /usr/bin/python3:
  # that is a Command Line Tools shim, and an OS update can leave it prompting to
  # install developer tools -- which would break this check exactly when an OS
  # update is the thing most likely to have broken something.
  extract() {
    printf '%s' "$body" | plutil -extract "$1" raw -o - - 2>/dev/null
  }

  status="$(extract status)"
  python="$(extract python)"
  python_version="$(extract python_version)"

  if [[ "$status" != "ok" ]]; then
    problems+=("health reports status=$status")
  fi

  # A mount that cannot be listed almost always means a privacy grant is
  # missing or was voided: Full Disk Access, or the File Provider permission a
  # cloud-synced folder can need on top of it.
  count="$(extract mounts)"
  for (( i = 0; i < ${count:-0}; i++ )); do
    name="$(extract "mounts.$i.name")"
    readable="$(extract "mounts.$i.readable")"
    if [[ "$readable" != "true" ]]; then
      problems+=("mount $name is not readable (privacy grant missing or voided?)")
    fi
  done

  # Full Disk Access is granted against the interpreter's resolved path, and a
  # uv Python upgrade moves it. The server keeps running until its next
  # restart, then can read nothing. This is the early warning.
  if [[ -n "$EXPECTED_PYTHON" && "$python" != "$EXPECTED_PYTHON" ]]; then
    problems+=("interpreter moved to $python; re-grant Full Disk Access and update EXPECTED_PYTHON")
  fi
fi

if (( ${#problems[@]} > 0 )); then
  message="files-mcp check FAILED"
  for problem in "${problems[@]}"; do
    message+=$'\n'"- $problem"
  done
  log "$message"
  ping_hc "/fail" "$message"
  summary="${problems[0]}"
  (( ${#problems[@]} > 1 )) && summary+=" (+$(( ${#problems[@]} - 1 )) more)"
  push_kuma down "$summary"
  exit 1
fi

# Logged on every run, not just on failure, so the log doubles as a record of
# how the service has actually been behaving.
log "files-mcp OK status=$status python=$python_version"
ping_hc ""
push_kuma up "OK python=$python_version"
