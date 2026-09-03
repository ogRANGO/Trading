"""Bar-by-bar backtest: signals -> portfolio -> exit plans -> simulated fills."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import pandas as pd

from botcore.brokers.sim import SimBroker
from botcore.config import BotConfig
from botcore.data.base import Timeframe, asset_class, bars_per_year
from botcore.strategy.exitplan import (
    build_plan, check_exit, entries_allowed, take_tp1, update_trail,
)
from botcore.strategy.portfolio import PortfolioManager
from botcore.strategy.signals import get_signal_fn
from botcore.backtest.metrics import compute_metrics


@dataclass
class BacktestResult:
    equity: pd.Series
    trades: pd.DataFrame
    metrics: Dict[str, float]
    summary: Dict[str, object] = field(default_factory=dict)


def _common_index(frames: Dict[str, pd.DataFrame]) -> pd.DatetimeIndex:
    idx: Optional[pd.DatetimeIndex] = None
    for df in frames.values():
        if df.empty:
            continue
        idx = df.index if idx is None else idx.union(df.index)
    return idx if idx is not None else pd.DatetimeIndex([], tz="UTC")


def run_backtest(
    frames: Dict[str, pd.DataFrame],
    cfg: BotConfig,
    *,
    starting_equity: float = 100_000.0,
    risk_fraction: Optional[float] = None,
    signal_family: Optional[str] = None,
    warmup: int = 150,
    sigs: Optional[Dict[str, pd.DataFrame]] = None,
) -> BacktestResult:
    frames = {s: df for s, df in frames.items() if not df.empty}
    if not frames:
        raise ValueError("no data to backtest")

    family = signal_family or cfg.strategy.signal_family
    if sigs is None:
        signal_fn = get_signal_fn(family, cfg.strategy.params)
        sigs = {s: signal_fn(df) for s, df in frames.items()}
    else:
        sigs = {s: df for s, df in sigs.items() if s in frames}

    pcfg = cfg.portfolio.model_copy(deep=True)
    if risk_fraction is not None:
        pcfg.risk_fraction = risk_fraction
    pm = PortfolioManager(pcfg)
    exit_cfg = pcfg.exit_for(family)

    broker = SimBroker(starting_cash=starting_equity, fees=cfg.fees)
    index = _common_index(frames)
    tf = Timeframe.parse(cfg.market_data.timeframe)
    klass = asset_class(next(iter(frames)))
    bpy = bars_per_year(tf, klass)

    plans: dict = {}
    entry_info: Dict[str, dict] = {}
    trades: List[dict] = []
    equity_points: List[float] = []

    def _close(symbol: str, i: int, date, price: float, reason: str,
               qty: "float | None" = None) -> None:
        """Close ``qty`` of a position (default: all of it).

        Mirrors the live engine's partial path so a TP1 taken in backtest is the
        same event as a TP1 taken live: the slice books its own trade row with a
        pro-rata share of the entry fee, and the remainder keeps its plan.
        """
        pos = broker.get_position(symbol)
        if pos is None:
            return
        full_qty = pos.qty
        sell_qty = full_qty if qty is None else min(qty, full_qty)
        if sell_qty <= 0:
            return
        partial = sell_qty < full_qty - 1e-12

        order = broker.fill_market(symbol, "sell", sell_qty, ref_price=price)
        info = entry_info.get(symbol, {}) if partial else entry_info.pop(symbol, {})
        entry_px = info.get("price", order.filled_avg_price)
        held_qty = info.get("qty", order.filled_qty)
        share = (sell_qty / held_qty) if held_qty else 1.0
        entry_fee_share = info.get("fee", 0.0) * share
        qty = sell_qty
        fees = entry_fee_share + order.fee
        pnl = (order.filled_avg_price - entry_px) * qty - fees
        trades.append({
            "symbol": symbol,
            "entry_date": info.get("date"),
            "exit_date": date,
            "entry_price": entry_px,
            "exit_price": order.filled_avg_price,
            "qty": qty,
            "pnl": pnl,
            "return_pct": (order.filled_avg_price / entry_px - 1.0) if entry_px else 0.0,
            "fees": fees,
            "bars_held": i - info.get("bar", i),
            "risk_dollars": info.get("risk_dollars", 0.0),
            "reason": reason,
        })
        if partial:
            plan = plans.get(symbol)
            if plan is not None and reason == "tp1":
                take_tp1(plan)
            info["qty"] = max(held_qty - sell_qty, 0.0)
            info["fee"] = info.get("fee", 0.0) - entry_fee_share
            return
        plans.pop(symbol, None)

    for i, date in enumerate(index):
        bars_today = {
            s: {k: float(frames[s].loc[date, k]) for k in ("open", "high", "low", "close")}
            for s in frames
            if date in frames[s].index
        }
        broker.mark(bars_today, clock=date.timestamp())

        # 1) manage exits on open positions
        for sym in list(broker.positions_by_symbol()):
            if sym not in bars_today:
                continue
            row = sigs[sym].loc[date] if date in sigs[sym].index else None
            atr_now = float(row["atr"]) if row is not None and pd.notna(row["atr"]) else 0.0
            plan = plans.get(sym)
            if plan is None:
                continue
            b = bars_today[sym]
            update_trail(plan, b["high"], atr_now, exit_cfg)
            hit = check_exit(plan, b["low"], b["high"], i, cfg=exit_cfg, now=date)
            if hit:
                reason, px = hit
                # tp1 is the only partial exit; everything else closes the lot
                part = (plan.tp1_fraction * entry_info.get(sym, {}).get("qty", 0.0)
                        if reason == "tp1" else None)
                _close(sym, i, date, px if px is not None else b["close"], reason, qty=part)

        equity_now = broker.get_account().equity

        # 2) portfolio decision (only after warmup)
        if i >= warmup:
            signals_today = {
                s: sigs[s].loc[date] for s in bars_today if date in sigs[s].index
            }
            holdings = {p.symbol: p.qty for p in broker.get_positions()}
            decision = pm.plan(signals=signals_today, holdings=holdings, equity=equity_now)

            for sym in decision.signal_exits:
                if sym in bars_today:
                    _close(sym, i, date, bars_today[sym]["close"], "signal_exit")

            if not entries_allowed(exit_cfg, date):
                decision.entries = []      # no new risk into the close

            for e in decision.entries:
                if e.symbol not in bars_today:
                    continue
                order = broker.fill_market(e.symbol, "buy", e.qty, ref_price=e.ref_price)
                entry_info[e.symbol] = {
                    "price": order.filled_avg_price, "qty": order.filled_qty,
                    "fee": order.fee, "date": date, "bar": i,
                    "risk_dollars": e.risk_dollars,
                }
                plans[e.symbol] = build_plan(
                    order.filled_avg_price, e.atr, i, exit_cfg,
                    stop=e.stop, target=e.target, tp1=e.tp1,
                )

        equity_points.append(broker.get_account().equity)

    # close survivors at the last bar
    if len(index):
        last = index[-1]
        for sym in list(broker.positions_by_symbol()):
            price = float(frames[sym].loc[last, "close"]) if last in frames[sym].index else \
                broker.get_position(sym).market_price
            _close(sym, len(index) - 1, last, price, "backtest_end")
        equity_points[-1] = broker.get_account().equity

    equity = pd.Series(equity_points, index=index, name="equity")
    trades_df = pd.DataFrame(trades)
    metrics = compute_metrics(equity, trades, bpy)
    return BacktestResult(
        equity=equity,
        trades=trades_df,
        metrics=metrics,
        summary={
            "universe": cfg.active_universe,
            "symbols": list(frames),
            "signal_family": family,
            "timeframe": str(tf),
            "risk_fraction": pcfg.risk_fraction,
            "bars": len(index),
            "start": str(index[0].date()) if len(index) else None,
            "end": str(index[-1].date()) if len(index) else None,
        },
    )
