"""Risk-per-trade sweep -> the "sweet spot" equity/drawdown comparison.

    python -m botcore.backtest.sweep --universe crypto_major --signals trend \
        --fractions 0.0025,0.005,0.01,0.02,0.03,0.05

Shows how return, drawdown, and risk-adjusted return move as you raise the
fraction of equity risked per trade -- the trade-off in your reference chart.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from botcore.backtest.engine import run_backtest
from botcore.backtest.plots import equity_drawdown_png
from botcore.config import REPO_ROOT, get_settings, load_bot_config
from botcore.data.history import load_history

log = logging.getLogger(__name__)


def _parse_fractions(s: str) -> List[float]:
    return [float(x) for x in s.replace(" ", "").split(",") if x]


def run(args: argparse.Namespace) -> int:
    cfg = load_bot_config()
    if args.universe:
        cfg.active_universe = args.universe
    if args.timeframe:
        cfg.market_data.timeframe = args.timeframe

    fractions = _parse_fractions(args.fractions)
    frames = load_history(
        cfg.universe, cfg.market_data.timeframe, days=args.days, settings=get_settings()
    )
    frames = {s: df for s, df in frames.items() if not df.empty}
    if not frames:
        print("no data loaded.")
        return 2

    curves = {}
    rows = []
    for rf in fractions:
        res = run_backtest(
            frames, cfg, starting_equity=args.equity,
            risk_fraction=rf, signal_family=args.signals, warmup=args.warmup,
        )
        m = res.metrics
        curves[f"risk {rf * 100:.2f}%/trade"] = (res.equity, m)
        rows.append((rf, m))

    print(f"\nrisk sweep: {cfg.active_universe} / {args.signals} / "
          f"{rows[0][1].get('num_trades', 0):.0f} trades\n")
    print(f"  {'risk/trade':>11} {'return':>9} {'CAGR':>8} {'maxDD':>8} "
          f"{'Sharpe':>7} {'Calmar':>7}")
    best = None
    for rf, m in rows:
        print(f"  {rf * 100:>10.2f}% {m['total_return'] * 100:>8.1f}% "
              f"{m['cagr'] * 100:>7.1f}% {m['max_drawdown'] * 100:>7.1f}% "
              f"{m['sharpe']:>7.2f} {m['calmar']:>7.2f}")
        key = m["calmar"] if m["calmar"] == m["calmar"] else -999
        if best is None or key > best[1]:
            best = (rf, key)
    if best:
        print(f"\n  best risk-adjusted (Calmar): {best[0] * 100:.2f}% per trade")

    out_dir = Path(args.out) if args.out else REPO_ROOT / "data"
    png = out_dir / f"sweep_{cfg.active_universe}_{args.signals}.png"
    equity_drawdown_png(
        curves, png, title=f"Risk-per-trade sweep: {cfg.active_universe} ({args.signals})"
    )
    print(f"\nchart -> {png}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="botcore.backtest.sweep")
    p.add_argument("--universe")
    p.add_argument("--signals", choices=["trend", "mean_reversion"], default="trend")
    p.add_argument("--timeframe")
    p.add_argument("--fractions", default="0.0025,0.005,0.01,0.02,0.03,0.05")
    p.add_argument("--days", type=int, default=1200)
    p.add_argument("--equity", type=float, default=100_000.0)
    p.add_argument("--warmup", type=int, default=150)
    p.add_argument("--out")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
