from __future__ import annotations

from datetime import datetime, timezone

import pytest

from botcore.brokers.base import Account, OrderRequest, Position, Quote
from botcore.config import RiskCfg, Settings
from botcore.risk.guards import PDTGuard, is_market_open, next_market_open
from botcore.risk.killswitch import DeadSwitch, KillSwitch
from botcore.risk.limits import RiskEngine, deposit_floor_breached


# -- guards ------------------------------------------------------------------
def test_crypto_always_open():
    assert is_market_open("crypto", datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc))


def test_equity_hours_and_weekend():
    wed_open = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)   # 11:00 ET
    wed_night = datetime(2026, 8, 26, 23, 0, tzinfo=timezone.utc)  # 19:00 ET
    sat = datetime(2026, 8, 29, 15, 0, tzinfo=timezone.utc)
    assert is_market_open("equity", wed_open)
    assert not is_market_open("equity", wed_night)
    assert not is_market_open("equity", sat)
    assert next_market_open("equity", sat).weekday() == 0  # Monday


def test_pdt_guard_blocks_fourth_day_trade_when_small():
    g = PDTGuard(min_equity=25_000, max_day_trades=3)
    now = datetime(2026, 8, 26, tzinfo=timezone.utc)
    for _ in range(3):
        g.record_day_trade(now)
    assert g.can_day_trade(equity=10_000, now=now) is False
    assert g.can_day_trade(equity=30_000, now=now) is True  # big account exempt


# -- kill switch -----------------------------------------------------------
def test_kill_switch_roundtrip(tmp_path):
    ks = KillSwitch(tmp_path / "HALT")
    assert not ks.engaged
    ks.engage("manual stop", source="test")
    assert ks.engaged and ks.info()["reason"] == "manual stop"
    ks.clear()
    assert not ks.engaged


def test_dead_switch_roundtrip(tmp_path):
    ds = DeadSwitch(tmp_path / "DEAD")
    assert not ds.dead and ds.certificate() is None
    ds.kill("equity $94k <= floor $95k", equity=94000.0, initial_equity=100000.0)
    assert ds.dead
    cert = ds.certificate()
    assert cert["reason"].startswith("equity") and cert["data"]["equity"] == 94000.0
    assert cert["source"] == "engine"
    ds.revive()
    assert not ds.dead


@pytest.mark.parametrize("equity,init,pct,expected", [
    (100_000, 100_000, 0.0, True),      # exactly at deposit -> breach
    (100_001, 100_000, 0.0, False),
    (95_000, 100_000, -0.05, True),     # at the 5% floor
    (95_001, 100_000, -0.05, False),
    (90_000, 100_000, -0.05, True),
    (50_000, 0.0, -0.05, False),        # anchor not set -> never
])
def test_deposit_floor_breached(equity, init, pct, expected):
    assert deposit_floor_breached(equity, init, pct) is expected


# -- risk engine ---------------------------------------------------------
def _engine(tmp_path, **settings_kw):
    s = Settings(_env_file=None, db_path=str(tmp_path / "bot.db"), **settings_kw)
    cfg = RiskCfg(max_concurrent_positions=2, max_position_weight=0.5,
                  max_total_exposure_pct=0.9, max_orders_per_hour=5)
    return RiskEngine(cfg, s, kill_switch=KillSwitch(tmp_path / "HALT"))


def _quote(sym="BTC-USD", px=100.0):
    return Quote(sym, bid=px * 0.999, ask=px * 1.001, ts=__import__("time").time())


def test_halt_blocks_all_buys(tmp_path):
    eng = _engine(tmp_path)
    eng.halt("drawdown")
    d = eng.pretrade_check(
        OrderRequest("BTC-USD", "buy", 1),
        account=Account(10_000, 10_000, 10_000), positions=[], quote=_quote(),
    )
    assert d.blocked and "halted" in d.reason


def test_live_mode_requires_live_max_usd(tmp_path):
    eng = _engine(tmp_path, bot_mode="live", live_max_usd=0, max_trade_usd=0)
    d = eng.pretrade_check(
        OrderRequest("BTC-USD", "buy", 1),
        account=Account(10_000, 10_000, 10_000), positions=[], quote=_quote(),
    )
    assert d.blocked and "LIVE_MAX_USD" in d.reason


def test_max_trade_usd_shrinks_order(tmp_path):
    eng = _engine(tmp_path, max_trade_usd=250.0)
    d = eng.pretrade_check(
        OrderRequest("BTC-USD", "buy", 10),  # 10 * ~100 = ~1000 notional
        account=Account(100_000, 100_000, 100_000), positions=[], quote=_quote(px=100.0),
    )
    assert d.allowed and d.adjusted_qty is not None
    assert d.adjusted_qty * 100.0 <= 250.0 + 1e-6


def test_max_positions_blocks_new_symbol(tmp_path):
    eng = _engine(tmp_path)
    held = [Position("ETH-USD", 1, 100, 100), Position("SOL-USD", 1, 100, 100)]
    d = eng.pretrade_check(
        OrderRequest("BTC-USD", "buy", 0.01),
        account=Account(100_000, 100_000, 100_000), positions=held, quote=_quote(),
    )
    assert d.blocked and "max positions" in d.reason


def test_wide_spread_blocked(tmp_path):
    eng = _engine(tmp_path)
    bad = Quote("BTC-USD", bid=90, ask=110, ts=__import__("time").time())
    d = eng.pretrade_check(
        OrderRequest("BTC-USD", "buy", 0.01),
        account=Account(100_000, 100_000, 100_000), positions=[], quote=bad,
    )
    assert d.blocked and "spread" in d.reason


def test_order_rate_cap(tmp_path):
    eng = _engine(tmp_path)
    for _ in range(5):
        eng.on_order_submitted()
    d = eng.pretrade_check(
        OrderRequest("BTC-USD", "buy", 0.01),
        account=Account(100_000, 100_000, 100_000), positions=[], quote=_quote(),
    )
    assert d.blocked and "rate cap" in d.reason


def test_drawdown_triggers_halt(tmp_path):
    eng = _engine(tmp_path)
    eng.cfg.max_drawdown_pct = 0.20
    eng.update_equity(100_000)
    assert eng.update_equity(85_000) is None       # -15%, ok
    reason = eng.update_equity(79_000)             # -21%
    assert reason and eng.state.halted


def test_consecutive_losses_start_cooldown(tmp_path):
    eng = _engine(tmp_path)
    eng.cfg.consecutive_loss_limit = 3
    for _ in range(3):
        eng.on_trade_closed(-50.0)
    d = eng.pretrade_check(
        OrderRequest("BTC-USD", "buy", 0.01),
        account=Account(100_000, 100_000, 100_000), positions=[], quote=_quote(),
    )
    assert d.blocked and "cooldown" in d.reason


def test_sells_pass_even_when_halted(tmp_path):
    eng = _engine(tmp_path)
    eng.halt("whatever")
    d = eng.pretrade_check(
        OrderRequest("BTC-USD", "sell", 1),
        account=Account(0, 0, 0), positions=[Position("BTC-USD", 1, 100, 100)], quote=_quote(),
    )
    # exits reduce risk -> never blocked; the engine handles FLATTEN separately
    assert d.allowed
