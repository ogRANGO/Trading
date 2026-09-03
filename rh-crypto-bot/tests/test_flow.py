from __future__ import annotations

import time

import numpy as np
import pandas as pd

from botcore.agents.base import AgentContext
from botcore.agents.flow import FlowAgent
from botcore.config import AgentCfg, Settings
from botcore.data.flow import FundingSnapshot, _binance_symbol, fetch_flow
from botcore.store.db import open_db


def _bars(px, n=60):
    idx = pd.date_range("2023-01-01", periods=n, freq="h", tz="UTC")
    c = pd.Series(np.linspace(px * 0.9, px, n), index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1.0})


def _ctx(tmp_path, klass="crypto"):
    conn = open_db(tmp_path / "f.db")
    return AgentContext(bars={"BTC-USD": _bars(100.0)}, quotes={}, positions={}, equity=1e5,
                        universe=["BTC-USD"], now=time.time(), conn=conn,
                        settings=Settings(_env_file=None), klass=klass)


def test_binance_symbol_map():
    assert _binance_symbol("BTC-USD") == "BTCUSDT"
    assert _binance_symbol("eth/usd") == "ETHUSDT"


def test_fetch_flow_parses(monkeypatch):
    import botcore.data.flow as mod

    def fake_get(path, params, timeout=8.0):
        if "premiumIndex" in path:
            return {"lastFundingRate": "0.0009", "markPrice": "63000.0"}
        return [{"sumOpenInterest": "100"}, {"sumOpenInterest": "115"}]

    monkeypatch.setattr(mod, "_get", fake_get)
    snap = fetch_flow("BTC-USD")
    assert snap.funding_rate == 0.0009
    assert round(snap.oi_change_pct, 1) == 15.0


def test_fetch_flow_failure_is_none(monkeypatch):
    import botcore.data.flow as mod
    monkeypatch.setattr(mod, "_get", lambda *a, **k: None)
    assert fetch_flow("BTC-USD") is None


def test_flow_agent_fades_hot_funding(tmp_path, monkeypatch):
    import botcore.agents.flow as mod
    monkeypatch.setattr(mod, "fetch_flow",
                        lambda sym: FundingSnapshot(sym, 0.0012, 100.0, 5.0))
    sigs = FlowAgent(AgentCfg(poll_minutes=1)).signals(_ctx(tmp_path))
    assert sigs and sigs[0].direction == -1


def test_flow_agent_follows_negative_funding(tmp_path, monkeypatch):
    import botcore.agents.flow as mod
    monkeypatch.setattr(mod, "fetch_flow",
                        lambda sym: FundingSnapshot(sym, -0.0003, 100.0, 8.0))
    sigs = FlowAgent(AgentCfg(poll_minutes=1)).signals(_ctx(tmp_path))
    assert sigs and sigs[0].direction == 1


def test_flow_agent_silent_on_equities(tmp_path, monkeypatch):
    import botcore.agents.flow as mod
    monkeypatch.setattr(mod, "fetch_flow", lambda sym: FundingSnapshot(sym, 0.0012, 100.0, 5.0))
    assert FlowAgent(AgentCfg(poll_minutes=1)).signals(_ctx(tmp_path, klass="equity")) == []
