#!/usr/bin/env python3
"""Pre-populate the candle cache so later backtests run offline.

    python scripts/backfill_history.py --universe crypto_major --days 1400
    python scripts/backfill_history.py --symbols BTC-USD ETH-USD --timeframe 1Hour
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

from botcore.config import get_settings, load_bot_config
from botcore.data.history import load_history


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--universe")
    p.add_argument("--symbols", nargs="*")
    p.add_argument("--timeframe", default="1Day")
    p.add_argument("--days", type=int, default=1400)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_bot_config()
    symbols = args.symbols or cfg.universes[args.universe or cfg.active_universe]

    frames = load_history(symbols, args.timeframe, days=args.days, settings=get_settings())
    for sym, df in frames.items():
        if df.empty:
            print(f"  {sym:10s}  NO DATA")
        else:
            print(f"  {sym:10s}  {len(df):5d} bars  {df.index.min().date()} -> {df.index.max().date()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
