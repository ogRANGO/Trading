from __future__ import annotations

import pandas as pd
import pytest

from botcore.config import ExitCfg, PortfolioCfg
from botcore.strategy.portfolio import PortfolioManager


def _cfg(**kw):
    base = dict(max_positions=2, risk_fraction=0.01, max_position_weight=0.5, min_notional_usd=1.0,
                exit=ExitCfg(hard_stop_atr_mult=2.0, target_atr_mult=0, trail_atr_mult=0, time_stop_bars=0))
    base.update(kw)
    return PortfolioCfg(**base)


def _row(entry=False, exit=False, score=1.0, atr=2.0, close=100.0):
    return pd.Series({"entry": entry, "hold": entry, "exit": exit,
                      "score": score, "atr": atr, "close": close})


def test_size_uses_risk_over_stop_distance():
    pm = PortfolioManager(_cfg())
    # risk = 100_000 * 0.01 = 1000 ; stop distance = 2 * atr(2) = 4 ; qty = 250
    # weight cap = 0.5 * 100_000 / 100 = 500 -> qty capped at min(250, 500) = 250
    qty = pm.size("AAPL", ref_price=100.0, atr=2.0, equity=100_000.0)
    assert qty == pytest.approx(250.0, rel=1e-6)


def test_size_respects_weight_cap_and_max_trade_usd():
    pm = PortfolioManager(_cfg(max_position_weight=0.1), max_trade_usd=500.0)
    qty = pm.size("AAPL", 100.0, 2.0, 100_000.0)
    assert qty * 100.0 <= 500.0 + 1e-6


def test_plan_ranks_by_score_and_fills_slots():
    pm = PortfolioManager(_cfg(max_positions=2))
    signals = {
        "A": _row(entry=True, score=0.5),
        "B": _row(entry=True, score=3.0),
        "C": _row(entry=True, score=1.0),
    }
    d = pm.plan(signals=signals, holdings={}, equity=100_000.0)
    assert [e.symbol for e in d.entries] == ["B", "C"]


def test_plan_emits_signal_exits_and_frees_slots():
    pm = PortfolioManager(_cfg(max_positions=2))
    signals = {
        "HELD": _row(exit=True),
        "NEW": _row(entry=True, score=2.0),
    }
    d = pm.plan(signals=signals, holdings={"HELD": 10.0}, equity=100_000.0)
    assert d.signal_exits == ["HELD"]
    assert [e.symbol for e in d.entries] == ["NEW"]


def test_plan_no_free_slots():
    pm = PortfolioManager(_cfg(max_positions=1))
    d = pm.plan(signals={"NEW": _row(entry=True, score=1.0)}, holdings={"HELD": 1.0}, equity=1e5)
    assert d.entries == [] and "no free slots" in " ".join(d.notes)


# --------------------------------------------------------------------------- #
# level-based sizing (smc)
# --------------------------------------------------------------------------- #
def test_size_uses_real_stop_distance_when_a_level_is_given():
    """Risk per share is the distance to the actual stop, not N x ATR.

    Sizing an OB stop as if it were 2xATR is how a '1% risk' trade quietly
    becomes a 3% one.
    """
    cfg = PortfolioCfg(risk_fraction=0.01, max_position_weight=1.0, min_notional_usd=0.0,
                       exit=ExitCfg(hard_stop_atr_mult=2.0))
    pm = PortfolioManager(cfg)
    equity, price, atr = 100_000.0, 100.0, 1.0

    atr_qty = pm.size("AAPL", price, atr, equity)             # risk 1000 / (2*1) = 500
    assert atr_qty == pytest.approx(500.0, rel=1e-3)

    # stop 4 away -> 1000/4 = 250 shares, half the size
    lvl_qty = pm.size("AAPL", price, atr, equity, stop=96.0)
    assert lvl_qty == pytest.approx(250.0, rel=1e-3)

    # a tight stop sizes up, and still risks the same dollars
    tight = pm.size("AAPL", price, atr, equity, stop=99.0)
    assert tight == pytest.approx(1000.0, rel=1e-3)
    assert (price - 99.0) * tight == pytest.approx(equity * cfg.risk_fraction, rel=1e-3)


def test_size_ignores_incoherent_stops():
    cfg = PortfolioCfg(risk_fraction=0.01, max_position_weight=1.0, min_notional_usd=0.0,
                       exit=ExitCfg(hard_stop_atr_mult=2.0))
    pm = PortfolioManager(cfg)
    fallback = pm.size("AAPL", 100.0, 1.0, 100_000.0)
    assert pm.size("AAPL", 100.0, 1.0, 100_000.0, stop=105.0) == fallback
    assert pm.size("AAPL", 100.0, 1.0, 100_000.0, stop=0.0) == fallback


def test_plan_carries_levels_onto_the_sized_entry():
    cfg = PortfolioCfg(max_positions=2, risk_fraction=0.01, max_position_weight=1.0,
                       min_notional_usd=0.0, exit=ExitCfg(hard_stop_atr_mult=2.0))
    pm = PortfolioManager(cfg)
    row = pd.Series({"entry": True, "exit": False, "score": 2.0, "atr": 1.0,
                     "close": 100.0, "stop": 97.0, "tp1": 104.0, "target": 110.0})
    d = pm.plan(signals={"NVDA": row}, holdings={}, equity=100_000.0)
    assert len(d.entries) == 1
    e = d.entries[0]
    assert (e.stop, e.tp1, e.target) == (97.0, 104.0, 110.0)
    assert e.qty == pytest.approx(1000.0 / 3.0, rel=1e-3), "sized off the 3-point stop"


def test_plan_treats_nan_levels_as_absent():
    cfg = PortfolioCfg(max_positions=2, risk_fraction=0.01, max_position_weight=1.0,
                       min_notional_usd=0.0, exit=ExitCfg(hard_stop_atr_mult=2.0))
    pm = PortfolioManager(cfg)
    row = pd.Series({"entry": True, "exit": False, "score": 1.0, "atr": 1.0,
                     "close": 100.0, "stop": float("nan"), "tp1": float("nan")})
    e = pm.plan(signals={"NVDA": row}, holdings={}, equity=100_000.0).entries[0]
    assert e.stop is None and e.tp1 is None
    assert e.qty == pytest.approx(500.0, rel=1e-3), "falls back to the ATR multiple"
