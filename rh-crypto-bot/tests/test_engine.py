from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

from botcore.brokers.base import Quote
from botcore.brokers.sim import SimBroker
from botcore.config import Settings, load_bot_config
from botcore.data.quotes import QuoteFeed
from botcore.engine.loop import TradingEngine
from botcore.execution.router import Execution
from botcore.store.db import open_db
from botcore.store.state import load_positions, recent_trades, set_flag
from botcore.strategy.portfolio import SizedEntry


class FakeFeed(QuoteFeed):
    name = "fake"

    def __init__(self, prices):
        self.prices = dict(prices)

    def set(self, sym, px):
        self.prices[sym] = px

    def get_quotes(self, symbols):
        out = {}
        for s in symbols:
            px = self.prices.get(s)
            if px:
                out[s] = Quote(s, bid=px * 0.999, ask=px * 1.001, ts=time.time())
        return out


def _bars(sym, path):
    idx = pd.date_range("2024-01-01", periods=len(path), freq="D", tz="UTC")
    c = pd.Series(path, index=idx)
    return {sym: pd.DataFrame({"open": c, "high": c * 1.02, "low": c * 0.98, "close": c, "volume": 1.0})}


def _engine(tmp_path, prices, bars, **sk):
    s = Settings(_env_file=None, bot_mode="paper", broker="sim",
                 db_path=str(tmp_path / "bot.db"), paper_start_equity=100_000.0, max_trade_usd=0, **sk)
    cfg = load_bot_config()
    cfg.strategy.signal_family = "trend"   # hermetic: don't inherit config.yaml's live family
    feed = FakeFeed(prices)
    execu = Execution(SimBroker(100_000.0, cfg.fees), feed, "paper", needs_price_feed=True)
    eng = TradingEngine(s, cfg, execution=execu, bars=bars)
    return eng, feed


def test_tick_writes_equity_snapshot_and_quotes(tmp_path):
    eng, _ = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.full(300, 100.0)))
    out = eng.tick()
    assert out["entries"] == [] and out["exits"] == []
    conn = open_db(eng.settings.db_path)
    assert conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM quotes WHERE symbol='BTC-USD'").fetchone()[0] == 1


def test_forced_entry_persists_position_and_plan(tmp_path):
    eng, feed = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.linspace(70, 100, 300)))
    eng.tick()
    q = feed.get_quote("BTC-USD")
    eng.execution.broker.mark({"BTC-USD": {"open": 100, "high": 100, "low": 100, "close": 100}})
    e = SizedEntry("BTC-USD", qty=100.0, ref_price=100.0, atr=4.0, score=1.0, risk_dollars=1000.0, reason="test")
    res = eng._enter(e, q, eng.execution.broker.get_account(), [])
    assert res["ok"]
    rows = load_positions(open_db(eng.settings.db_path), "paper")
    assert "BTC-USD" in rows and rows["BTC-USD"]["plan_json"]


def test_hard_stop_exit_records_losing_trade(tmp_path):
    eng, feed = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.linspace(70, 100, 300)))
    eng.tick()
    q = feed.get_quote("BTC-USD")
    eng.execution.broker.mark({"BTC-USD": {"open": 100, "high": 100, "low": 100, "close": 100}})
    eng._enter(SizedEntry("BTC-USD", 100.0, 100.0, 4.0, 1.0, 1000.0, "test"),
               q, eng.execution.broker.get_account(), [])
    # plan hard stop = 100 - 4*4 = 84 ; drop price under it
    feed.set("BTC-USD", 80.0)
    out = eng.tick()
    assert any(x["reason"] == "hard_stop" for x in out["exits"])
    trades = recent_trades(open_db(eng.settings.db_path), 5)
    assert trades and trades[0]["pnl"] < 0
    assert not eng.execution.broker.get_positions()


def test_pause_flag_blocks_trading(tmp_path):
    eng, _ = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.linspace(70, 100, 300)))
    set_flag(eng.conn, "paused", "1")
    assert eng.tick().get("paused") is True


