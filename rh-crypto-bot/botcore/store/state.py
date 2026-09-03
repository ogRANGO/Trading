"""Repository functions over the SQLite store, shared by the engine and dashboard.

The engine is the single writer; the dashboard only reads (plus toggling flags).
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from botcore.brokers.base import Order, Position


# --------------------------------------------------------------------------- #
# control flags
# --------------------------------------------------------------------------- #
def set_flag(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO bot_flags(key, value, updated_ts) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
        (key, value, time.time()),
    )


def get_flag(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM bot_flags WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def is_paused(conn: sqlite3.Connection) -> bool:
    return get_flag(conn, "paused", "0") == "1"


# --------------------------------------------------------------------------- #
# orders
# --------------------------------------------------------------------------- #
def record_order(conn: sqlite3.Connection, o: Order, *, mode: str, role: str = "") -> None:
    conn.execute(
        "INSERT INTO orders_local(id, client_order_id, symbol, side, type, asset_quantity, "
        " limit_price, stop_price, status, filled_quantity, average_price, fee, mode, strategy, "
        " role, created_ts, updated_ts, raw) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
        " filled_quantity=excluded.filled_quantity, average_price=excluded.average_price, "
        " fee=excluded.fee, updated_ts=excluded.updated_ts, raw=excluded.raw",
        (o.id, o.client_order_id, o.symbol, o.side, o.type, o.qty, o.limit_price, o.stop_price,
         o.status, o.filled_qty, o.filled_avg_price, o.fee, mode, o.strategy, role,
         o.submitted_at, time.time(), json.dumps(_order_raw(o))),
    )


def _order_raw(o: Order) -> dict:
    return {"reason": o.reason, "filled_at": o.filled_at, "type": o.type}


def recent_orders(conn: sqlite3.Connection, limit: int = 50) -> List[dict]:
    rows = conn.execute(
        "SELECT * FROM orders_local ORDER BY created_ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# positions
# --------------------------------------------------------------------------- #
def upsert_position(
    conn: sqlite3.Connection,
    mode: str,
    pos: Position,
    *,
    plan: Optional[dict] = None,
    strategy: str = "",
    entry_reason: str = "",
    opened_ts: Optional[float] = None,
) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO positions(symbol, mode, qty, avg_price, stop_price, high_water, "
        " plan_json, strategy, entry_reason, opened_ts, updated_ts) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, mode) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price, "
        " stop_price=excluded.stop_price, high_water=excluded.high_water, "
        " plan_json=excluded.plan_json, updated_ts=excluded.updated_ts",
        (pos.symbol, mode, pos.qty, pos.avg_price,
         (plan or {}).get("effective_stop"), (plan or {}).get("high_water"),
         json.dumps(plan) if plan else None, strategy, entry_reason,
         opened_ts or now, now),
    )


def delete_position(conn: sqlite3.Connection, mode: str, symbol: str) -> None:
    conn.execute("DELETE FROM positions WHERE mode=? AND symbol=?", (mode, symbol))


def load_positions(conn: sqlite3.Connection, mode: str) -> Dict[str, dict]:
    rows = conn.execute("SELECT * FROM positions WHERE mode=?", (mode,)).fetchall()
    return {r["symbol"]: dict(r) for r in rows}


# --------------------------------------------------------------------------- #
# trades / equity / events
# --------------------------------------------------------------------------- #
def record_trade(conn: sqlite3.Connection, t: dict, mode: str) -> None:
    conn.execute(
        "INSERT INTO trades(symbol, qty, entry_price, exit_price, fees, pnl, mode, strategy, "
        " entry_order_id, exit_order_id, opened_ts, closed_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (t["symbol"], t["qty"], t["entry_price"], t.get("exit_price"), t.get("fees", 0.0),
         t.get("pnl"), mode, t.get("strategy", ""), t.get("entry_order_id"),
         t.get("exit_order_id"), t.get("opened_ts") or time.time(), t.get("closed_ts") or time.time()),
    )


def recent_trades(conn: sqlite3.Connection, limit: int = 50) -> List[dict]:
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY COALESCE(closed_ts, opened_ts) DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def snapshot_equity(
    conn: sqlite3.Connection, mode: str, cash: float, positions_value: float,
    peak: Optional[float] = None, ts: Optional[float] = None,
) -> None:
    ts = ts or time.time()
    conn.execute(
        "INSERT OR REPLACE INTO equity_snapshots(ts, mode, cash_usd, positions_value, equity, peak_equity) "
        "VALUES(?,?,?,?,?,?)",
        (ts, mode, cash, positions_value, cash + positions_value, peak),
    )


def equity_series(conn: sqlite3.Connection, mode: str, limit: int = 5000) -> List[dict]:
    rows = conn.execute(
        "SELECT ts, equity, cash_usd, positions_value FROM equity_snapshots "
        "WHERE mode=? ORDER BY ts DESC LIMIT ?", (mode, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def recent_events(conn: sqlite3.Connection, limit: int = 100) -> List[dict]:
    rows = conn.execute(
        "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def upsert_quote(conn: sqlite3.Connection, symbol: str, bid: float, ask: float, ts: Optional[float] = None) -> None:
    ts = ts or time.time()
    conn.execute(
        "INSERT OR REPLACE INTO quotes(symbol, ts, bid, ask, mid) VALUES(?,?,?,?,?)",
        (symbol, ts, bid, ask, (bid + ask) / 2 if bid and ask else (ask or bid)),
    )


def latest_quotes(conn: sqlite3.Connection, symbols: Optional[List[str]] = None) -> List[dict]:
    rows = conn.execute(
        "SELECT q.* FROM quotes q JOIN (SELECT symbol, MAX(ts) mts FROM quotes GROUP BY symbol) m "
        "ON q.symbol=m.symbol AND q.ts=m.mts ORDER BY q.symbol"
    ).fetchall()
    out = [dict(r) for r in rows]
    if symbols:
        keep = set(symbols)
        out = [r for r in out if r["symbol"] in keep]
    return out


# --------------------------------------------------------------------------- #
# retention / maintenance (Phase 5 housekeeping job)
# --------------------------------------------------------------------------- #
def prune_events(conn: sqlite3.Connection, older_than_ts: float) -> int:
    return max(conn.execute("DELETE FROM events WHERE ts < ?", (older_than_ts,)).rowcount, 0)


def prune_quotes(conn: sqlite3.Connection, older_than_ts: float) -> int:
    return max(conn.execute("DELETE FROM quotes WHERE ts < ?", (older_than_ts,)).rowcount, 0)


def downsample_equity_snapshots(conn: sqlite3.Connection, mode: str, older_than_ts: float) -> int:
    """Keep one snapshot per UTC day for rows older than the cutoff; delete the rest."""
    rows = conn.execute(
        "SELECT ts FROM equity_snapshots WHERE mode=? AND ts < ? ORDER BY ts",
        (mode, older_than_ts),
    ).fetchall()
    seen: set = set()
    drop: List[float] = []
    for (ts,) in rows:
        day = int(ts // 86400)
        (drop.append(ts) if day in seen else seen.add(day))
    for ts in drop:
        conn.execute("DELETE FROM equity_snapshots WHERE mode=? AND ts=?", (mode, ts))
    return len(drop)


def checkpoint_wal(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass


# --------------------------------------------------------------------------- #
# SimBroker persistence (Phase 5 — sim paper book survives restarts)
# --------------------------------------------------------------------------- #
@dataclass
class SimSnapshot:
    cash: float
    realized_pnl: float
    total_fees: float
    positions: Dict[str, Position]


def load_sim_state(conn: sqlite3.Connection) -> Optional[SimSnapshot]:
    row = conn.execute(
        "SELECT cash, realized_pnl, total_fees FROM sim_broker_state WHERE id=1"
    ).fetchone()
    if row is None:
        return None
    positions: Dict[str, Position] = {}
    for r in conn.execute("SELECT symbol, qty, avg_price, market_price FROM sim_positions"):
        positions[r["symbol"]] = Position(
            symbol=r["symbol"], qty=r["qty"], avg_price=r["avg_price"],
            market_price=r["market_price"] or r["avg_price"],
        )
    return SimSnapshot(
        cash=float(row["cash"]), realized_pnl=float(row["realized_pnl"]),
        total_fees=float(row["total_fees"]), positions=positions,
    )


def save_sim_account(conn: sqlite3.Connection, cash: float, realized_pnl: float, total_fees: float) -> None:
    conn.execute(
        "INSERT INTO sim_broker_state(id, cash, realized_pnl, total_fees, updated_ts) "
        "VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
        "cash=excluded.cash, realized_pnl=excluded.realized_pnl, "
        "total_fees=excluded.total_fees, updated_ts=excluded.updated_ts",
        (cash, realized_pnl, total_fees, time.time()),
    )


def save_sim_position(conn: sqlite3.Connection, pos: Position) -> None:
    conn.execute(
        "INSERT INTO sim_positions(symbol, qty, avg_price, market_price, updated_ts) "
        "VALUES(?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
        "qty=excluded.qty, avg_price=excluded.avg_price, "
        "market_price=excluded.market_price, updated_ts=excluded.updated_ts",
        (pos.symbol, pos.qty, pos.avg_price, pos.market_price, time.time()),
    )


def delete_sim_position(conn: sqlite3.Connection, symbol: str) -> None:
    conn.execute("DELETE FROM sim_positions WHERE symbol=?", (symbol,))


class SimBrokerStore:
    """Adapter passed to :class:`SimBroker` so it can persist without importing the store."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def load(self) -> Optional[SimSnapshot]:
        return load_sim_state(self.conn)

    def save_account(self, cash: float, realized_pnl: float, total_fees: float) -> None:
        save_sim_account(self.conn, cash, realized_pnl, total_fees)

    def save_position(self, pos: Position) -> None:
        save_sim_position(self.conn, pos)

    def delete_position(self, symbol: str) -> None:
        delete_sim_position(self.conn, symbol)


