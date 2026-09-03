#!/bin/bash
# Start / stop the memecoin paper bot (port 8788, sim broker, crypto_memes).
#
#   scripts/crypto.sh start|stop|restart|status
#
# The stock bot runs under launchd (deploy/run.sh); this one is a plain nohup
# process, so it needs its own wrapper. Everything resolves relative to the repo
# root discovered from $0 — a repo move can no longer strand it on a dead path,
# which is what happened on 2026-09-02 when it was launched with hardcoded
# /Users/og.rango/rh-crypto-bot paths and spent hours in a FileNotFoundError loop.
set -eu
cd "$(dirname "$0")/.."
REPO="$(pwd)"

PY="$REPO/.venv/bin/python"
PIDFILE="$REPO/data/crypto_bot.pid"
LOG="$REPO/logs/crypto_bot.out"

export CONFIG_PATH="$REPO/config_crypto.yaml"
export DB_PATH="$REPO/data/bot_crypto.db"
export BROKER="sim"
export ACTIVE_UNIVERSE="crypto_memes"
export DASHBOARD_PORT="8788"

alive() {  # pid file names a live python engine?
  [ -f "$PIDFILE" ] || return 1
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  ps -p "$pid" -o command= 2>/dev/null | grep -q "botcore.serve"
}

case "${1:-}" in
  start)
    [ -x "$PY" ] || { echo "no venv at $PY"; exit 1; }
    alive && { echo "already running (pid $(cat "$PIDFILE"))"; exit 0; }
    mode="$("$PY" -c 'from botcore.config import get_settings as g; print(g().bot_mode)')"
    [ "$mode" = "paper" ] || { echo "refusing: BOT_MODE=$mode (paper only)"; exit 1; }
    dead="$(dirname "$DB_PATH")/DEAD"
    [ -f "$dead" ] && { echo "refusing: DEAD marker at $dead"; exit 0; }
    mkdir -p "$REPO/logs"
    nohup caffeinate -is "$PY" -m botcore.serve >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    echo "started (wrapper pid $(cat "$PIDFILE")) -> $LOG"
    ;;
  stop)
    alive || { echo "not running"; exit 0; }
    pid="$(cat "$PIDFILE")"
    kill -TERM "$pid" 2>/dev/null || true
    # the engine logs "[engine] stopped" on SIGTERM; caffeinate exits with its child.
    # Wait for the port to actually free, else `restart` races and fails to bind 8788.
    for _ in $(seq 1 20); do
      ps -p "$pid" >/dev/null 2>&1 || break
      sleep 0.5
    done
    if ps -p "$pid" >/dev/null 2>&1; then
      echo "pid $pid ignored SIGTERM after 10s; sending SIGKILL"
      kill -9 "$pid" 2>/dev/null || true
      sleep 1
    fi
    rm -f "$PIDFILE"
    echo "stopped $pid"
    ;;
  restart)
    "$0" stop || true
    exec "$0" start
    ;;
  status)
    if alive; then
      echo "running (pid $(cat "$PIDFILE"))"
    else
      echo "not running"
    fi
    echo "db:     $DB_PATH"
    echo "config: $CONFIG_PATH"
    tok="$(grep -E '^DASHBOARD_TOKEN=' "$REPO/.env" | cut -d= -f2)"
    curl -sS -m 5 -o /dev/null -w "dashboard /api/state http=%{http_code}\n" \
      -H "X-Dashboard-Token: $tok" \
      "http://127.0.0.1:$DASHBOARD_PORT/api/state" 2>/dev/null || echo "dashboard unreachable"
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status}"; exit 2 ;;
esac