def test_flatten_request_closes_all_and_halts(tmp_path):
    eng, feed = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.linspace(70, 100, 300)))
    eng.tick()
    q = feed.get_quote("BTC-USD")
    eng.execution.broker.mark({"BTC-USD": {"open": 100, "high": 100, "low": 100, "close": 100}})
    eng._enter(SizedEntry("BTC-USD", 50.0, 100.0, 4.0, 1.0, 1000.0, "test"),
               q, eng.execution.broker.get_account(), [])
    set_flag(eng.conn, "flatten_requested", "1")
    out = eng.tick()
    assert out.get("flattened") is True
    assert not eng.execution.broker.get_positions()
    assert eng.risk.kill.engaged


def test_scheduler_tick_runs_from_worker_thread(tmp_path):
    # regression: self.conn is created on the main thread but the APScheduler
    # tick runs on a worker thread -> must not raise "SQLite objects created in
    # a thread can only be used in that same thread".
    eng, _ = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.full(300, 100.0)))
    eng.start()
    try:
        deadline = time.time() + 10
        conn = open_db(eng.settings.db_path)
        while time.time() < deadline:
            if conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] >= 1:
                break
            time.sleep(0.2)
        assert conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] >= 1
        errs = conn.execute("SELECT message FROM events WHERE level='error'").fetchall()
        assert not errs, [e[0] for e in errs]
    finally:
        eng.stop()


def test_resume_requested_clears_halt_and_rebaselines(tmp_path):
    eng, _ = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.full(300, 100.0)))
    eng.risk.halt("max drawdown breached: -25%", source="risk")
    assert eng.risk.kill.engaged and eng.risk.state.halted
    eng.risk.state.peak_equity = 200_000.0

    set_flag(eng.conn, "resume_requested", "1")
    eng.tick()
    assert eng.risk.state.halted is False
    assert eng.risk.kill.engaged is False
    assert eng.risk.state.peak_equity != 200_000.0        # re-baselined by update_equity


def test_entry_fee_survives_a_restart(tmp_path):
    from botcore.config import load_bot_config

    prices = {"BTC-USD": 100.0}
    bars = _bars("BTC-USD", np.linspace(70, 100, 300))
    s = Settings(_env_file=None, bot_mode="paper", broker="sim",
                 db_path=str(tmp_path / "bot.db"), paper_start_equity=100_000.0, max_trade_usd=0)
    cfg = load_bot_config()
    cfg.fees.commission_pct = 0.01                       # force a non-zero entry fee
    feed = FakeFeed(prices)
    execu = Execution(SimBroker(100_000.0, cfg.fees), feed, "paper", needs_price_feed=True)
    eng = TradingEngine(s, cfg, execution=execu, bars=bars)
    eng.tick()
    q = feed.get_quote("BTC-USD")
    eng.execution.broker.mark({"BTC-USD": {"open": 100, "high": 100, "low": 100, "close": 100}})
    eng._enter(SizedEntry("BTC-USD", 100.0, 100.0, 4.0, 1.0, 1000.0, "test"),
               q, eng.execution.broker.get_account(), [])
    fee_in = eng.plans["BTC-USD"].entry_fee
    assert fee_in > 0

    # fresh engine on the same DB -> _load_plans must recover the fee
    eng2 = TradingEngine(s, cfg, execution=Execution(
        eng.execution.broker, feed, "paper", needs_price_feed=True), bars=bars)
    assert eng2._entry_meta["BTC-USD"]["fee"] == pytest.approx(fee_in)


def test_reconcile_attaches_protective_stop_to_orphan_position(tmp_path):
    eng, feed = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.linspace(70, 100, 300)))
    # position created directly on the broker, no plan
    eng.execution.broker.set_price("BTC-USD", 100.0)
    eng.execution.broker.fill_market("BTC-USD", "buy", 10, ref_price=100.0)
    eng.tick()
    assert "BTC-USD" in eng.plans
    assert eng.plans["BTC-USD"].hard_stop < 100.0