# --------------------------------------------------------------------------- #
# Multi-agent trading floor (Phase 7)
# --------------------------------------------------------------------------- #
def record_agent_signal(conn: sqlite3.Connection, agent_id: str, symbol: str, direction: int,
                        conviction: float, reason: str, mode: str,
                        ts: Optional[float] = None) -> None:
    conn.execute(
        "INSERT INTO agent_signals(ts, agent_id, symbol, direction, conviction, reason, mode) "
        "VALUES(?,?,?,?,?,?,?)",
        (ts or time.time(), agent_id, symbol, int(direction), float(conviction), reason, mode),
    )


def record_agent_trade(conn: sqlite3.Connection, agent_id: str, symbol: str, qty: float,
                       entry_price: float, exit_price: float, pnl: float, *, kind: str, mode: str,
                       fees: float = 0.0, opened_ts: Optional[float] = None,
                       closed_ts: Optional[float] = None) -> None:
    conn.execute(
        "INSERT INTO agent_trades(agent_id, symbol, qty, entry_price, exit_price, pnl, fees, "
        " kind, mode, opened_ts, closed_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, symbol, qty, entry_price, exit_price, pnl, fees, kind, mode,
         opened_ts or time.time(), closed_ts or time.time()),
    )


def snapshot_agent_equity(conn: sqlite3.Connection, agent_id: str, mode: str, equity: float,
                          ts: Optional[float] = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO agent_equity(ts, agent_id, mode, equity) VALUES(?,?,?,?)",
        (ts or time.time(), agent_id, mode, equity),
    )


