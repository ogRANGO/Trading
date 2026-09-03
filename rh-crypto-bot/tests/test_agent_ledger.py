from __future__ import annotations

import time

import numpy as np
import pandas as pd

from botcore.agents.base import AgentContext, AgentSignal
from botcore.agents.ledger import AgentLedger
from botcore.brokers.base import Quote
from botcore.config import AgentKillCfg, Settings
from botcore.store.db import open_db


def _bars(px_last=100.0, n=120):
    idx = pd.date_range("2023-01-01", periods=n, freq="D", tz="UTC")
    c = pd.Series(np.linspace(px_last * 0.8, px_last, n), index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.02, "low": c * 0.98, "close": c, "volume": 1.0})


def _ctx(tmp_path, conn, mid, now=None):
    q = Quote("BTC-USD", bid=mid * 0.999, ask=mid * 1.001, ts=time.time())
    return AgentContext(bars={"BTC-USD": _bars(mid)}, quotes={"BTC-USD": q}, positions={},
                        equity=100_000.0, universe=["BTC-USD"], now=now or time.time(),
                        conn=conn, settings=Settings(_env_file=None), klass="crypto")


def _ledger(tmp_path, conn, **kw):
    opts = dict(stake_usd=1000.0, kill_floor_pct=-0.15, confirm_ticks=1, min_trades=2)
    opts.update(kw)
    return AgentLedger(conn, AgentKillCfg(**opts), mode="paper", dead_dir=tmp_path / "agents")


def test_shadow_book_opens_and_closes(tmp_path):
    conn = open_db(tmp_path / "l.db")
    led = _ledger(tmp_path, conn)
    # tick 1: agent says buy at 100
    led.tick(_ctx(tmp_path, conn, 100.0), {"a": [AgentSignal("BTC-USD", 1, 0.9)]})
    assert led._broker("a").get_position("BTC-USD") is not None
    # tick 2: price up to 120, agent says exit -> realises a gain
    led.tick(_ctx(tmp_path, conn, 120.0), {"a": [AgentSignal("BTC-USD", -1, 0.9)]})
    assert led._broker("a").get_position("BTC-USD") is None
    assert led.shadow_equity("a") > 1000.0
    assert conn.execute("SELECT COUNT(*) FROM agent_trades WHERE kind='shadow'").fetchone()[0] == 1


def test_attribution_splits_pnl(tmp_path):
    conn = open_db(tmp_path / "l.db")
    led = _ledger(tmp_path, conn)
    led.attribute({"a": 0.75, "b": 0.25}, pnl=100.0, symbol="BTC-USD", now=time.time())
    rows = {r[0]: r[1] for r in conn.execute(
        "SELECT agent_id, pnl FROM agent_trades WHERE kind='attributed'")}
    assert abs(rows["a"] - 75.0) < 1e-6 and abs(rows["b"] - 25.0) < 1e-6


def test_kill_fires_below_floor_after_min_trades(tmp_path):
    conn = open_db(tmp_path / "l.db")
    led = _ledger(tmp_path, conn, min_trades=2, confirm_ticks=1)
    a = "loser"
    # three losing round trips: buy at 100, sell at 70
    now = 1000.0
    for i in range(3):
        led.tick(_ctx(tmp_path, conn, 100.0, now=now), {a: [AgentSignal("BTC-USD", 1, 0.9)]})
        now += 60
        led.tick(_ctx(tmp_path, conn, 70.0, now=now), {a: [AgentSignal("BTC-USD", -1, 0.9)]})
        now += 60
    assert led.shadow_equity(a) < 850.0
    ctx = _ctx(tmp_path, conn, 70.0, now=now + 120)
    killed = led.check_kills(ctx)
    assert killed and killed[0][0] == a
    assert led.is_dead(a)


def test_kill_waits_for_min_trades(tmp_path):
    conn = open_db(tmp_path / "l.db")
    led = _ledger(tmp_path, conn, min_trades=10, confirm_ticks=1)
    a = "fresh"
    now = 1000.0
    for _ in range(3):
        led.tick(_ctx(tmp_path, conn, 100.0, now=now), {a: [AgentSignal("BTC-USD", 1, 0.9)]})
        led.tick(_ctx(tmp_path, conn, 30.0, now=now + 60), {a: [AgentSignal("BTC-USD", -1, 0.9)]})
        now += 120
    assert led.shadow_equity(a) < 850.0          # well below the floor
    assert led.trade_count(a) < 10
    assert led.check_kills(_ctx(tmp_path, conn, 30.0, now=now)) == []   # but < min_trades
    assert not led.is_dead(a)


def test_revive_resets_agent(tmp_path):
    conn = open_db(tmp_path / "l.db")
    led = _ledger(tmp_path, conn)
    led._switch("x").kill("test")
    led._brokers["x"] = led._broker("x")
    assert led.is_dead("x")
    led.revive("x")
    assert not led.is_dead("x")
    assert "x" not in led._brokers
