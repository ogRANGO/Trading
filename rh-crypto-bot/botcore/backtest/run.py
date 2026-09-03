"""Single backtest run: metrics table + equity/drawdown PNG.

    python -m botcore.backtest.run --universe crypto_major --signals both --days 1200
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from botcore.backtest.engine import run_backtest
from botcore.backtest.metrics import format_metrics
from botcore.backtest.plots import equity_drawdown_png
from botcore.config import REPO_ROOT, get_settings, load_bot_config
from botcore.data.history import load_history

log = logging.getLogger(__name__)


def _families(arg: str) -> List[str]:
    return ["trend", "mean_reversion"] if arg == "both" else [arg]


def run(args: argparse.Namespace) -> int:
    cfg = load_bot_config()
    if args.universe:
        cfg.active_universe = args.universe
    if args.timeframe:
        cfg.market_data.timeframe = args.timeframe
    symbols = cfg.universe

    log.info("loading %d symbols (%s, %s) ...", len(symbols), cfg.active_universe, cfg.market_data.timeframe)
    frames = load_history(
        symbols, cfg.market_data.timeframe, days=args.days, settings=get_settings()
    )
    frames = {s: df for s, df in frames.items() if not df.empty}
    if not frames:
        print("no data loaded (equities need ALPACA_KEY_ID/SECRET; crypto is keyless).")
        return 2
    missing = [s for s in symbols if s not in frames]
    if missing:
        print(f"warning: no data for {', '.join(missing)}")

    out_dir = Path(args.out) if args.out else REPO_ROOT / "data"
    curves = {}
    for fam in _families(args.signals):
        res = run_backtest(
            frames, cfg, starting_equity=args.equity,
            risk_fraction=args.risk_fraction, signal_family=fam, warmup=args.warmup,
        )
        print(f"\n=== {fam}  |  {res.summary['start']} -> {res.summary['end']}  "
              f"|  {res.summary['bars']} bars  |  risk_fraction={res.summary['risk_fraction']} ===")
        print(format_metrics(res.metrics))
        if not res.trades.empty:
            by_reason = res.trades["reason"].value_counts().to_dict()
            print(f"  exits: {by_reason}")
        curves[fam] = (res.equity, res.metrics)

    png = out_dir / f"backtest_{cfg.active_universe}_{args.signals}.png"
    equity_drawdown_png(curves, png, title=f"Backtest: {cfg.active_universe} ({args.signals})")
    print(f"\nchart -> {png}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="botcore.backtest.run")
    p.add_argument("--universe", help="name from config.yaml (default: active_universe)")
    p.add_argument("--signals", choices=["trend", "mean_reversion", "both"], default="both")
    p.add_argument("--timeframe", help="override market_data.timeframe, e.g. 1Day / 1Hour")
    p.add_argument("--days", type=int, default=1200)
    p.add_argument("--equity", type=float, default=100_000.0)
    p.add_argument("--risk-fraction", type=float, default=None, dest="risk_fraction")
    p.add_argument("--warmup", type=int, default=150)
    p.add_argument("--out", help="output directory for the PNG")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
