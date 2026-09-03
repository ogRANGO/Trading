"""Per-agent shadow P&L + the permanent per-agent kill.

Each agent runs a throwaway solo book (a :class:`SimBroker` seeded with
``stake_usd``) driven only by that agent's own signals. When an agent's shadow
equity falls past its floor — after it has had a fair number of shadow trades —
it is permanently disabled: ``data/agents/<id>.DEAD`` is written and the
coordinator drops it. The main bot keeps trading with the survivors.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from botcore.agents.base import AgentContext, AgentSignal
from botcore.brokers.sim import SimBroker
from botcore.config import AgentKillCfg, FeesCfg
from botcore.risk.killswitch import DeadSwitch
from botcore.store.state import (
    get_flag,
    record_agent_signal,
    record_agent_trade,
    set_flag,
    snapshot_agent_equity,
)

log = logging.getLogger(__name__)

_RISK_FRAC = 0.02        # shadow book risks this much of the stake per trade
_STOP_ATR = 3.0          # to a 3x ATR hard stop
_NOTIONAL_CAP = 0.5      # never more than half the stake in one name


class AgentLedger:
    def __init__(self, conn, cfg: AgentKillCfg, *, mode: str, fees: Optional[FeesCfg] = None,
                 dead_dir: Optional[Path] = None) -> None:
        self.conn = conn
        self.cfg = cfg
        self.mode = mode
        self.fees = fees or FeesCfg()
        self.dead_dir = Path(dead_dir) if dead_dir else Path(_default_dead_dir(conn))
        self.dead_dir.mkdir(parents=True, exist_ok=True)
        self._brokers: Dict[str, SimBroker] = {}
        self._meta: Dict[str, Dict[str, dict]] = {}   # agent_id -> {symbol -> {entry, atr}}
        self._trades: Dict[str, int] = {}             # agent_id -> shadow trade count
        self._strikes: Dict[str, int] = {}
        self._dead: Dict[str, DeadSwitch] = {}

    # -- dead switch ---------------------------------------------------- #
    def _switch(self, agent_id: str) -> DeadSwitch:
        s = self._dead.get(agent_id)
        if s is None:
            s = DeadSwitch(str(self.dead_dir / f"{agent_id}.DEAD"))
            self._dead[agent_id] = s
        return s

    def is_dead(self, agent_id: str) -> bool:
        return self._switch(agent_id).dead

    # -- signals ------------------------------------------------------- #
    def record_signal(self, agent_id: str, sig: AgentSignal, ctx: AgentContext) -> None:
        try:
            record_agent_signal(self.conn, agent_id, sig.symbol, sig.direction,
                                sig.conviction, sig.reason, self.mode, ts=ctx.now)
        except Exception:  # noqa: BLE001
            log.debug("record_agent_signal failed", exc_info=True)

    # -- shadow book -------------------------------------------------- #
    def _broker(self, agent_id: str) -> SimBroker:
        b = self._brokers.get(agent_id)
        if b is None:
            b = SimBroker(starting_cash=self.cfg.stake_usd, fees=self.fees)
            self._brokers[agent_id] = b
            self._meta[agent_id] = {}
        return b

    def tick(self, ctx: AgentContext, per_agent: Dict[str, List[AgentSignal]]) -> None:
        for agent_id, sigs in per_agent.items():
            if self.is_dead(agent_id):
                continue
            b = self._broker(agent_id)
            meta = self._meta[agent_id]
            # mark to current mids
            marks = {s: {"open": q.mid, "high": q.mid, "low": q.mid, "close": q.mid}
                     for s, q in ctx.quotes.items() if q.mid > 0}
            if marks:
                b.mark(marks, clock=ctx.now)

            wants = {s.symbol: s for s in sigs}
            for sym, q in ctx.quotes.items():
                if q.mid <= 0:
                    continue
                pos = b.get_position(sym)
                sig = wants.get(sym)
                held = pos is not None and pos.qty > 1e-12

                # exits: opposite signal, veto, or ATR stop
                if held:
                    m = meta.get(sym, {})
                    stop = m.get("entry", 0.0) - _STOP_ATR * m.get("atr", 0.0)
                    hit_stop = m.get("atr", 0.0) > 0 and q.mid <= stop
                    if (sig and sig.direction <= 0) or hit_stop:
                        self._close(agent_id, b, sym, q.mid, ctx.now,
                                    "stop" if hit_stop else "signal")
                        meta.pop(sym, None)
                        continue

                # entries
                if not held and sig and sig.direction > 0:
                    df = ctx.bars.get(sym)
                    atr = _last_atr(df) if df is not None else 0.0
                    if atr <= 0:
                        continue
                    qty = self._size(q.ask or q.mid, atr)
                    if qty <= 0:
                        continue
                    b.fill_market(sym, "buy", qty, ref_price=q.ask or q.mid)
                    fp = b.get_position(sym)
                    meta[sym] = {"entry": fp.avg_price if fp else q.mid, "atr": atr,
                                 "opened_ts": ctx.now}

            self._snapshot(agent_id, b, ctx.now)

    def _size(self, price: float, atr: float) -> float:
        if price <= 0 or atr <= 0:
            return 0.0
        qty = (self.cfg.stake_usd * _RISK_FRAC) / (_STOP_ATR * atr)
        qty = min(qty, self.cfg.stake_usd * _NOTIONAL_CAP / price)
        return max(qty, 0.0)

    def _close(self, agent_id: str, b: SimBroker, sym: str, mid: float, now: float, why: str) -> None:
        pos = b.get_position(sym)
        if pos is None or pos.qty <= 1e-12:
            return
        entry_px = self._meta[agent_id].get(sym, {}).get("entry", pos.avg_price)
        qty = pos.qty
        before = b.realized_pnl
        b.fill_market(sym, "sell", qty, ref_price=mid)
        pnl = b.realized_pnl - before
        opened = self._meta[agent_id].get(sym, {}).get("opened_ts", now)
        try:
            record_agent_trade(self.conn, agent_id, sym, qty, entry_px, mid, pnl,
                               kind="shadow", mode=self.mode, opened_ts=opened, closed_ts=now)
        except Exception:  # noqa: BLE001
            log.debug("record_agent_trade failed", exc_info=True)
        self._trades[agent_id] = self._trades.get(agent_id, 0) + 1

    def _snapshot(self, agent_id: str, b: SimBroker, now: float) -> None:
        try:
            snapshot_agent_equity(self.conn, agent_id, self.mode,
                                  float(b.get_account().equity), ts=now)
        except Exception:  # noqa: BLE001
            log.debug("snapshot_agent_equity failed", exc_info=True)

    # -- attribution (real trades) ----------------------------------- #
    def attribute(self, contributors: Dict[str, float], pnl: float, symbol: str, now: float) -> None:
        total = sum(v for v in contributors.values() if v > 0)
        if total <= 0:
            return
        for aid, w in contributors.items():
            if w <= 0:
                continue
            share = pnl * (w / total)
            try:
                record_agent_trade(self.conn, aid, symbol, 0.0, 0.0, 0.0, share,
                                   kind="attributed", mode=self.mode, opened_ts=now, closed_ts=now)
            except Exception:  # noqa: BLE001
                log.debug("attribute record failed", exc_info=True)

    # -- the kill --------------------------------------------------- #
    def shadow_equity(self, agent_id: str) -> float:
        b = self._brokers.get(agent_id)
        return float(b.get_account().equity) if b else self.cfg.stake_usd

    def trade_count(self, agent_id: str) -> int:
        n = self._trades.get(agent_id)
        if n is not None:
            return n
        row = self.conn.execute(
            "SELECT COUNT(*) FROM agent_trades WHERE agent_id=? AND kind='shadow' AND mode=?",
            (agent_id, self.mode)).fetchone()
        n = int(row[0]) if row else 0
        self._trades[agent_id] = n
        return n

    def check_kills(self, ctx: AgentContext) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        floor = self.cfg.stake_usd * (1.0 + self.cfg.kill_floor_pct)
        for agent_id in list(self._brokers):
            if self.is_dead(agent_id):
                continue
            eq = self.shadow_equity(agent_id)
            n = self.trade_count(agent_id)
            key = f"agent_kill_since:{agent_id}"
            if eq > floor or n < self.cfg.min_trades:
                self._strikes[agent_id] = 0
                if get_flag(self.conn, key):
                    set_flag(self.conn, key, "")
                continue
            self._strikes[agent_id] = self._strikes.get(agent_id, 0) + 1
            since_raw = get_flag(self.conn, key)
            if not since_raw:
                set_flag(self.conn, key, repr(ctx.now))
                since = ctx.now
            else:
                since = float(since_raw)
            need = max(self.cfg.confirm_ticks, 1)
            if self._strikes[agent_id] < need and (ctx.now - since) < need * 60:
                continue
            reason = (f"agent '{agent_id}' disabled: shadow equity ${eq:,.0f} <= "
                      f"floor ${floor:,.0f} after {n} trades")
            self._switch(agent_id).kill(reason, source="ledger",
                                        shadow_equity=eq, stake=self.cfg.stake_usd, trades=n)
            out.append((agent_id, reason))
        return out

    def revive(self, agent_id: str) -> None:
        self._switch(agent_id).revive()
        self._brokers.pop(agent_id, None)
        self._meta.pop(agent_id, None)
        self._trades.pop(agent_id, None)
        self._strikes.pop(agent_id, None)


def _last_atr(df) -> float:
    from botcore.strategy import indicators as ind
    a = ind.atr(df["high"], df["low"], df["close"], 14).dropna()
    return float(a.iloc[-1]) if len(a) else 0.0


def _default_dead_dir(conn) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    db_path = row[2] if row and row[2] else ""
    return str(Path(db_path).parent / "agents") if db_path else "data/agents"
