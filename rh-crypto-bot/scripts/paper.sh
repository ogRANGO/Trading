#!/usr/bin/env bash
# Supervise the paper-trading engine + dashboard as a background process on macOS.
#
#   scripts/paper.sh start     # launch (caffeinate keeps the Mac awake)
#   scripts/paper.sh stop      # graceful shutdown (SIGTERM -> engine.stop())
#   scripts/paper.sh status    # running? last tick? equity?
#   scripts/paper.sh logs      # tail -f the log
#   scripts/paper.sh restart
#
# Paper only. Does not survive a reboot — for that, use a launchd agent (Phase 5).
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
LOG_DIR="$ROOT/logs"
LOG="$LOG_DIR/paper.log"
PIDFILE="$ROOT/data/paper.pid"
mkdir -p "$LOG_DIR" "$ROOT/data"

alive() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

start() {
  if alive; then echo "already running (pid $(cat "$PIDFILE"))"; exit 0; fi
  [[ -x "$PY" ]] || { echo "no venv at $PY — create it first"; exit 1; }

  mode=$("$PY" -c 'from botcore.config import get_settings as g; print(g().bot_mode)')
  if [[ "$mode" != "paper" ]]; then
    echo "refusing to start: BOT_MODE=$mode (this supervisor is paper-only)"; exit 1
  fi

  echo "starting paper engine + dashboard ..."
  # caffeinate -is: no idle sleep, no system sleep, while this process lives.
  nohup caffeinate -is "$PY" -m botcore.serve >>"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  sleep 3
  if alive; then
    echo "up (pid $(cat "$PIDFILE"))  log: $LOG"
    "$PY" -c 'from botcore.config import get_settings as g; s=g(); t="" if s.dashboard_token in ("","change-me") else f"?token={s.dashboard_token}"; print(f"dashboard: http://{s.dashboard_host}:{s.dashboard_port}/{t}")'
  else
    echo "failed to start — last log lines:"; tail -n 20 "$LOG"; exit 1
  fi
}

stop() {
  if ! alive; then echo "not running"; rm -f "$PIDFILE"; exit 0; fi
  pid="$(cat "$PIDFILE")"
  echo "stopping (pid $pid) ..."
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do alive || break; sleep 0.5; done
  if alive; then echo "still up, sending SIGKILL"; kill -KILL "$pid" 2>/dev/null || true; fi
  rm -f "$PIDFILE"
  echo "stopped"
}

status() {
  if alive; then
    echo "RUNNING (pid $(cat "$PIDFILE"))"
  elif launchctl print "gui/$(id -u)/com.rhcryptobot.paper" 2>/dev/null | grep -q "state = running"; then
    echo "RUNNING (launchd)"
  else
    echo "STOPPED"
  fi
  "$PY" - <<'PY'
from botcore.config import get_settings
from botcore.store.db import open_db
import time
s = get_settings()
try:
    c = open_db(s.db_path)
    row = c.execute("SELECT ts, equity FROM equity_snapshots WHERE mode=? ORDER BY ts DESC LIMIT 1", (s.bot_mode,)).fetchone()
    if row:
        age = time.time() - row[0]
        print(f"last tick   : {age:,.0f}s ago   equity ${row[1]:,.2f}")
    n = c.execute("SELECT COUNT(*) FROM trades WHERE mode=?", (s.bot_mode,)).fetchone()[0]
    o = c.execute("SELECT COUNT(*) FROM positions WHERE mode=?", (s.bot_mode,)).fetchone()[0]
    print(f"open positions: {o}   closed trades: {n}")
    for lvl, kind, msg, ts in c.execute("SELECT level, kind, message, ts FROM events ORDER BY ts DESC LIMIT 5"):
        print(f"  {time.strftime('%m-%d %H:%M', time.localtime(ts))}  {lvl:5s} {kind:10s} {msg[:80]}")
except Exception as e:  # noqa: BLE001
    print(f"(no db yet: {e})")
PY
}

case "${1:-}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 1; start ;;
  status)  status ;;
  logs)    tail -f "$LOG" ;;
  *) echo "usage: $0 {start|stop|restart|status|logs}"; exit 2 ;;
esac