# --------------------------------------------------------------------------- #
# Phase 6: permanent kill-on-loss
# --------------------------------------------------------------------------- #
def _acct(eq):
    from botcore.brokers.base import Account
    return Account(cash=eq, equity=eq, buying_power=eq)


def test_initial_equity_anchored_once(tmp_path):
    eng, feed = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.full(300, 100.0)))
    assert eng.initial_equity == 100_000.0
    from botcore.store.state import get_flag
    assert float(get_flag(eng.conn, "initial_equity")) == 100_000.0

    # a fresh engine on the same DB keeps the anchor even if the broker is now poorer
    s = Settings(_env_file=None, bot_mode="paper", broker="sim",
                 db_path=str(tmp_path / "bot.db"), paper_start_equity=100_000.0, max_trade_usd=0)
    cfg = load_bot_config()
    broker = SimBroker(50_000.0, cfg.fees)
    eng2 = TradingEngine(s, cfg, execution=Execution(broker, feed, "paper", needs_price_feed=True),
                         bars=_bars("BTC-USD", np.full(300, 100.0)))
    assert eng2.initial_equity == 100_000.0


class _Exit(Exception):
    pass


def _no_exit(code):
    raise _Exit(code)


def test_kill_floor_fires_after_confirm_ticks(tmp_path, monkeypatch):
    import botcore.engine.loop as loop_mod

    monkeypatch.setattr(loop_mod.os, "_exit", _no_exit)
    monkeypatch.setattr(loop_mod, "_disable_launchd_agents", lambda: None)

    eng, feed = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.full(300, 100.0)))
    eng.cfg.risk.kill_below_deposit = True
    eng.cfg.risk.kill_floor_confirm_ticks = 2
    eng.cfg.risk.kill_floor_pct = -0.05
    # anchor is 100k; report 90k (below the 95k floor)
    monkeypatch.setattr(eng.execution.broker, "get_account", lambda: _acct(90_000.0))

    out1 = eng.tick()
    assert out1.get("killed") is None and eng._kill_floor_strikes == 1
    assert not eng.dead.dead

    with pytest.raises(_Exit):
        eng.tick()
    assert eng.dead.dead
    from botcore.store.state import get_flag
    assert get_flag(eng.conn, "killed") == "1"
    cert = eng.dead.certificate()
    assert "deposit floor" in cert["reason"]


def test_kill_floor_resets_on_recovery(tmp_path, monkeypatch):
    eng, feed = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.full(300, 100.0)))
    eng.cfg.risk.kill_below_deposit = True
    eng.cfg.risk.kill_floor_confirm_ticks = 3
    eng.cfg.risk.kill_floor_pct = -0.05

    monkeypatch.setattr(eng.execution.broker, "get_account", lambda: _acct(90_000.0))
    eng.tick()
    assert eng._kill_floor_strikes == 1
    monkeypatch.setattr(eng.execution.broker, "get_account", lambda: _acct(101_000.0))
    eng.tick()
    assert eng._kill_floor_strikes == 0
    assert not eng.dead.dead


def test_kill_disabled_never_kills(tmp_path, monkeypatch):
    eng, feed = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.full(300, 100.0)))
    eng.cfg.risk.kill_below_deposit = False
    eng.cfg.risk.kill_floor_confirm_ticks = 1
    monkeypatch.setattr(eng.execution.broker, "get_account", lambda: _acct(10_000.0))
    eng.tick()
    eng.tick()
    assert not eng.dead.dead and eng._kill_floor_strikes == 0


