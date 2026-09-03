#!/usr/bin/env bash
# Install / manage the unattended paper-trading launchd agents (macOS).
#
#   scripts/launchd.sh install     # render plists -> ~/Library/LaunchAgents, load, start
#   scripts/launchd.sh uninstall   # unload + remove
#   scripts/launchd.sh status      # launchd state + DB view
#   scripts/launchd.sh restart     # kickstart the paper agent
#   scripts/launchd.sh logs        # tail the rotating app log
#   scripts/launchd.sh revive --confirm   # re-enable a bot that was KILLED for losing money
#
# Paper only. Reboot-safe (RunAtLoad) and crash-safe (KeepAlive). $0 to run.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
PY="$REPO/.venv/bin/python"
UID_="$(id -u)"
LA="$HOME/Library/LaunchAgents"
PAPER="com.rhcryptobot.paper"
WATCH="com.rhcryptobot.watchdog"
PUBLISH="com.rhcryptobot.publish"

render() {  # <template> <dest>
  sed -e "s|__REPO__|$REPO|g" -e "s|__UID__|$UID_|g" "$1" > "$2"
}

bootstrap() {  # <label>
  launchctl bootout "gui/$UID_/$1" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_" "$LA/$1.plist" 2>/dev/null \
    || launchctl load -w "$LA/$1.plist"
}

case "${1:-}" in
  install)
    [ -x "$PY" ] || { echo "no venv at $PY"; exit 1; }
    mode="$("$PY" -c 'from botcore.config import get_settings as g; print(g().bot_mode)')"
    [ "$mode" = "paper" ] || { echo "refusing: BOT_MODE=$mode (paper only)"; exit 1; }
    mkdir -p "$LA" "$REPO/logs"
    chmod +x "$REPO/deploy/run.sh" "$REPO/scripts/watchdog.sh" "$REPO/scripts/publish_status.sh"
    render "$REPO/deploy/$PAPER.plist.template"    "$LA/$PAPER.plist"
    render "$REPO/deploy/$WATCH.plist.template"    "$LA/$WATCH.plist"
    render "$REPO/deploy/$PUBLISH.plist.template"  "$LA/$PUBLISH.plist"
    bootstrap "$PAPER"
    bootstrap "$WATCH"
    bootstrap "$PUBLISH"
    launchctl kickstart -k "gui/$UID_/$PAPER" 2>/dev/null || true
    echo "installed. dashboard:"
    "$PY" -c 'from botcore.config import get_settings as g; s=g(); t="" if s.dashboard_token in ("","change-me") else f"?token={s.dashboard_token}"; print(f"  http://{s.dashboard_host}:{s.dashboard_port}/{t}")'
    ;;
  uninstall)
    launchctl bootout "gui/$UID_/$PAPER" 2>/dev/null || true
    launchctl bootout "gui/$UID_/$WATCH" 2>/dev/null || true
    launchctl bootout "gui/$UID_/$PUBLISH" 2>/dev/null || true
    rm -f "$LA/$PAPER.plist" "$LA/$WATCH.plist" "$LA/$PUBLISH.plist"
    echo "uninstalled (data/ and logs/ kept)"
    ;;
  restart)
    launchctl kickstart -k "gui/$UID_/$PAPER"
    echo "kicked $PAPER"
    ;;
  status)
    for l in "$PAPER" "$WATCH" "$PUBLISH"; do
      echo "== $l =="
      launchctl print "gui/$UID_/$l" 2>/dev/null | grep -E "state =|pid =|last exit code" || echo "  (not loaded)"
    done
    echo
    exec "$REPO/scripts/paper.sh" status
    ;;
  logs)
    exec tail -f "$REPO/logs/paper.log"
    ;;
  revive)
    if [ "${2:-}" != "--confirm" ]; then
      echo "This re-enables a bot that was KILLED for losing money past the deposit floor."
      echo "Review logs/paper.log and data/DEAD first, then:  $0 revive --confirm"
      [ -f "$REPO/data/DEAD" ] && { echo; cat "$REPO/data/DEAD"; }
      exit 1
    fi
    "$PY" - <<'PY'
from pathlib import Path
from botcore.config import get_settings
from botcore.store.db import open_db
from botcore.store.state import set_flag
s = get_settings(); base = Path(s.db_path).parent
for f in ("DEAD", "HALT"):
    try:
        (base / f).unlink()
    except FileNotFoundError:
        pass
c = open_db(s.db_path)
set_flag(c, "killed", "0")
set_flag(c, "resume_requested", "1")                        # engine clears RiskState + re-baselines DD peak
c.execute("DELETE FROM bot_flags WHERE key IN ('initial_equity','kill_floor_since')")  # re-anchor on next boot
print("revived: DEAD/HALT cleared, killed=0, initial_equity will re-anchor on next engine boot")
PY
    bootstrap "$PAPER"
    bootstrap "$WATCH"
    launchctl kickstart -k "gui/$UID_/$PAPER" 2>/dev/null || true
    echo "revived + agents re-bootstrapped"
    ;;
  revive-agent)
    id="${2:-}"
    [ -n "$id" ] || { echo "usage: $0 revive-agent <id> --confirm"; exit 2; }
    if [ "${3:-}" != "--confirm" ]; then
      echo "Re-enables trading agent '$id' that was disabled for losing money."
      [ -f "$REPO/data/agents/$id.DEAD" ] && { echo; cat "$REPO/data/agents/$id.DEAD"; }
      echo; echo "Then:  $0 revive-agent $id --confirm"
      exit 1
    fi
    AGENT_ID="$id" "$PY" - <<'PY'
import os
from pathlib import Path
from botcore.config import get_settings
from botcore.store.db import open_db
aid = os.environ["AGENT_ID"]
s = get_settings(); base = Path(s.db_path).parent
try:
    (base / "agents" / f"{aid}.DEAD").unlink()
except FileNotFoundError:
    pass
c = open_db(s.db_path)
c.execute("DELETE FROM bot_flags WHERE key=?", (f"agent_kill_since:{aid}",))
c.execute("DELETE FROM agent_equity WHERE agent_id=?", (aid,))   # re-anchor the shadow book
c.execute("DELETE FROM agent_trades WHERE agent_id=?", (aid,))
print(f"revived agent {aid}: DEAD cleared, shadow book reset")
PY
    launchctl kickstart -k "gui/$UID_/$PAPER" 2>/dev/null || true
    echo "restart the engine to re-instantiate the agent"
    ;;
  *)
    echo "usage: $0 {install|uninstall|restart|status|logs|revive|revive-agent}"; exit 2 ;;
esac