def agent_equity_series(conn: sqlite3.Connection, agent_id: str, mode: str,
                        limit: int = 2000) -> List[dict]:
    rows = conn.execute(
        "SELECT ts, equity FROM agent_equity WHERE agent_id=? AND mode=? ORDER BY ts DESC LIMIT ?",
        (agent_id, mode, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def agent_pnl_summary(conn: sqlite3.Connection, mode: str) -> Dict[str, dict]:
    """Per-agent {shadow_pnl, shadow_trades, wins, attributed_pnl}."""
    out: Dict[str, dict] = {}
    for r in conn.execute(
        "SELECT agent_id, kind, COUNT(*) n, COALESCE(SUM(pnl),0) pnl, "
        " SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) wins "
        "FROM agent_trades WHERE mode=? GROUP BY agent_id, kind", (mode,),
    ):
        d = out.setdefault(r["agent_id"], {"shadow_pnl": 0.0, "shadow_trades": 0,
                                           "wins": 0, "attributed_pnl": 0.0})
        if r["kind"] == "shadow":
            d["shadow_pnl"] = float(r["pnl"])
            d["shadow_trades"] = int(r["n"])
            d["wins"] = int(r["wins"] or 0)
        elif r["kind"] == "attributed":
            d["attributed_pnl"] = float(r["pnl"])
    return out


def last_agent_signal(conn: sqlite3.Connection, agent_id: str, mode: str) -> Optional[dict]:
    r = conn.execute(
        "SELECT ts, symbol, direction, conviction, reason FROM agent_signals "
        "WHERE agent_id=? AND mode=? ORDER BY ts DESC LIMIT 1", (agent_id, mode),
    ).fetchone()
    return dict(r) if r else None
