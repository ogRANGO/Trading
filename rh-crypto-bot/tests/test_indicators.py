from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from botcore.strategy import indicators as ind


def test_ema_matches_recursive_definition():
    data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    out = ind.ema(pd.Series(data), 3)
    alpha = 2.0 / (3.0 + 1.0)
    assert out.iloc[:2].isna().all()  # min_periods masks the first 2
    y = data[0]
    for i, x in enumerate(data):
        y = x if i == 0 else alpha * x + (1 - alpha) * y
        if i >= 2:
            assert out.iloc[i] == pytest.approx(y)


def test_rsi_all_gains_is_100_all_losses_is_0():
    up = pd.Series(np.arange(1, 40), dtype=float)
    down = pd.Series(np.arange(40, 1, -1), dtype=float)
    assert ind.rsi(up, 14).dropna().iloc[-1] == pytest.approx(100.0)
    assert ind.rsi(down, 14).dropna().iloc[-1] == pytest.approx(0.0)


def test_rsi_known_wilder_value():
    # Classic Wilder example series (first 15 closes) -> RSI ~ 70.46
    closes = pd.Series([
        44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
        45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
    ])
    assert ind.rsi(closes, 14).iloc[-1] == pytest.approx(70.46, abs=0.5)


def test_atr_is_positive_and_tracks_range():
    n = 50
    high = pd.Series(np.linspace(10, 20, n)) + 0.5
    low = pd.Series(np.linspace(10, 20, n)) - 0.5
    close = pd.Series(np.linspace(10, 20, n))
    a = ind.atr(high, low, close, 14).dropna()
    assert (a > 0).all()
    assert a.iloc[-1] == pytest.approx(1.0, abs=0.3)


def test_macd_crossover_sign():
    close = pd.Series(list(np.linspace(1, 2, 40)) + list(np.linspace(2, 1, 40)))
    m = ind.macd(close, 12, 26, 9)
    assert m["macd"].iloc[35] > 0      # uptrend
    assert m["macd"].iloc[-1] < 0      # downtrend
    assert set(m.columns) == {"macd", "signal", "hist"}


def test_bollinger_pctb_bounds_and_bandwidth():
    rng = np.random.default_rng(0)
    close = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    b = ind.bollinger(close, 20, 2.0).dropna()
    assert (b["upper"] > b["lower"]).all()
    # most points sit inside a 2-sigma rolling band (looser than the iid 95%
    # rule because a random walk with rolling stats is not iid-normal)
    inside = ((close.loc[b.index] <= b["upper"]) & (close.loc[b.index] >= b["lower"])).mean()
    assert inside > 0.82
    assert ((b["pctb"] >= -0.2) & (b["pctb"] <= 1.2)).mean() > 0.9


def test_adx_high_in_strong_trend_low_in_chop():
    n = 120
    trend = pd.Series(np.linspace(10, 40, n))
    chop = pd.Series(10 + np.tile([0, 1, 0, -1], n // 4)[:n])
    a_trend = ind.adx(trend + 0.2, trend - 0.2, trend, 14)["adx"].dropna().iloc[-1]
    a_chop = ind.adx(chop + 0.2, chop - 0.2, chop, 14)["adx"].dropna().iloc[-1]
    assert a_trend > 40
    assert a_chop < a_trend


def test_zscore_zero_mean_unit_std_on_window():
    s = pd.Series(np.arange(100, dtype=float))
    z = ind.rolling_zscore(s, 20).dropna()
    assert abs(z.mean()) < 3
    assert z.iloc[-1] == pytest.approx(ind.rolling_zscore(s, 20).iloc[-1])


def test_indicators_do_not_mutate_input():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    snapshot = s.copy()
    ind.ema(s, 2); ind.rsi(s, 2); ind.macd(s); ind.rolling_zscore(s, 2)
    pd.testing.assert_series_equal(s, snapshot)
