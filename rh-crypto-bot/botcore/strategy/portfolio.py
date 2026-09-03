"""Portfolio construction: rank signals, pick <= N names, risk-size each.

Runs the same in backtest and live. The engine/backtester is responsible for
turning a :class:`PortfolioDecision` into broker orders and for managing exits
via :mod:`botcore.strategy.exitplan`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from botcore.config import PortfolioCfg
from botcore.data.base import asset_class


@dataclass
class SizedEntry:
    symbol: str
    qty: float
    ref_price: float
    atr: float
    score: float
    risk_dollars: float
    reason: str = ""
    # Level-based families (smc) carry their own stop/targets; None = size and
    # exit off ATR multiples, as trend and mean_reversion always have.
    stop: Optional[float] = None
    tp1: Optional[float] = None
    target: Optional[float] = None


@dataclass
class PortfolioDecision:
    entries: List[SizedEntry] = field(default_factory=list)
    signal_exits: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _level(row: pd.Series, key: str) -> Optional[float]:
    """Read a price level from a signal row, treating missing/NaN as absent."""
    try:
        v = row.get(key)
    except AttributeError:
        return None
    if v is None:
        return None
    v = float(v)
    return v if v == v and v > 0 else None      # v != v filters NaN


def _round_qty(symbol: str, qty: float) -> float:
    step = 1e-8 if asset_class(symbol) == "crypto" else 1e-3
    return float(int(qty / step) * step)


class PortfolioManager:
    def __init__(
        self,
        cfg: PortfolioCfg,
        *,
        max_trade_usd: Optional[float] = None,
        whole_shares: bool = False,
    ) -> None:
        self.cfg = cfg
        self.max_trade_usd = max_trade_usd
        self.whole_shares = whole_shares

    def size(
        self, symbol: str, ref_price: float, atr: float, equity: float,
        *, stop: Optional[float] = None,
    ) -> float:
        """Risk-sized quantity.

        With an explicit ``stop`` the risk per share is the real distance to it,
        which is the whole point of the level-based families: an OB wick sits
        wherever it sits, and pretending it is N x ATR mis-sizes every trade.
        """
        if ref_price <= 0 or atr <= 0 or equity <= 0:
            return 0.0
        risk_dollars = equity * self.cfg.risk_fraction

        stop_distance = self.cfg.exit.hard_stop_atr_mult * atr
        if stop is not None and 0 < stop < ref_price:
            stop_distance = ref_price - stop
        if stop_distance <= 0:
            return 0.0
        qty = risk_dollars / stop_distance

        cap_notional = self.cfg.max_position_weight * equity
        if self.max_trade_usd is not None:
            cap_notional = min(cap_notional, self.max_trade_usd)
        qty = min(qty, cap_notional / ref_price)

        if self.whole_shares and asset_class(symbol) == "equity":
            qty = float(int(qty))
        else:
            qty = _round_qty(symbol, qty)

        if qty * ref_price < self.cfg.min_notional_usd:
            return 0.0
        return qty

    def plan(
        self,
        *,
        signals: Dict[str, pd.Series],
        holdings: Dict[str, float],
        equity: float,
    ) -> PortfolioDecision:
        d = PortfolioDecision()
        held = {s for s, q in holdings.items() if abs(q) > 1e-12}

        for sym in held:
            row = signals.get(sym)
            if row is not None and bool(row.get("exit", False)):
                d.signal_exits.append(sym)

        remaining_after_exits = held - set(d.signal_exits)
        free_slots = self.cfg.max_positions - len(remaining_after_exits)
        if free_slots <= 0:
            d.notes.append(f"no free slots ({len(remaining_after_exits)}/{self.cfg.max_positions})")
            return d

        candidates = [
            (sym, row)
            for sym, row in signals.items()
            if sym not in held and bool(row.get("entry", False)) and float(row.get("score", 0)) > 0
        ]
        candidates.sort(key=lambda t: float(t[1]["score"]), reverse=True)

        for sym, row in candidates[:free_slots]:
            ref_price = float(row["close"])
            atr = float(row["atr"])
            stop = _level(row, "stop")
            qty = self.size(sym, ref_price, atr, equity, stop=stop)
            if qty <= 0:
                d.notes.append(f"{sym}: sized to zero")
                continue
            d.entries.append(
                SizedEntry(
                    symbol=sym, qty=qty, ref_price=ref_price, atr=atr,
                    score=float(row["score"]),
                    risk_dollars=equity * self.cfg.risk_fraction,
                    reason=f"{self.cfg.__class__.__name__}: score={float(row['score']):.2f}",
                    stop=stop, tp1=_level(row, "tp1"), target=_level(row, "target"),
                )
            )
        return d
