"""Equity-curve + drawdown charts for backtests and the risk sweep."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

import matplotlib
import pandas as pd

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

_BG = "#0b0e12"
_FG = "#c8d0da"
_GRID = "#233040"
_COLORS = ["#4da3ff", "#37d67a", "#ff6b6b", "#f0b429", "#b980f0", "#5bd1d7"]


def _drawdown(equity: pd.Series) -> pd.Series:
    return equity / equity.cummax() - 1.0


def equity_drawdown_png(
    curves: Dict[str, Tuple[pd.Series, dict]],
    path: "str | Path",
    *,
    title: str = "Backtest",
    logy: bool = True,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [2.4, 1]}
    )
    fig.patch.set_facecolor(_BG)
    for ax in (ax1, ax2):
        ax.set_facecolor(_BG)
        ax.tick_params(colors=_FG)
        for s in ax.spines.values():
            s.set_color(_GRID)
        ax.grid(True, color=_GRID, linewidth=0.5, alpha=0.6)

    for i, (label, (equity, metrics)) in enumerate(curves.items()):
        c = _COLORS[i % len(_COLORS)]
        dd = _drawdown(equity)
        tot = metrics.get("total_return", float("nan")) * 100
        mdd = metrics.get("max_drawdown", float("nan")) * 100
        shp = metrics.get("sharpe", float("nan"))
        ax1.plot(equity.index, equity.values, color=c, linewidth=1.4,
                 label=f"{label}  ({tot:+.0f}%, Sharpe {shp:.2f})")
        ax2.plot(dd.index, dd.values * 100, color=c, linewidth=1.0,
                 label=f"{label}  maxDD {mdd:.1f}%")

    if logy:
        ax1.set_yscale("log")
    ax1.set_title(title, color=_FG, fontsize=13)
    ax1.set_ylabel("Equity ($)", color=_FG)
    ax1.legend(facecolor=_BG, edgecolor=_GRID, labelcolor=_FG, fontsize=8, loc="upper left")
    ax2.set_ylabel("Drawdown (%)", color=_FG)
    ax2.legend(facecolor=_BG, edgecolor=_GRID, labelcolor=_FG, fontsize=8, loc="lower left")
    ax2.axhline(0, color=_GRID, linewidth=0.8)

    fig.tight_layout()
    fig.savefig(path, dpi=120, facecolor=_BG)
    plt.close(fig)
    return path
