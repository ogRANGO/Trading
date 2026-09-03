#!/usr/bin/env bash
# External belt-and-suspenders watchdog, run every 5 min by com.rhcryptobot.watchdog.
# The in-process watchdog (600s) normally wins; this covers a fully frozen interpreter.
#
# If the engine heartbeat is older than STALE_S AND the bot is not deliberately
# HALTED / PAUSED / DEAD: push an ntfy alert, write the HALT file, kick the paper agent.
set -euo pipefail

cd "$(dirname "$0")/.."
PY=".venv/bin/python"
STALE_S=1800

# Python prints: "<age_s> <halt 0|1> <paused 0|1> <dead 0|1> <ntfy_topic|-> <ntfy_base> <halt_path>"
info="$("$PY" - <<'PYEOF'
import os, time
from botcore.config import get_settings
from botcore.store.db import open_db
s = get_settings()
d = os.path.dirname(s.db_path)
halt_path = os.path.join(d, "HALT")
halt = int(os.path.exists(halt_path))
dead = int(os.path.exists(os.path.join(d, "DEAD")))
age, paused = -1, 0                       # age -1 => no heartbeat row (engine never ran / disabled)
try:
    c = open_db(s.db_path)
    row = c.execute("SELECT value FROM bot_flags WHERE key='engine_heartbeat'").fetchone()
    if row:
        age = int(time.time() - float(row[0]))
    p = c.execute("SELECT value FROM bot_flags WHERE key='paused'").fetchone()
    paused = int(bool(p and p[0] == "1"))
except Exception:
    pass
print(age, halt, paused, dead, s.ntfy_topic or "-", s.ntfy_base_url, halt_path)
PYEOF
)"

read -r age halt_present paused dead_present topic base halt_path <<< "$info"

# age -1 = no heartbeat ever (engine disabled / broker-down / dashboard-only): a
# kickstart won't help, so leave it. Only act on a genuinely stale live heartbeat.
if [ "$dead_present" -eq 1 ] || [ "$halt_present" -eq 1 ] || [ "$paused" -eq 1 ] \
   || [ "$age" -lt 0 ] || [ "$age" -lt "$STALE_S" ]; then
  exit 0
fi

msg="external watchdog: engine heartbeat stale ${age}s -> HALT + restart"
logger -t rhcryptobot "$msg"
if [ "$topic" != "-" ]; then
  curl -s -m 5 -H "Title: BOT WATCHDOG" -H "Priority: urgent" -d "$msg" "$base/$topic" >/dev/null 2>&1 || true
fi
printf '{"reason":"%s","source":"external-watchdog","ts":%s}\n' "$msg" "$(date +%s)" > "$halt_path"
launchctl kickstart -k "gui/$(id -u)/com.rhcryptobot.paper" 2>/dev/null || true