def test_kill_floor_skipped_when_market_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("ACTIVE_UNIVERSE", "tech_equity")
    import botcore.engine.loop as loop_mod
    monkeypatch.setattr(loop_mod, "is_market_open", lambda *a, **k: False)

    s = Settings(_env_file=None, bot_mode="paper", broker="sim",
                 db_path=str(tmp_path / "bot.db"), paper_start_equity=100_000.0, max_trade_usd=0)
    cfg = load_bot_config()
    assert cfg.active_universe == "tech_equity"
    feed = FakeFeed({"AAPL": 100.0})
    broker = SimBroker(100_000.0, cfg.fees)
    eng = TradingEngine(s, cfg, execution=Execution(broker, feed, "paper", needs_price_feed=True),
                        bars=_bars("AAPL", np.full(300, 100.0)))
    eng.cfg.risk.kill_floor_confirm_ticks = 1
    monkeypatch.setattr(broker, "get_account", lambda: _acct(50_000.0))

    out = eng.tick()
    assert out.get("market_closed") is True
    assert not eng.dead.dead


# --------------------------------------------------------------------------- #
# Phase 7: multi-agent engine mode
# --------------------------------------------------------------------------- #
def test_multi_mode_tick_runs_the_coordinator(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "multi")
    eng, feed = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.linspace(60, 100, 300)))
    assert eng.coordinator is not None
    assert [a.id for a in eng.coordinator.agents]  # technical agents built
    eng.coordinator.agents = [a for a in eng.coordinator.agents if a.kind == "technical"]
    out = eng.tick()
    assert "entries" in out and "exits" in out
    conn = open_db(eng.settings.db_path)
    # every enabled agent recorded a signal or an equity snapshot this tick
    assert conn.execute("SELECT COUNT(*) FROM agent_equity").fetchone()[0] > 0


def test_multi_mode_agent_kill_does_not_exit_process(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_MODE", "multi")
    import botcore.engine.loop as loop_mod
    monkeypatch.setattr(loop_mod.os, "_exit", _no_exit)   # would raise if the bot tried to die

    eng, feed = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.full(300, 100.0)))
    eng.coordinator.agents = [a for a in eng.coordinator.agents if a.kind == "technical"]
    led = eng.coordinator.ledger
    led.cfg.min_trades = 1
    led.cfg.confirm_ticks = 1
    # blow up one agent's shadow book directly
    aid = eng.coordinator.agents[0].id
    b = led._broker(aid)
    b.set_price("BTC-USD", 100.0)
    b.fill_market("BTC-USD", "buy", 8.0, ref_price=100.0)
    b.fill_market("BTC-USD", "sell", 8.0, ref_price=10.0)   # -$720
    led._trades[aid] = 5

    from botcore.agents.base import AgentContext
    ctx = AgentContext(bars=eng._bars, quotes=feed.get_quotes(["BTC-USD"]), positions={},
                       equity=100_000.0, universe=["BTC-USD"], now=time.time(),
                       conn=eng.conn, settings=eng.settings, klass="crypto")
    killed = led.check_kills(ctx)
    assert killed and killed[0][0] == aid
    assert led.is_dead(aid)
    # the engine keeps ticking fine with the survivors
    eng.tick()


def test_single_mode_unchanged(tmp_path):
    eng, _ = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.full(300, 100.0)))
    assert eng.coordinator is None
    assert eng.tick()["entries"] == []


# --------------------------------------------------------------------------- #
# partial TP1 in the live engine
# --------------------------------------------------------------------------- #
def _mark(eng, px, sym="BTC-USD"):
    """Give the SimBroker a price. tick() does this; these tests call _enter directly."""
    eng.execution.broker.mark({sym: {"open": px, "high": px, "low": px, "close": px}},
                              clock=time.time())

