from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from botcore.backtest.engine import run_backtest
from botcore.backtest.metrics import compute_metrics
from botcore.config import ExitCfg, load_bot_config


def _series(path, freq="D", start="2020-01-01"):
    idx = pd.date_range(start, periods=len(path), freq=freq, tz="UTC")
    close = pd.Series(path, index=idx)
    return pd.DataFrame({
        "open": close.shift(1).fillna(close.iloc[0]),
        "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": 1_000.0,
    })


# -- metrics --------------------------------------------------------------
def test_metrics_known_curve():
    eq = pd.Series(
        [100, 110, 121, 108.9, 119.79],
        index=pd.date_range("2021-01-01", periods=5, freq="365D", tz="UTC"),
    )
    m = compute_metrics(eq, [], bars_per_year=1.0)
    assert m["total_return"] == pytest.approx(0.1979, abs=1e-3)
    assert m["max_drawdown"] == pytest.approx(-0.10, abs=1e-6)  # 121 -> 108.9


def test_metrics_trade_stats():
    eq = pd.Series([100, 101], index=pd.date_range("2021-01-01", periods=2, freq="D", tz="UTC"))
    trades = [
        {"pnl": 100.0, "risk_dollars": 50.0, "fees": 1.0, "bars_held": 4},
        {"pnl": -50.0, "risk_dollars": 50.0, "fees": 1.0, "bars_held": 2},
    ]
    m = compute_metrics(eq, trades, 252)
    assert m["win_rate"] == pytest.approx(0.5)
    assert m["profit_factor"] == pytest.approx(2.0)
    assert m["avg_r_multiple"] == pytest.approx((2.0 - 1.0) / 2)
    assert m["total_fees"] == pytest.approx(2.0)


# -- engine --------------------------------------------------------------
def test_backtest_runs_and_conserves_value_on_flat_market():
    cfg = load_bot_config()
    flat = _series(np.full(400, 100.0))
    res = run_backtest({"BTC-USD": flat}, cfg, starting_equity=100_000, signal_family="trend", warmup=150)
    assert res.equity.iloc[-1] == pytest.approx(100_000, rel=1e-9)  # no signals -> no trades
    assert res.trades.empty


def test_backtest_captures_a_clean_uptrend():
    cfg = load_bot_config()
    up = _series(np.linspace(100, 300, 500) + np.sin(np.linspace(0, 30, 500)))
    res = run_backtest({"BTC-USD": up}, cfg, starting_equity=100_000, signal_family="trend", warmup=150)
    assert not res.trades.empty
    assert res.equity.iloc[-1] > 100_000
    # equity series aligned to the data index, no NaNs
    assert res.equity.notna().all()
    assert len(res.equity) == len(up)


def test_backtest_empty_frames_raise():
    cfg = load_bot_config()
    with pytest.raises(ValueError):
        run_backtest({}, cfg)


def test_run_backtest_accepts_precomputed_sigs():
    cfg = load_bot_config()
    up = _series(np.linspace(100, 200, 300))
    sig = pd.DataFrame(index=up.index)
    sig["entry"] = False
    sig.iloc[160, sig.columns.get_loc("entry")] = True
    sig["hold"] = True
    sig["exit"] = False
    sig["score"] = 1.0
    sig["atr"] = up["close"] * 0.02
    sig["close"] = up["close"]
    res = run_backtest({"BTC-USD": up}, cfg, warmup=150, sigs={"BTC-USD": sig})
    assert not res.trades.empty


def test_multi_agent_backtest_blend_shape():
    from botcore.agents.registry import build_agents
    from botcore.backtest.multi import blended_sigs

    cfg = load_bot_config()
    up = _series(np.linspace(100, 250, 260))
    frames = {"BTC-USD": up}
    agents = [a for a in build_agents(cfg) if a.kind == "technical"]
    weights = {a.id: cfg.agents[a.id].weight for a in agents}
    bsig = blended_sigs(agents, weights, cfg.coordinator, frames, ["BTC-USD"], "crypto", warmup=60)
    assert set(bsig["BTC-USD"].columns) == {"entry", "hold", "exit", "score", "atr", "close"}
    assert len(bsig["BTC-USD"]) == len(up)
    res = run_backtest(frames, cfg, warmup=60, sigs=bsig)
    assert res.equity.notna().all()


