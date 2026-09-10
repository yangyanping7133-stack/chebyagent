#!/bin/sh
set -eu

STATE_ROOT="${CHEBY_APP_SERVER_STATE:-/root/.cheby/appserver}"
PID_FILE="$STATE_ROOT/app-server.pid"
LOG_FILE="$STATE_ROOT/app-server.log"
VERIFIER_FILE="$STATE_ROOT/token.sha256"
SETTINGS_FILE="/root/.cheby/provider-settings.json"
PROVIDER_LAUNCHER="/opt/cheby/appserver/provider-launcher.py"
MCP_PATH="/opt/cheby/connector/cheby_connector/local_mcp.py"
PHONEBRIDGE_STATE="/root/.cheby/phonebridge"

usage() {
  echo "usage: start-codex-appserver --token-sha256 HEX [--foreground]" >&2
  exit 2
}

test "${1:-}" = "--token-sha256" || usage
token_sha256="${2:-}"
foreground=0
case "$#" in
  2) ;;
  3)
    test "${3:-}" = "--foreground" || usage
    foreground=1
    ;;
  *) usage ;;
esac
case "$token_sha256" in
  *[!0-9a-f]*|'') usage ;;
esac
test "${#token_sha256}" -eq 64 || usage

install -d -m 700 "$STATE_ROOT"
test -r "$PROVIDER_LAUNCHER"
test -r "$MCP_PATH"
test -x /usr/bin/python3
test -x /opt/cheby/bin/start-phonebridge
codex app-server --help | grep -F -- '--ws-token-sha256' >/dev/null

ensure_phonebridge_mcp() {
  install -d -m 700 "$PHONEBRIDGE_STATE/artifacts"
  test -r "$PHONEBRIDGE_STATE/local-controller-token"
  if ! codex mcp get phonebridge --json >/dev/null 2>&1; then
    codex mcp add phonebridge \
      --env CHEBY_PHONEBRIDGE_URL=http://127.0.0.1:3437 \
      --env CHEBY_PHONEBRIDGE_TOKEN_FILE="$PHONEBRIDGE_STATE/local-controller-token" \
      --env CHEBY_PHONEBRIDGE_ARTIFACT_DIR="$PHONEBRIDGE_STATE/artifacts" \
      --env CHEBY_LOCAL_MCP_DATA_ROOT=/root/.codex/local-mcp \
      -- /usr/bin/python3 -I "$MCP_PATH" phonebridge \
      >/dev/null
  fi
}

stop_tracked_server() {
  test -r "$PID_FILE" || return 1
  tracked_pid="$(cat "$PID_FILE")"
  case "$tracked_pid" in
    *[!0-9]*|'') return 1 ;;
  esac
  if kill -0 "$tracked_pid" 2>/dev/null; then
    command_line="$(tr '\000' ' ' <"/proc/$tracked_pid/cmdline" 2>/dev/null || true)"
    printf '%s' "$command_line" | grep -Eq \
      '(/opt/cheby/appserver/provider-launcher\.py|codex[^ ]* .*app-server|codex-stdio-bridge\.mjs)' || return 1
    kill "$tracked_pid"
    attempt=0
    while kill -0 "$tracked_pid" 2>/dev/null && test "$attempt" -lt 10; do
      attempt=$((attempt + 1))
      sleep 1
    done
    kill -0 "$tracked_pid" 2>/dev/null && return 1
  fi
  rm -f "$PID_FILE"
}

if curl -fsS --max-time 1 http://127.0.0.1:4500/readyz >/dev/null 2>&1; then
  if test -r "$VERIFIER_FILE" && test "$(cat "$VERIFIER_FILE")" = "$token_sha256" && ! test -e "$SETTINGS_FILE"; then
    echo "codex_appserver_already_running=true"
    exit 0
  fi
  if ! stop_tracked_server; then
    echo "codex_appserver_owned_process_conflict=true" >&2
    exit 1
  fi
  if curl -fsS --max-time 1 http://127.0.0.1:4500/readyz >/dev/null 2>&1; then
    echo "codex_appserver_listener_conflict=true" >&2
    exit 1
  fi
fi
rm -f "$PID_FILE"

printf '%s\n' "$token_sha256" >"$VERIFIER_FILE"
chmod 600 "$VERIFIER_FILE"

# Retire the old Mac-assisted model proxy. A stale loopback endpoint silently
# breaks native phone networking after USB is unplugged; all model traffic now
# follows Android's validated default network directly.
rm -f "$STATE_ROOT/model-proxy-url"

if test "$foreground" -eq 1; then
  : >"$LOG_FILE"
  printf '%s\n' "$$" >"$PID_FILE"
  chmod 600 "$PID_FILE" "$LOG_FILE"
  /opt/cheby/bin/start-phonebridge >/dev/null
  ensure_phonebridge_mcp
  exec /usr/bin/python3 -I "$PROVIDER_LAUNCHER" \
    app-server \
    --listen ws://127.0.0.1:4500 \
    --ws-auth capability-token \
    --ws-token-sha256 "$token_sha256" \
    >"$LOG_FILE" 2>&1
fi

/opt/cheby/bin/start-phonebridge >/dev/null
ensure_phonebridge_mcp
nohup /usr/bin/python3 -I "$PROVIDER_LAUNCHER" \
  app-server \
  --listen ws://127.0.0.1:4500 \
  --ws-auth capability-token \
  --ws-token-sha256 "$token_sha256" \
  >"$LOG_FILE" 2>&1 </dev/null &
app_server_pid=$!
printf '%s\n' "$app_server_pid" >"$PID_FILE"
chmod 600 "$PID_FILE" "$LOG_FILE"

attempt=0
while test "$attempt" -lt 30; do
  if curl -fsS --max-time 1 http://127.0.0.1:4500/readyz >/dev/null 2>&1; then
    echo "codex_appserver_started=true"
    exit 0
  fi
  if ! kill -0 "$app_server_pid" 2>/dev/null; then
    tail -n 40 "$LOG_FILE"
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done

tail -n 40 "$LOG_FILE"
exit 1
