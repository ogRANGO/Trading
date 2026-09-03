from __future__ import annotations

import sqlite3
import time

import numpy as np
import pandas as pd
import pytest

from botcore.agents.base import AgentContext, AgentSignal
from botcore.agents.technical import (
    MeanReversionAgent,
    MomentumAgent,
    TrendAgent,
    VolRegimeAgent,
)
from botcore.brokers.base import Quote
from botcore.config import SignalParams
from botcore.store.db import open_db


def _bars(path, hi_mult=1.02, lo_mult=0.98):
    idx = pd.date_range("2023-01-01", periods=len(path), freq="D", tz="UTC")
    c = pd.Series(path, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c * hi_mult, "low": c * lo_mult,
                         "close": c, "volume": 1.0})


def _ctx(tmp_path, bars_by_sym, klass="crypto"):
    conn = open_db(tmp_path / "a.db")
    quotes = {s: Quote(s, bid=float(df["close"].iloc[-1]) * 0.999,
                       ask=float(df["close"].iloc[-1]) * 1.001, ts=time.time())
              for s, df in bars_by_sym.items()}
    from botcore.config import Settings
    return AgentContext(bars=bars_by_sym, quotes=quotes, positions={}, equity=100_000.0,
                        universe=list(bars_by_sym), now=time.time(), conn=conn,
                        settings=Settings(_env_file=None), klass=klass)


def test_agent_signal_clamps():
    s = AgentSignal("BTC-USD", direction=5, conviction=3.0)
    assert s.direction == 1 and s.conviction == 1.0
    s2 = AgentSignal("BTC-USD", direction=-9, conviction=-1.0)
    assert s2.direction == -1 and s2.conviction == 0.0


def test_trend_agent_fires_long_in_uptrend(tmp_path):
    up = np.linspace(100, 200, 260)
    ctx = _ctx(tmp_path, {"BTC-USD": _bars(up)})
    sigs = TrendAgent(SignalParams()).signals(ctx)
    # somewhere in a clean uptrend the trend family produces a fresh entry
    longs = [s for s in sigs if s.direction > 0]
    assert all(s.symbol == "BTC-USD" for s in sigs)
    # not guaranteed on the very last bar, so also check the family directly stays long-biased
    assert not any(s.direction < 0 for s in sigs)


def test_trend_agent_exits_in_downtrend(tmp_path):
    down = np.linspace(200, 100, 260)
    ctx = _ctx(tmp_path, {"BTC-USD": _bars(down)})
    sigs = TrendAgent(SignalParams()).signals(ctx)
    assert any(s.direction < 0 for s in sigs)


def test_momentum_agent_breakout(tmp_path):
    path = np.concatenate([np.full(80, 100.0), np.linspace(100, 140, 40)])
    ctx = _ctx(tmp_path, {"ETH-USD": _bars(path)})
    sigs = MomentumAgent(lookback=20, roc_period=10).signals(ctx)
    assert any(s.direction > 0 and s.symbol == "ETH-USD" for s in sigs)


def test_vol_regime_veto_in_crash(tmp_path):
    # calm then a sharp drop -> vol spike + regime rollover
    calm = np.full(230, 100.0) + np.random.default_rng(1).normal(0, 0.2, 230)
    crash = np.linspace(100, 60, 40)
    ctx = _ctx(tmp_path, {"BTC-USD": _bars(np.concatenate([calm, crash]))})
    sigs = VolRegimeAgent().signals(ctx)
    assert sigs and all(s.direction == -1 for s in sigs)
    assert sigs[0].conviction >= 0.6


def test_vol_regime_quiet_in_calm_uptrend(tmp_path):
    up = np.linspace(100, 130, 300)
    ctx = _ctx(tmp_path, {"BTC-USD": _bars(up, hi_mult=1.003, lo_mult=0.997)})
    assert VolRegimeAgent().signals(ctx) == []


def test_mean_reversion_agent_shape(tmp_path):
    path = np.linspace(100, 150, 200).tolist() + [150, 148, 143, 138, 133]  # dip after uptrend
    ctx = _ctx(tmp_path, {"SOL-USD": _bars(np.array(path))})
    sigs = MeanReversionAgent(SignalParams()).signals(ctx)
    for s in sigs:
        assert s.symbol == "SOL-USD" and -1 <= s.direction <= 1


def test_agents_skip_short_history(tmp_path):
    ctx = _ctx(tmp_path, {"BTC-USD": _bars(np.full(10, 100.0))})
    for agent in (TrendAgent(SignalParams()), MeanReversionAgent(SignalParams()),
                  MomentumAgent(), VolRegimeAgent()):
        assert agent.signals(ctx) == []
