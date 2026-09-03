"""FastAPI dashboard: KPI cards, positions + exit-plan status, order ledger,
quote quality, and PAUSE / RESUME / FLATTEN & HALT controls.

The engine is the only writer of trading state; this app reads the SQLite DB and
writes control flags (``paused``, ``flatten_requested``) + the HALT file.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from botcore.config import get_config, get_settings
from botcore.risk.killswitch import DeadSwitch, KillSwitch
from botcore.store.db import open_db
from botcore.store.state import (
    equity_series,
    get_flag,
    is_paused,
    latest_quotes,
    load_positions,
    recent_events,
    recent_orders,
    recent_trades,
    set_flag,
)
from botcore.strategy.exitplan import ExitPlan, assess

INDEX_HTML = (Path(__file__).parent / "static" / "index.html")


def create_app() -> FastAPI:
    settings = get_settings()
    cfg = get_config()
    app = FastAPI(title="rh-crypto-bot dashboard", docs_url=None, redoc_url=None)
    halt = KillSwitch(str(Path(settings.db_path).with_name("HALT")))
    dead = DeadSwitch(str(Path(settings.db_path).with_name("DEAD")))

    def db():
        conn = open_db(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_token(
        x_dashboard_token: Optional[str] = Header(default=None),
        token: Optional[str] = Query(default=None),
    ) -> None:
        supplied = x_dashboard_token or token
        if settings.dashboard_token and settings.dashboard_token != "change-me":
            if supplied != settings.dashboard_token:
                raise HTTPException(401, "bad or missing dashboard token")

    # -- page -----------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return INDEX_HTML.read_text(encoding="utf-8")

    # -- state --------------------------------------------------------
    @app.get("/api/state")
    def state(conn=Depends(db)) -> JSONResponse:
        mode = settings.bot_mode
        quotes = {q["symbol"]: q for q in latest_quotes(conn)}
        pos_rows = load_positions(conn, mode)

        positions = []
        issues_count = 0
        stop_dists = []
        for sym, row in pos_rows.items():
            plan = ExitPlan.from_dict(_loads(row.get("plan_json"))) if row.get("plan_json") else None
            cur = quotes.get(sym, {}).get("mid") or row["avg_price"]
            a = assess(row["avg_price"], cur, plan)
            if a.status != "OK":
                issues_count += 1
            if a.stop_distance_pct is not None:
                stop_dists.append(a.stop_distance_pct)
            positions.append({
                "ticker": sym,
                "status": "OPEN",
                "shares": row["qty"],
                "entry_price": row["avg_price"],
                "current_price": cur,
                "stop_price": plan.effective_stop if plan else None,
                "target_price": plan.target if plan else None,
                "trailing_stop_price": plan.trail_stop if plan else None,
                "tp1_price": plan.tp1 if plan else None,
                "tp1_done": bool(plan.tp1_done) if plan else False,
                "stop_distance_pct": None if a.stop_distance_pct is None else round(a.stop_distance_pct * 100, 2),
                "exit_plan_status": a.status,
                "issues": "; ".join(a.issues),
                "unrealized_pct": round((cur / row["avg_price"] - 1) * 100, 2) if row["avg_price"] else 0.0,
                "strategy": row.get("strategy"),
                "reason": row.get("entry_reason") or "",
                "updated_at": _iso(row.get("updated_ts")),
            })

        eq = equity_series(conn, mode)
        equity_now = eq[-1]["equity"] if eq else settings.paper_start_equity
        start_eq = eq[0]["equity"] if eq else settings.paper_start_equity

        halted = halt.engaged
        paused = is_paused(conn)
        is_dead = dead.dead
        protective = ("DEAD" if is_dead else "HALTED" if halted else "PAUSED" if paused
                      else "ISSUES" if issues_count else "OK")

        return JSONResponse({
            "mode": mode,
            "broker": settings.broker,
            "universe": cfg.active_universe,
            "signal_family": cfg.strategy.signal_family,
            "config_sha": cfg.config_sha,
            "paused": paused,
            "halted": halted,
            "halt_info": halt.info(),
            "dead": is_dead,
            "dead_certificate": dead.certificate(),
            "engine_mode": cfg.engine.engine_mode,
            "kpis": {
                "active_positions": len(positions),
                "exit_plan_issues": issues_count,
                "avg_stop_distance_pct": round(sum(stop_dists) / len(stop_dists) * 100, 2) if stop_dists else None,
                "protective_state": protective,
                **_agent_kpis(conn, mode, cfg, settings.db_path),
            },
            "equity": {
                "now": round(equity_now, 2),
                "start": round(start_eq, 2),
                "return_pct": round((equity_now / start_eq - 1) * 100, 2) if start_eq else 0.0,
            },
            "positions": positions,
            "server_ts": time.time(),
        })

    @app.get("/api/equity")
    def equity(conn=Depends(db)):
        return [{"ts": r["ts"], "equity": r["equity"]} for r in equity_series(conn, settings.bot_mode)]

    @app.get("/api/orders")
    def orders(limit: int = 60, conn=Depends(db)):
        return recent_orders(conn, limit)

    @app.get("/api/trades")
    def trades(limit: int = 60, conn=Depends(db)):
        return recent_trades(conn, limit)

    @app.get("/api/quotes")
    def quotes(conn=Depends(db)):
        rows = latest_quotes(conn, cfg.universe)
        for r in rows:
            m = r["mid"] or 0
            r["spread_pct"] = round((r["ask"] - r["bid"]) / m * 100, 4) if m else None
            r["age_s"] = round(time.time() - r["ts"], 1)
        return rows

    @app.get("/api/events")
    def events(limit: int = 100, conn=Depends(db)):
        return recent_events(conn, limit)

    @app.get("/api/agents")
    def agents(conn=Depends(db)):
        if cfg.engine.engine_mode != "multi":
            return []
        from botcore.agents.registry import agent_roster
        return agent_roster(conn, settings.bot_mode, cfg, settings.db_path)

    # -- controls ---------------------------------------------------
    @app.post("/api/pause", dependencies=[Depends(require_token)])
    def pause(conn=Depends(db)):
        set_flag(conn, "paused", "1")
        return {"paused": True}

    @app.post("/api/resume", dependencies=[Depends(require_token)])
    def resume(conn=Depends(db)):
        set_flag(conn, "paused", "0")
        return {"paused": False}

    @app.post("/api/flatten", dependencies=[Depends(require_token)])
    def flatten(conn=Depends(db)):
        set_flag(conn, "flatten_requested", "1")
        return {"flatten_requested": True, "note": "engine will flatten & halt on its next tick"}

    @app.post("/api/halt", dependencies=[Depends(require_token)])
    def do_halt():
        halt.engage("dashboard HALT button", source="dashboard")
        return {"halted": True}

    @app.post("/api/clear-halt", dependencies=[Depends(require_token)])
    def clear_halt(conn=Depends(db)):
        halt.clear()
        set_flag(conn, "flatten_requested", "0")
        set_flag(conn, "resume_requested", "1")   # engine clears RiskState.halted + re-baselines DD
        return {"halted": False}

    return app


def _loads(s):
    import json
    return json.loads(s) if s else None


def _iso(ts):
    if not ts:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _agent_kpis(conn, mode, cfg, db_path) -> dict:
    if cfg.engine.engine_mode != "multi":
        return {}
    try:
        from botcore.agents.registry import agent_roster
        roster = agent_roster(conn, mode, cfg, db_path)
    except Exception:  # noqa: BLE001
        return {}
    return {
        "agents_active": sum(1 for a in roster if a["enabled"] and not a["dead"]),
        "agents_dead": sum(1 for a in roster if a["dead"]),
    }


app = create_app()
