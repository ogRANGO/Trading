#!/bin/bash
# Guard wrapper launchd runs for com.rhcryptobot.paper.
#  - refuses to start unless BOT_MODE=paper (a later .env flip to live never runs here)
#  - refuses to start if the DEAD marker is present (bot lost money past the floor)
# then execs the engine under caffeinate (no idle/system sleep).
set -eu
cd "$(dirname "$0")/.."

PY=".venv/bin/python"
[ -x "$PY" ] || { logger -t rhcryptobot "no venv at $PY"; exit 1; }

cfg() { "$PY" -c "from botcore.config import get_settings as g; print(getattr(g(), '$1'))" 2>/dev/null || echo ""; }
topic="$(cfg ntfy_topic)"
base="$(cfg ntfy_base_url)"; [ -n "$base" ] || base="https://ntfy.sh"
notify() { [ -n "$topic" ] && curl -s -m 5 -H "Title: BOT" -d "$1" "$base/$topic" >/dev/null 2>&1 || true; }

db="$(cfg db_path)"
dead="$(dirname "$db")/DEAD"
if [ -n "$db" ] && [ -f "$dead" ]; then
  logger -t rhcryptobot "refusing to start: DEAD marker present ($dead)"
  notify "launchd: refusing to start, DEAD marker present"
  exit 0     # clean exit -> KeepAlive(SuccessfulExit=false) leaves it stopped
fi

mode="$(cfg bot_mode)"
if [ "$mode" != "paper" ]; then
  logger -t rhcryptobot "refusing to start: BOT_MODE=$mode (launchd agent is paper-only)"
  notify "launchd: refusing to start, BOT_MODE=$mode"
  exit 0
fi

exec caffeinate -is "$PY" -m botcore.serve
