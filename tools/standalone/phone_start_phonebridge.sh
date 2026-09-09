#!/bin/sh
set -eu

RUNTIME_ROOT="${CHEBY_PHONEBRIDGE_ROOT:-/opt/cheby/phonebridge}"
STATE_ROOT="${CHEBY_PHONEBRIDGE_STATE:-/root/.cheby/phonebridge}"
PID_FILE="$STATE_ROOT/phonebridge.pid"
LOG_FILE="$STATE_ROOT/phonebridge.log"
TOKEN_FILE="$STATE_ROOT/local-controller-token"

mkdir -p "$STATE_ROOT"
chmod 700 "$STATE_ROOT"
if ! test -s "$TOKEN_FILE"; then
  umask 077
  python3 -c 'import secrets; print(secrets.token_urlsafe(48))' >"$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"
test -x /usr/bin/node
test -r "$RUNTIME_ROOT/phonebridge.mjs"

if curl -fsS --max-time 1 http://127.0.0.1:3437/health >/dev/null 2>&1; then
  echo "phonebridge_already_running=true"
  exit 0
fi
rm -f "$PID_FILE"

nohup env \
  PHONEBRIDGE_ALLOW_INSECURE=1 \
  PHONEBRIDGE_PUBLIC_HOST=127.0.0.1 \
  PHONEBRIDGE_PUBLIC_PORT=3448 \
  PHONEBRIDGE_LOCAL_HOST=127.0.0.1 \
  PHONEBRIDGE_LOCAL_PORT=3437 \
  PHONEBRIDGE_LOCAL_TOKEN_FILE="$TOKEN_FILE" \
  PHONEBRIDGE_LOCAL_TOKEN_CONTROLLER=chebycodex \
  PHONEBRIDGE_MUTATION_CONTROLLER=chebycodex \
  /usr/bin/node "$RUNTIME_ROOT/phonebridge.mjs" serve \
  >"$LOG_FILE" 2>&1 </dev/null &
phonebridge_pid=$!
printf '%s\n' "$phonebridge_pid" >"$PID_FILE"
chmod 600 "$PID_FILE" "$LOG_FILE"

attempt=0
while test "$attempt" -lt 30; do
  if curl -fsS --max-time 1 http://127.0.0.1:3437/health >/dev/null 2>&1; then
    echo "phonebridge_started=true"
    echo "phonebridge_pid=$phonebridge_pid"
    exit 0
  fi
  if ! kill -0 "$phonebridge_pid" 2>/dev/null; then
    tail -n 40 "$LOG_FILE"
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done

tail -n 40 "$LOG_FILE"
exit 1
