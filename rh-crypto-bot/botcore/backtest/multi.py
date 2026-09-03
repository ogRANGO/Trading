"""Multi-agent backtest: the coordinator's blended book + each agent standalone.

Answers "which agents actually earn?" before any of them touch real money.
Reuses :func:`run_backtest` by feeding it a pre-computed ``sigs`` frame.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from botcore.agents.base import Agent, AgentContext
from botcore.agents.coordinator import Coordinator
from botcore.agents.registry import build_agents
from botcore.backtest.engine import BacktestResult, _common_index, run_backtest
from botcore.backtest.metrics import format_metrics
from botcore.backtest.plots import equity_drawdown_png
from botcore.config import CoordinatorCfg, get_settings, load_bot_config
from botcore.data.base import asset_class
from botcore.data.history import load_history
from botcore.strategy import indicators as ind

log = logging.getLogger(__name__)


class _NoopLedger:
    def is_dead(self, _):
        return False

    def record_signal(self, *a, **k):
        pass

    def tick(self, *a, **k):
        pass

    def check_kills(self, *a, **k):
        return []


def _row_for(df: pd.DataFrame, net, sym: str) -> dict:
    a = ind.atr(df["high"], df["low"], df["close"], 14).dropna()
    return {
        "entry": bool(net and net.enter),
        "hold": bool(net and net.enter),
        "exit": bool(net and net.veto),
        "score": float(net.score) if net else 0.0,
        "atr": float(a.iloc[-1]) if len(a) else 0.0,
        "close": float(df["close"].iloc[-1]),
    }


def blended_sigs(agents: List[Agent], weights: Dict[str, float], ccfg: CoordinatorCfg,
                 frames: Dict[str, pd.DataFrame], universe: List[str], klass: str,
                 warmup: int) -> Dict[str, pd.DataFrame]:
    coord = Coordinator(agents, weights, ccfg, _NoopLedger())
    index = _common_index(frames)
    rows: Dict[str, List[dict]] = {s: [] for s in frames}
    settings = get_settings()
    for i, date in enumerate(index):
        sliced = {s: df.loc[:date] for s, df in frames.items() if date in df.index}
        if i < warmup or not sliced:
            for s in frames:
                d = frames[s]
                if date in d.index:
                    rows[s].append(_row_for(d.loc[:date], None, s))
            continue
        quotes = {s: _FakeQuote(float(df["close"].iloc[-1])) for s, df in sliced.items()}
        ctx = AgentContext(bars=sliced, quotes=quotes, positions={}, equity=100_000.0,
                           universe=universe, now=date.timestamp(), conn=None,
                           settings=settings, klass=klass)
        net = coord._blend(ctx, {a.id: (_safe_signals(a, ctx)) for a in agents})
        for s, df in sliced.items():
            rows[s].append(_row_for(df, net.get(s), s))
    return {s: pd.DataFrame(r, index=[d for d in index if d in frames[s].index]) for s, r in rows.items()}


def _safe_signals(agent: Agent, ctx: AgentContext):
    try:
        return agent.signals(ctx) or []
    except Exception:  # noqa: BLE001
        return []


class _FakeQuote:
    def __init__(self, px: float) -> None:
        self.bid = px * 0.999
        self.ask = px * 1.001
        self.mid = px
        self.ts = time.time()


def run_multi(args: argparse.Namespace) -> int:
    cfg = load_bot_config()
    if args.universe:
        cfg.active_universe = args.universe
    tf = args.timeframe or cfg.market_data.timeframe
    syms = cfg.universe
    klass = "crypto" if all(asset_class(s) == "crypto" for s in syms) else "equity"
    frames = load_history(syms, tf, days=args.days, settings=get_settings())
    frames = {s: df for s, df in frames.items() if not df.empty}
    if not frames:
        print("no data"); return 1

    all_agents = [a for a in build_agents(cfg) if a.kind == "technical" or args.include_slow]
    weights = {a.id: cfg.agents[a.id].weight for a in all_agents}
    ccfg = cfg.coordinator
    curves = {}

    # blended
    bsig = blended_sigs(all_agents, weights, ccfg, frames, list(syms), klass, args.warmup)
    blend = run_backtest(frames, cfg, starting_equity=args.equity, warmup=args.warmup, sigs=bsig)
    print("\n=== BLENDED (all agents) ===")
    print(format_metrics(blend.metrics))
    curves["blended"] = (blend.equity, blend.metrics)

    # each agent standalone (min_agents_agree = 1, its own weight only)
    solo_cfg = CoordinatorCfg(min_agents_agree=1, min_net_conviction=0.01,
                              veto_conviction=ccfg.veto_conviction)
    for a in all_agents:
        if a.id == "vol_regime":
            continue  # veto-only, never opens
        ssig = blended_sigs([a], {a.id: 1.0}, solo_cfg, frames, list(syms), klass, args.warmup)
        res = run_backtest(frames, cfg, starting_equity=args.equity, warmup=args.warmup, sigs=ssig)
        print(f"\n=== {a.id} (standalone) ===")
        print(format_metrics(res.metrics))
        curves[a.id] = (res.equity, res.metrics)

    png = args.out or f"data/backtest_multi_{cfg.active_universe}.png"
    equity_drawdown_png(curves, png, title=f"Multi-agent — {cfg.active_universe}")
    print(f"\nchart -> {png}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="botcore.backtest.multi")
    p.add_argument("--universe", default="")
    p.add_argument("--timeframe", default="")
    p.add_argument("--days", type=int, default=1200)
    p.add_argument("--equity", type=float, default=100_000.0)
    p.add_argument("--warmup", type=int, default=150)
    p.add_argument("--include-slow", action="store_true", help="also run the news/flow agents")
    p.add_argument("--out", default="")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return run_multi(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
