from __future__ import annotations

import time

import numpy as np
import pandas as pd

from botcore.agents.base import Agent, AgentContext, AgentSignal
from botcore.agents.coordinator import Coordinator
from botcore.agents.ledger import AgentLedger
from botcore.brokers.base import Quote
from botcore.config import AgentKillCfg, CoordinatorCfg, Settings
from botcore.store.db import open_db


class FakeAgent(Agent):
    def __init__(self, aid, out):
        self.id = aid
        self._out = out
        self.asset_classes = frozenset({"crypto", "equity"})

    def signals(self, ctx):
        return list(self._out)


def _bars(n=120, px=100.0):
    idx = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")
    c = pd.Series(np.full(n, px), index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1.0})


def _ctx(tmp_path, syms=("BTC-USD", "ETH-USD")):
    conn = open_db(tmp_path / "c.db")
    bars = {s: _bars() for s in syms}
    quotes = {s: Quote(s, 99.9, 100.1, ts=time.time()) for s in syms}
    return AgentContext(bars=bars, quotes=quotes, positions={}, equity=100_000.0,
                        universe=list(syms), now=time.time(), conn=conn,
                        settings=Settings(_env_file=None), klass="crypto"), conn


def _coord(tmp_path, agents, weights, conn, cfg=None):
    ledger = AgentLedger(conn, AgentKillCfg(), mode="paper", dead_dir=tmp_path / "agents")
    return Coordinator(agents, weights, cfg or CoordinatorCfg(), ledger)


def test_needs_min_agents_to_agree(tmp_path):
    ctx, conn = _ctx(tmp_path)
    a1 = FakeAgent("a1", [AgentSignal("BTC-USD", 1, 0.9)])
    a2 = FakeAgent("a2", [])  # silent
    coord = _coord(tmp_path, [a1, a2], {"a1": 1.0, "a2": 1.0}, conn)
    net = coord.decide(ctx)
    assert net["BTC-USD"].enter is False        # only 1 agent agreed, need 2


def test_two_agents_agree_enters(tmp_path):
    ctx, conn = _ctx(tmp_path)
    a1 = FakeAgent("a1", [AgentSignal("BTC-USD", 1, 0.8)])
    a2 = FakeAgent("a2", [AgentSignal("BTC-USD", 1, 0.6)])
    coord = _coord(tmp_path, [a1, a2], {"a1": 1.0, "a2": 1.0}, conn)
    net = coord.decide(ctx)
    assert net["BTC-USD"].enter is True
    assert set(net["BTC-USD"].contributors) == {"a1", "a2"}


def test_veto_blocks_entry(tmp_path):
    ctx, conn = _ctx(tmp_path)
    a1 = FakeAgent("a1", [AgentSignal("BTC-USD", 1, 0.9)])
    a2 = FakeAgent("a2", [AgentSignal("BTC-USD", 1, 0.9)])
    veto = FakeAgent("v", [AgentSignal("BTC-USD", -1, 0.8)])
    coord = _coord(tmp_path, [a1, a2, veto], {"a1": 1, "a2": 1, "v": 1}, conn)
    net = coord.decide(ctx)
    assert net["BTC-USD"].enter is False and net["BTC-USD"].veto is True


def test_low_net_conviction_no_entry(tmp_path):
    ctx, conn = _ctx(tmp_path)
    a1 = FakeAgent("a1", [AgentSignal("BTC-USD", 1, 0.2)])
    a2 = FakeAgent("a2", [AgentSignal("BTC-USD", 1, 0.2)])
    coord = _coord(tmp_path, [a1, a2], {"a1": 0.3, "a2": 0.3}, conn,
                   cfg=CoordinatorCfg(min_agents_agree=2, min_net_conviction=0.5))
    assert coord.decide(ctx)["BTC-USD"].enter is False


def test_to_series_shape_matches_portfolio(tmp_path):
    ctx, conn = _ctx(tmp_path)
    a1 = FakeAgent("a1", [AgentSignal("BTC-USD", 1, 0.9)])
    a2 = FakeAgent("a2", [AgentSignal("BTC-USD", 1, 0.9)])
    coord = _coord(tmp_path, [a1, a2], {"a1": 1, "a2": 1}, conn)
    net = coord.decide(ctx)
    series = coord.to_series(net, ctx.bars)
    row = series["BTC-USD"]
    for k in ("entry", "hold", "exit", "score", "atr", "close"):
        assert k in row
    assert bool(row["entry"]) is True and row["close"] == 100.0


def test_dead_agent_excluded(tmp_path):
    ctx, conn = _ctx(tmp_path)
    a1 = FakeAgent("a1", [AgentSignal("BTC-USD", 1, 0.9)])
    a2 = FakeAgent("a2", [AgentSignal("BTC-USD", 1, 0.9)])
    coord = _coord(tmp_path, [a1, a2], {"a1": 1, "a2": 1}, conn)
    coord.ledger._switch("a2").kill("test disable")
    net = coord.decide(ctx)
    assert net["BTC-USD"].enter is False        # only a1 left, need 2
