#!/usr/bin/env bash
# Snapshot both bots -> status.json -> commit + push to the Trading repo (main).
# Vercel's "Ignored Build Step" skips the build for status.json-only commits, so
# these frequent pushes don't redeploy the site. The page reads status.json via
# jsDelivr. Run on a timer: deploy/com.rhcryptobot.publish.plist
#
# Monorepo layout: the bot lives in trading/rh-crypto-bot/ and the monitor page +
# status.json sit at the repo root (trading/). SITE defaults to the parent of the
# bot repo; override with MONITOR_SITE_DIR.
set -uo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
PY="$REPO/.venv/bin/python"
SITE="${MONITOR_SITE_DIR:-$(dirname "$REPO")}"
TOKEN="$(grep -E '^DASHBOARD_TOKEN=' "$REPO/.env" | cut -d= -f2)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

[ -d "$SITE/.git" ] || { echo "no site repo at $SITE — see monitor setup in README.md"; exit 1; }

fetch() {  # <port> <endpoint> <outfile>
  curl -sS -m 8 -H "X-Dashboard-Token: $TOKEN" "http://127.0.0.1:$1/api/$2" -o "$3" 2>/dev/null || true
  [ -s "$3" ] || echo 'null' > "$3"
}
fetch 8787 state           "$TMP/s_state.json"
fetch 8787 trades          "$TMP/s_tr.json"
fetch 8787 "events?limit=40" "$TMP/s_ev.json"
fetch 8788 state           "$TMP/m_state.json"
fetch 8788 trades          "$TMP/m_tr.json"
fetch 8788 "events?limit=40" "$TMP/m_ev.json"

TMP="$TMP" REPO="$REPO" CYCLE_FILE="$SITE/.cycle" "$PY" - "$SITE/status.json" <<'PY'
import json, math, os, sqlite3, sys, time
T = os.environ["TMP"]
REPO = os.environ["REPO"]

def load(n):
    try:
        with open(os.path.join(T, n)) as f:
            return json.load(f)
    except Exception:
        return None

def equity_series(db_path, points=60, window_rows=360):
    """Last `points` equity snapshots (down-sampled from up to `window_rows`)."""
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        rows = c.execute(
            "SELECT ts, equity FROM equity_snapshots WHERE mode='paper' "
            "ORDER BY ts DESC LIMIT ?", (window_rows,)).fetchall()
        c.close()
    except Exception:
        return []
    rows = rows[::-1]
    if len(rows) > points:
        step = math.ceil(len(rows) / points)
        rows = rows[::step] + ([rows[-1]] if (len(rows) - 1) % step else [])
    return [{"ts": round(t, 1), "equity": round(e, 2)} for t, e in rows]

def trade_stats(trades):
    pnls = [t.get("pnl") for t in trades if isinstance(t.get("pnl"), (int, float))]
    if not pnls:
        return {"trades_total": 0, "win_rate": None, "avg_pnl": None,
                "best_trade": None, "worst_trade": None}
    wins = sum(1 for p in pnls if p > 0)
    return {
        "trades_total": len(pnls),
        "win_rate": round(wins / len(pnls), 3),
        "avg_pnl": round(sum(pnls) / len(pnls), 2),
        "best_trade": round(max(pnls), 2),
        "worst_trade": round(min(pnls), 2),
    }

def bot(bid, name, port, db_rel, s, tr, ev):
    if not isinstance(s, dict):
        return {"id": bid, "name": name, "dashboard_port": port, "down": True,
                "equity": {}, "positions": [], "closed_trades": [], "events": [],
                "equity_series": [], "trades_total": 0, "win_rate": None,
                "avg_pnl": None, "best_trade": None, "worst_trade": None}
    tr = tr if isinstance(tr, list) else []
    ev = ev if isinstance(ev, list) else []
    k = s.get("kpis", {})
    closed = [
        {"symbol": t.get("symbol"), "pnl": t.get("pnl"),
         "entry_price": t.get("entry_price"), "exit_price": t.get("exit_price"),
         "closed_ts": t.get("closed_ts")} for t in tr]
    return {
        "id": bid, "name": name, "dashboard_port": port,
        "universe": s.get("universe"), "family": s.get("signal_family"),
        "broker": s.get("broker"), "engine_mode": s.get("engine_mode"),
        "config_sha": s.get("config_sha"),
        "equity": s.get("equity", {}), "protective_state": k.get("protective_state"),
        "paused": s.get("paused"), "halted": s.get("halted"), "dead": s.get("dead"),
        "positions": [
            {"ticker": p.get("ticker"), "shares": p.get("shares"),
             "entry_price": p.get("entry_price"), "current_price": p.get("current_price"),
             "unrealized_pct": p.get("unrealized_pct"), "stop_price": p.get("stop_price")}
            for p in s.get("positions", [])],
        "closed_trades": closed[-8:][::-1],
        "events": [
            {"ts": e.get("ts"), "level": e.get("level"), "kind": e.get("kind"),
             "message": e.get("message")} for e in ev[:12]],
        "equity_series": equity_series(os.path.join(REPO, db_rel)),
        **trade_stats(closed),
    }

bots = [
    bot("stock", "Stocks", 8787, "data/bot.db",
        load("s_state.json"), load("s_tr.json"), load("s_ev.json")),
    bot("meme", "Memecoins", 8788, "data/bot_crypto.db",
        load("m_state.json"), load("m_tr.json"), load("m_ev.json")),
]

# merged activity log
activity = []
for src, key in ((load("s_ev.json"), "stock"), (load("m_ev.json"), "meme")):
    for e in (src if isinstance(src, list) else []):
        activity.append({"bot": key, "ts": e.get("ts"), "level": e.get("level"),
                         "kind": e.get("kind"), "message": e.get("message")})
activity.sort(key=lambda e: e.get("ts") or 0, reverse=True)
activity = activity[:40]

# monotonic cycle counter (gitignored file in the site repo)
try:
    cycle = int(open(os.environ["CYCLE_FILE"]).read().strip()) + 1
except Exception:
    cycle = 1
try:
    open(os.environ["CYCLE_FILE"], "w").write(str(cycle))
except Exception:
    pass

out = {"generated_at": int(time.time()), "cycle": cycle, "bots": bots, "activity": activity}
with open(sys.argv[1], "w") as f:
    json.dump(out, f, separators=(",", ":"))
print("cycle", cycle, "| bots:",
      [b["name"] + (" DOWN" if b.get("down") else " ok") for b in bots],
      "| activity", len(activity))
PY

cd "$SITE"
git pull -q --rebase --autostash 2>/dev/null || true
git add status.json
git -c user.email="bot@localhost" -c user.name="status-bot" \
    commit -q -m "status $(date -u +%FT%TZ)" 2>/dev/null || { echo "no change"; exit 0; }
git push -q 2>&1 || { echo "push failed — check git credentials for $SITE"; exit 1; }

SLUG="$(git remote get-url origin | sed -E 's#.*github.com[:/]##; s#\.git$##')"
echo "pushed ($SLUG)"