def test_partial_exit_books_a_slice_and_keeps_managing_the_rest(tmp_path):
    """TP1 sells part of the position; the plan and the remainder must survive.

    This is the live-side twin of the backtester's partial test -- if the two
    disagree, every backtested TP1 number is fiction.
    """
    from botcore.config import ExitCfg
    from botcore.strategy.exitplan import build_plan

    eng, feed = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.full(300, 100.0)))
    eng.exit_cfg = ExitCfg(hard_stop_atr_mult=4.0, target_atr_mult=0.0, trail_atr_mult=0.0,
                           time_stop_bars=0, tp1_fraction=0.25, be_after_tp1=True)
    _mark(eng, 100.0)

    # open a position by hand, with a level-based plan carrying a TP1
    e = SizedEntry(symbol="BTC-USD", qty=100.0, ref_price=100.0, atr=2.0, score=1.0,
                   risk_dollars=1000.0, reason="test", stop=95.0, tp1=110.0, target=130.0)
    q = feed.get_quotes(["BTC-USD"])["BTC-USD"]
    acct = eng.execution.broker.get_account()
    res = eng._enter(e, q, acct, [])
    assert res["ok"], res

    plan = eng.plans["BTC-USD"]
    assert plan.hard_stop == 95.0 and plan.tp1 == 110.0 and plan.tp1_fraction == 0.25

    # price trades through TP1 -> partial sell
    feed.set("BTC-USD", 112.0)
    _mark(eng, 112.0)
    q = feed.get_quotes(["BTC-USD"])["BTC-USD"]
    pos = eng.execution.broker.get_position("BTC-USD")
    opened_qty = pos.qty
    eng._exit("BTC-USD", pos, q, "tp1", eng.execution.broker.get_account(),
              [pos], qty=plan.tp1_fraction * opened_qty)

    still = eng.execution.broker.get_position("BTC-USD")
    assert still is not None, "the remainder must still be held"
    assert still.qty == pytest.approx(0.75 * opened_qty, rel=1e-6)

    plan = eng.plans["BTC-USD"]
    assert plan.tp1_done, "tp1 must not be able to fire twice"
    assert plan.hard_stop == pytest.approx(plan.entry_price), "stop lifted to breakeven"
    assert eng._entry_meta["BTC-USD"]["qty"] == pytest.approx(0.75 * opened_qty, rel=1e-6)

    conn = open_db(eng.settings.db_path)
    rows = conn.execute("SELECT symbol, qty FROM trades WHERE mode='paper'").fetchall()
    assert len(rows) == 1 and rows[0][1] == pytest.approx(0.25 * opened_qty, rel=1e-6)

    # the position row must reflect the reduced size, not the original
    held = load_positions(conn, "paper")["BTC-USD"]
    assert held["qty"] == pytest.approx(0.75 * opened_qty, rel=1e-6)


def test_full_exit_after_a_partial_closes_everything(tmp_path):
    from botcore.config import ExitCfg

    eng, feed = _engine(tmp_path, {"BTC-USD": 100.0}, _bars("BTC-USD", np.full(300, 100.0)))
    eng.exit_cfg = ExitCfg(hard_stop_atr_mult=4.0, tp1_fraction=0.25, be_after_tp1=False)
    _mark(eng, 100.0)
    e = SizedEntry(symbol="BTC-USD", qty=100.0, ref_price=100.0, atr=2.0, score=1.0,
                   risk_dollars=1000.0, reason="test", stop=95.0, tp1=110.0)
    q = feed.get_quotes(["BTC-USD"])["BTC-USD"]
    eng._enter(e, q, eng.execution.broker.get_account(), [])

    pos = eng.execution.broker.get_position("BTC-USD")
    eng._exit("BTC-USD", pos, q, "tp1", eng.execution.broker.get_account(), [pos], qty=25.0)
    pos = eng.execution.broker.get_position("BTC-USD")
    eng._exit("BTC-USD", pos, q, "signal_exit", eng.execution.broker.get_account(), [pos])

    assert eng.execution.broker.get_position("BTC-USD") is None
    assert "BTC-USD" not in eng.plans and "BTC-USD" not in eng._entry_meta
    conn = open_db(eng.settings.db_path)
    qtys = [r[0] for r in conn.execute("SELECT qty FROM trades WHERE mode='paper' ORDER BY id")]
    assert len(qtys) == 2
    assert sum(qtys) == pytest.approx(100.0, rel=1e-6), "slices must sum to the position"
