"""Performance statistics for an equity curve + a trade blotter."""

from __future__ import annotations

from typing import Dict, List

import numpy as np
import pandas as pd


def _safe_div(a: float, b: float) -> float:
    return a / b if b not in (0, 0.0) else float("nan")


def compute_metrics(
    equity: pd.Series, trades: List[dict], bars_per_year: float
) -> Dict[str, float]:
    equity = equity.dropna().astype(float)
    if len(equity) < 2:
        return {"error": 1.0, "num_trades": float(len(trades))}

    start_eq, end_eq = float(equity.iloc[0]), float(equity.iloc[-1])
    rets = equity.pct_change().dropna()

    # Use the exact span, not whole days: an intraday backtest spans 0 days, and
    # annualising over ~0 overflows the exponent. Floored at one day, because
    # annualising a six-hour window is meaningless however it is computed.
    span_seconds = (equity.index[-1] - equity.index[0]).total_seconds()
    span_years = max(span_seconds / (365.25 * 86400.0), 1.0 / 365.25)
    total_return = end_eq / start_eq - 1.0
    try:
        cagr = (end_eq / start_eq) ** (1.0 / span_years) - 1.0 if end_eq > 0 else -1.0
    except OverflowError:  # absurd compounding on a very short window
        cagr = float("inf") if end_eq > start_eq else -1.0

    vol = float(rets.std(ddof=0) * np.sqrt(bars_per_year))
    sharpe = _safe_div(float(rets.mean()), float(rets.std(ddof=0))) * np.sqrt(bars_per_year)
    downside = rets[rets < 0]
    sortino = _safe_div(float(rets.mean()), float(downside.std(ddof=0))) * np.sqrt(bars_per_year)

    roll_max = equity.cummax()
    drawdown = equity / roll_max - 1.0
    max_dd = float(drawdown.min())
    calmar = _safe_div(cagr, abs(max_dd))

    pnls = np.array([t["pnl"] for t in trades], dtype=float)
    r_multiples = np.array(
        [t["pnl"] / t["risk_dollars"] for t in trades if t.get("risk_dollars")], dtype=float
    )
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    bars_held = np.array([t.get("bars_held", np.nan) for t in trades], dtype=float)

    return {
        "start_equity": start_eq,
        "end_equity": end_eq,
        "total_return": total_return,
        "cagr": cagr,
        "volatility": vol,
        "sharpe": float(sharpe),
        "sortino": float(sortino),
        "max_drawdown": max_dd,
        "calmar": float(calmar),
        "num_trades": float(len(trades)),
        "win_rate": _safe_div(float(len(wins)), float(len(pnls))),
        "profit_factor": _safe_div(gross_win, gross_loss),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "avg_r_multiple": float(np.nanmean(r_multiples)) if r_multiples.size else float("nan"),
        "expectancy": float(pnls.mean()) if len(pnls) else 0.0,
        "avg_bars_held": float(np.nanmean(bars_held)) if bars_held.size else float("nan"),
        "total_fees": float(sum(t.get("fees", 0.0) for t in trades)),
    }


PCT_KEYS = {"total_return", "cagr", "volatility", "max_drawdown", "win_rate"}
MONEY_KEYS = {"start_equity", "end_equity", "avg_win", "avg_loss", "expectancy", "total_fees"}


def format_metrics(m: Dict[str, float]) -> str:
    order = [
        "start_equity", "end_equity", "total_return", "cagr", "volatility",
        "sharpe", "sortino", "max_drawdown", "calmar", "num_trades", "win_rate",
        "profit_factor", "avg_win", "avg_loss", "avg_r_multiple", "expectancy",
        "avg_bars_held", "total_fees",
    ]
    lines = []
    for k in order:
        if k not in m:
            continue
        v = m[k]
        if k in PCT_KEYS:
            lines.append(f"  {k:16s} {v * 100:>10.2f}%")
        elif k in MONEY_KEYS:
            lines.append(f"  {k:16s} {v:>11,.2f}")
        else:
            lines.append(f"  {k:16s} {v:>11.2f}")
    return "\n".join(lines)
