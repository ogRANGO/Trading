"""Daily summary: a one-line P&L / activity digest over the trailing 24h.

Read-only over the store; produced by the engine's daily cron job and pushed via
:mod:`botcore.notify.push`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class DailySummary:
    mode: str
    equity_now: float
    equity_start: float
    realized_pnl_24h: float
    trades_24h: int
    open_positions: int
    warn_24h: int
    error_24h: int
    halt_24h: int

    @property
    def equity_delta_24h(self) -> float:
        return self.equity_now - self.equity_start

    def oneline(self) -> str:
        d = self.equity_delta_24h
        return (
            f"{self.mode}: equity ${self.equity_now:,.0f} ({d:+,.0f} 24h) | "
            f"realized {self.realized_pnl_24h:+,.2f} on {self.trades_24h} trade(s) | "
            f"{self.open_positions} open | "
            f"{self.warn_24h}w/{self.error_24h}e/{self.halt_24h}halt"
        )

    def as_ntfy(self) -> Tuple[str, str, List[str]]:
        up = self.equity_delta_24h >= 0
        tags: List[str] = ["chart_with_upwards_trend" if up else "chart_with_downwards_trend"]
        if self.halt_24h or self.error_24h:
            tags.append("warning")
        return (f"Daily summary — {self.mode}", self.oneline(), tags)


def build_daily_summary(conn, mode: str, now: Optional[float] = None) -> DailySummary:
    now = now if now is not None else time.time()
    since = now - 86400

    n, realized = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM trades WHERE mode=? AND closed_ts >= ?",
        (mode, since),
    ).fetchone()

    eq_rows = conn.execute(
        "SELECT equity FROM equity_snapshots WHERE mode=? AND ts >= ? ORDER BY ts",
        (mode, since),
    ).fetchall()
    if eq_rows:
        equity_start = float(eq_rows[0][0])
        equity_now = float(eq_rows[-1][0])
    else:
        last = conn.execute(
            "SELECT equity FROM equity_snapshots WHERE mode=? ORDER BY ts DESC LIMIT 1", (mode,)
        ).fetchone()
        equity_now = equity_start = float(last[0]) if last else 0.0

    levels = {
        r[0]: int(r[1])
        for r in conn.execute(
            "SELECT level, COUNT(*) FROM events WHERE ts >= ? AND level IN ('warn','error','halt') "
            "GROUP BY level",
            (since,),
        )
    }
    open_pos = int(
        conn.execute("SELECT COUNT(*) FROM positions WHERE mode=?", (mode,)).fetchone()[0]
    )

    return DailySummary(
        mode=mode,
        equity_now=equity_now,
        equity_start=equity_start,
        realized_pnl_24h=float(realized),
        trades_24h=int(n),
        open_positions=open_pos,
        warn_24h=levels.get("warn", 0),
        error_24h=levels.get("error", 0),
        halt_24h=levels.get("halt", 0),
    )