# --------------------------------------------------------------------------- #
# partial TP1 + the intraday clock, in the backtester
# --------------------------------------------------------------------------- #
def _level_sigs(bars, entry_at, *, stop, tp1, target):
    sig = pd.DataFrame(index=bars.index)
    sig["entry"] = False
    sig.iloc[entry_at, sig.columns.get_loc("entry")] = True
    sig["hold"] = True
    sig["exit"] = False
    sig["score"] = 1.0
    sig["atr"] = bars["close"] * 0.02
    sig["close"] = bars["close"]
    sig["stop"], sig["tp1"], sig["target"] = stop, tp1, target
    return sig


def _cfg_with_smc_exit(**exit_kw):
    cfg = load_bot_config()
    base = dict(hard_stop_atr_mult=4.0, target_atr_mult=0.0, trail_atr_mult=0.0,
                time_stop_bars=0, tp1_fraction=0.25, be_after_tp1=False)
    base.update(exit_kw)
    cfg.strategy.signal_family = "smc"
    cfg.portfolio.exit_profiles["smc"] = ExitCfg(**base)
    return cfg


def test_partial_tp1_books_two_trades_and_conserves_equity():
    """The slice and the remainder must together account for the equity change.

    A partial that double-counts the entry fee, or forgets to shrink the
    remaining quantity, shows up here as a mismatch.
    """
    up = _series(np.linspace(100, 160, 300))
    cfg = _cfg_with_smc_exit()
    # entry fills near 132 (bar 160 of a 100->160 ramp), so tp1 and target must
    # sit above that -- build_plan rightly discards a tp1 below the entry
    sigs = {"BTC-USD": _level_sigs(up, 160, stop=90.0, tp1=138.0, target=150.0)}

    res = run_backtest({"BTC-USD": up}, cfg, warmup=150, sigs=sigs, starting_equity=100_000.0)
    reasons = list(res.trades["reason"])
    assert "tp1" in reasons, f"expected a partial take-profit, got {reasons}"
    assert len(res.trades) >= 2, "TP1 slice and the remainder are separate rows"

    tp1_row = res.trades[res.trades["reason"] == "tp1"].iloc[0]
    rest = res.trades[res.trades["reason"] != "tp1"]
    assert tp1_row["qty"] > 0 and rest["qty"].sum() > 0
    assert tp1_row["qty"] == pytest.approx(0.25 * (tp1_row["qty"] + rest["qty"].sum()), rel=1e-6)

    total_pnl = res.trades["pnl"].sum()
    assert res.equity.iloc[-1] - 100_000.0 == pytest.approx(total_pnl, abs=1.0)


def test_tp1_fires_at_most_once_per_position():
    up = _series(np.linspace(100, 200, 300))
    cfg = _cfg_with_smc_exit()
    sigs = {"BTC-USD": _level_sigs(up, 160, stop=90.0, tp1=145.0, target=190.0)}
    res = run_backtest({"BTC-USD": up}, cfg, warmup=150, sigs=sigs)
    assert (res.trades["reason"] == "tp1").sum() <= 1


def test_no_position_survives_the_flat_by_close_bell():
    """Intraday means intraday: nothing may still be open after flat_by_et."""
    idx = pd.date_range("2026-09-02 13:30", periods=40, freq="15min", tz="UTC")  # 09:30 ET
    close = pd.Series(np.linspace(100, 108, len(idx)), index=idx)
    bars = pd.DataFrame({"open": close, "high": close * 1.002, "low": close * 0.998,
                         "close": close, "volume": 1000.0})
    cfg = _cfg_with_smc_exit(flat_by_et="15:55", tp1_fraction=0.0)
    sigs = {"NVDA": _level_sigs(bars, 2, stop=95.0, tp1=None, target=130.0)}

    res = run_backtest({"NVDA": bars}, cfg, warmup=1, sigs=sigs)
    assert "flat_close" in list(res.trades["reason"]), \
        f"position was not closed at the bell: {list(res.trades['reason'])}"
    closed_at = res.trades[res.trades["reason"] == "flat_close"].iloc[0]["exit_date"]
    assert closed_at.tz_convert("America/New_York").time().strftime("%H:%M") >= "15:55"
