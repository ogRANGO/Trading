"""Hand-rolled technical indicators.

Kept dependency-free (pandas/numpy only) so the math is auditable and unit-tested,
and so we don't depend on ``pandas-ta`` (which breaks under numpy 2.x).

All functions take/return pandas Series or DataFrames indexed by timestamp and do
not mutate their inputs.

Conventions:
  * ``ema`` uses the pandas ``adjust=False`` recursion seeded from the first
    value (no SMA seed). It converges to the ta-lib/TradingView EMA within a few
    multiples of ``period`` bars.
  * ``_wilder`` (used by RSI / ATR / ADX) uses the classic Wilder form: seed with
    the simple mean of the first ``period`` values, then
    ``avg = (avg*(period-1) + x) / period``. This matches published RSI values.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def _wilder(series: pd.Series, period: int) -> pd.Series:
    """SMA-seeded Wilder smoothing. NaNs before the seed are treated as 0 once
    the seed window is complete (matches how RSI handles the leading diff NaN)."""
    arr = series.to_numpy(dtype=float)
    n = arr.shape[0]
    out = np.full(n, np.nan)
    valid = ~np.isnan(arr)

    start = None
    for i in range(period - 1, n):
        if valid[i - period + 1 : i + 1].all():
            start = i
            break
    if start is None:
        return pd.Series(out, index=series.index)

    out[start] = arr[start - period + 1 : start + 1].mean()
    for i in range(start + 1, n):
        x = arr[i] if valid[i] else 0.0
        out[i] = (out[i - 1] * (period - 1) + x) / period
    return pd.Series(out, index=series.index)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = _wilder(gain, period)
    avg_loss = _wilder(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    out[avg_loss == 0.0] = 100.0
    out[(avg_gain == 0.0) & (avg_loss == 0.0)] = 50.0
    return out


def macd(
    close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return _wilder(true_range(high, low, close), period)


def bollinger(
    close: pd.Series, period: int = 20, num_std: float = 2.0
) -> pd.DataFrame:
    mid = sma(close, period)
    sd = close.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + num_std * sd
    lower = mid - num_std * sd
    width = (upper - lower) / mid
    pctb = (close - lower) / (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "width": width, "pctb": pctb})


def rolling_zscore(series: pd.Series, period: int = 20) -> pd.Series:
    mean = series.rolling(period, min_periods=period).mean()
    sd = series.rolling(period, min_periods=period).std(ddof=0)
    return (series - mean) / sd.replace(0.0, np.nan)


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.DataFrame:
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)

    tr = true_range(high, low, close)
    atr_ = _wilder(tr, period)
    plus_di = 100.0 * _wilder(plus_dm, period) / atr_.replace(0.0, np.nan)
    minus_di = 100.0 * _wilder(minus_dm, period) / atr_.replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_ = _wilder(dx, period)
    return pd.DataFrame({"adx": adx_, "plus_di": plus_di, "minus_di": minus_di})


def annualized_vol(close: pd.Series, period: int = 20, periods_per_year: int = 252) -> pd.Series:
    """Rolling stdev of log returns, annualised."""
    logret = np.log(close / close.shift(1))
    return logret.rolling(period, min_periods=period).std(ddof=0) * np.sqrt(periods_per_year)
