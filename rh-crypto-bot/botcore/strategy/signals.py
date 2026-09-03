"""Signal families. The backtester decides which one to run live.

All produce, per bar, an aligned DataFrame with columns:

    entry   : bool  - a fresh long setup is present on this bar
    hold    : bool  - conditions still favour holding a long
    exit    : bool  - a discretionary exit signal (stops/targets are separate)
    score   : float - ranking strength for position selection (higher = better)
    atr     : float - ATR at this bar, for stop / size math
    close   : float

The ``smc`` family additionally emits the price levels it trades against --
``stop``, ``tp1``, ``target``, ``rr_to_structure`` (see LEVEL_COLUMNS). The other
families leave those absent, and everything downstream treats absent as "size
and exit off ATR, as before".

Long-only. Signals are computed on closed bars; the engine acts on the next bar.
"""

from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from botcore.config import SignalParams
from botcore.strategy import indicators as ind
from botcore.strategy.smc import smc_signals

SignalFn = Callable[[pd.DataFrame], pd.DataFrame]


def _atr(bars: pd.DataFrame, period: int) -> pd.Series:
    return ind.atr(bars["high"], bars["low"], bars["close"], period)


def trend_signals(bars: pd.DataFrame, p: SignalParams) -> pd.DataFrame:
    close = bars["close"]
    ema_f = ind.ema(close, p.ema_fast)
    ema_s = ind.ema(close, p.ema_slow)
    macd = ind.macd(close, p.macd_fast, p.macd_slow, p.macd_signal)
    adx = ind.adx(bars["high"], bars["low"], close, p.adx_period)["adx"]
    atr = _atr(bars, p.atr_period)

    up = ema_f > ema_s
    macd_up = macd["macd"] > macd["signal"]
    trending = adx >= p.adx_min
    above = close > ema_s

    # Enter on a fresh trend alignment confirmed by MACD + ADX; exit only when the
    # slower EMA relationship flips. Hard/trailing stops (exitplan) do the rest, so
    # the exit isn't whipsawed by every MACD wiggle.
    aligned = up & above
    entry = aligned & macd_up & trending & ~(aligned & macd_up).shift(1, fill_value=False)
    hold = aligned
    exit_ = ~up

    score = ((ema_f - ema_s) / atr).clip(lower=0).fillna(0.0) * (adx / 100.0).fillna(0.0)

    return pd.DataFrame(
        {"entry": entry.fillna(False), "hold": hold.fillna(False),
         "exit": exit_.fillna(False), "score": score, "atr": atr, "close": close}
    )


def mean_reversion_signals(bars: pd.DataFrame, p: SignalParams) -> pd.DataFrame:
    close = bars["close"]
    bb = ind.bollinger(close, p.bb_period, p.bb_std)
    rsi = ind.rsi(close, p.rsi_period)
    z = ind.rolling_zscore(close, p.zscore_period)
    atr = _atr(bars, p.atr_period)
    # HIGH-ACTIVITY OVERRIDE (2026-09-01): regime filter disabled (buy dips in a downtrend),
    # band gate loosened 0.15 -> 0.40, and the "fresh transition" gate dropped so it enters
    # ANY oversold name it isn't already holding (portfolio layer dedups). To restore the
    # real config: trend_ok = regime > regime.shift(10); band <= 0.15;
    # entry = oversold & ~oversold.shift(1, fill_value=False) & trend_ok.fillna(False).
    regime = ind.ema(close, 50)
    trend_ok = pd.Series(True, index=close.index)

    oversold = (bb["pctb"] <= 0.40) & (rsi <= p.rsi_buy)
    entry = oversold & trend_ok
    hold = close < bb["mid"]
    exit_ = (close >= bb["mid"]) | (rsi >= p.rsi_exit)

    score = ((-z).clip(lower=0).fillna(0.0) + (p.rsi_buy - rsi).clip(lower=0) / 20.0).fillna(0.0)

    return pd.DataFrame(
        {"entry": entry.fillna(False), "hold": hold.fillna(False),
         "exit": exit_.fillna(False), "score": score, "atr": atr, "close": close}
    )


_FAMILIES = {
    "trend": trend_signals,
    "mean_reversion": mean_reversion_signals,
    "smc": smc_signals,
}

# Price levels only the SMC family emits. Downstream (sizing, exit plans) reads
# these as "absent" when missing or NaN, so trend/mean_reversion are unaffected.
LEVEL_COLUMNS = ("stop", "tp1", "target", "rr_to_structure")


def get_signal_fn(family: str, params: SignalParams) -> SignalFn:
    try:
        fn = _FAMILIES[family]
    except KeyError:
        raise ValueError(f"unknown signal family {family!r}; have {list(_FAMILIES)}")

    def _fn(bars: pd.DataFrame) -> pd.DataFrame:
        out = fn(bars, params)
        return out.replace([np.inf, -np.inf], np.nan).fillna(
            {"entry": False, "hold": False, "exit": False, "score": 0.0}
        )

    return _fn
