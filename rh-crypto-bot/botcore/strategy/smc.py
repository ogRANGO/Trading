"""Smart-Money-Concepts (SMC) Level 1, made mechanical.

The discretionary rules this encodes, and where each one lives:

  * trend = market structure: a close through the last confirmed swing high is a
    Break of Structure (BoS) with the trend, or a Change of Character (ChoCh)
    against it.                                          -> ``structure()``
  * order block = the last opposing candle before the impulse that broke
    structure, taken wick to wick, and only if an imbalance (FVG) follows it.
                                                         -> ``order_blocks()``
  * entry on the first retrace into an unmitigated OB inside the "golden
    window" of ``ob_max_age_bars``; stop under the OB wick; TP1 at the first
    opposing OB overhead; full exit when structure breaks again.
                                                         -> ``smc_signals()``

Everything is one forward pass over the bars. A swing is only *confirmed*
``swing_len`` bars after it prints, and nothing reads an index above the
current one, so the no-lookahead property holds by construction rather than by
convention -- ``tests/test_smc.py`` asserts it against an incremental replay.

Long-only, like the other families. Bearish order blocks are still tracked,
because TP1 needs the first one overhead.

Output is the standard signal frame (entry/hold/exit/score/atr/close) plus the
price levels this family works in: ``stop``, ``tp1``, ``target`` and the
diagnostic ``rr_to_structure``. The other families emit NaN for those.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from botcore.config import SignalParams
from botcore.strategy import indicators as ind

# 15Min bars are read against 1Hour structure, 1Hour against 1Day. Anything not
# listed falls back to "no HTF filter" rather than guessing a rule.
_HTF_FOR = {
    "1Min": "15Min", "5Min": "30Min", "15Min": "1Hour", "30Min": "2Hour",
    "1Hour": "1Day", "2Hour": "1Day", "4Hour": "1Day", "1Day": "1Week",
}
_PANDAS_RULE = {
    "15Min": "15min", "30Min": "30min", "1Hour": "1h", "2Hour": "2h",
    "4Hour": "4h", "1Day": "1D", "1Week": "1W",
}


class OrderBlock:
    """A price zone left behind by the candle that preceded an impulse."""

    __slots__ = ("top", "bottom", "created_idx", "break_idx", "bullish", "mitigated_idx")

    def __init__(self, top: float, bottom: float, created_idx: int, bullish: bool,
                 break_idx: Optional[int] = None) -> None:
        self.top = top
        self.bottom = bottom
        self.created_idx = created_idx
        # Index of the bar whose close broke structure. The impulse leaves from
        # the block, so it overlaps it by construction -- entries are only legal
        # on a later retrace, never on the impulse itself.
        self.break_idx = created_idx if break_idx is None else break_idx
        self.bullish = bullish
        self.mitigated_idx: Optional[int] = None

    @property
    def mitigated(self) -> bool:
        return self.mitigated_idx is not None

    def age(self, i: int) -> int:
        """Bars since the setup became valid, i.e. since the structure break.

        The golden window is the retest deadline, and the clock only starts once
        there is something to retest -- measuring from the OB candle instead
        would spend part of the window on the impulse.
        """
        return i - self.break_idx

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        kind = "bull" if self.bullish else "bear"
        return f"<OB {kind} [{self.bottom:.4f}, {self.top:.4f}] @{self.created_idx}>"


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #
def swings(bars: pd.DataFrame, swing_len: int) -> pd.DataFrame:
    """Fractal swing points, marked on the bar where they are *confirmed*.

    A bar j is a swing high when its high is the maximum of the ``swing_len``
    bars either side. That cannot be known until bar ``j + swing_len``, so the
    output carries both the pivot's own index (``sh_idx``) and the price
    (``sh_price``) on the confirmation row -- never on row j itself, which is
    the shape of lookahead bias this family is most prone to.
    """
    n = len(bars)
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    L = max(1, int(swing_len))

    sh_idx = np.full(n, -1, dtype=int)
    sl_idx = np.full(n, -1, dtype=int)
    sh_price = np.full(n, np.nan, dtype=float)
    sl_price = np.full(n, np.nan, dtype=float)

    for i in range(2 * L, n):
        j = i - L                                  # candidate pivot, now confirmable
        window = slice(j - L, j + L + 1)
        if high[j] >= high[window].max():
            sh_idx[i] = j
            sh_price[i] = high[j]
        if low[j] <= low[window].min():
            sl_idx[i] = j
            sl_price[i] = low[j]

    return pd.DataFrame(
        {"sh_idx": sh_idx, "sl_idx": sl_idx, "sh_price": sh_price, "sl_price": sl_price},
        index=bars.index,
    )


def structure(bars: pd.DataFrame, swing_len: int) -> pd.DataFrame:
    """Running BoS / ChoCh state machine over closes.

    A level is *consumed* once broken: ``last_sh`` is cleared after a bullish
    break and only refills when a new swing high is confirmed. Without that a
    single break would re-fire on every subsequent bar and ``exit`` would be
    permanently true.
    """
    sw = swings(bars, swing_len)
    close = bars["close"].to_numpy(dtype=float)
    n = len(bars)

    sh_idx_a = sw["sh_idx"].to_numpy()
    sl_idx_a = sw["sl_idx"].to_numpy()
    sh_price_a = sw["sh_price"].to_numpy()
    sl_price_a = sw["sl_price"].to_numpy()

    trend = np.array(["none"] * n, dtype=object)
    bull_break = np.zeros(n, dtype=bool)     # bullish BoS or ChoCh on this bar
    bear_break = np.zeros(n, dtype=bool)
    is_choch = np.zeros(n, dtype=bool)
    last_sh_out = np.full(n, np.nan, dtype=float)
    last_sl_out = np.full(n, np.nan, dtype=float)

    cur = "none"
    last_sh = np.nan
    last_sl = np.nan

    for i in range(n):
        # 1. absorb any swing confirmed as of this bar
        if sh_idx_a[i] >= 0:
            last_sh = sh_price_a[i]
        if sl_idx_a[i] >= 0:
            last_sl = sl_price_a[i]

        # 2. structure break on the close
        if not np.isnan(last_sh) and close[i] > last_sh:
            bull_break[i] = True
            is_choch[i] = cur == "down"
            cur = "up"
            last_sh = np.nan                 # consumed
        elif not np.isnan(last_sl) and close[i] < last_sl:
            bear_break[i] = True
            is_choch[i] = cur == "up"
            cur = "down"
            last_sl = np.nan

        trend[i] = cur
        last_sh_out[i] = last_sh
        last_sl_out[i] = last_sl

    return pd.DataFrame(
        {"trend": trend, "bull_break": bull_break, "bear_break": bear_break,
         "choch": is_choch, "last_sh": last_sh_out, "last_sl": last_sl_out},
        index=bars.index,
    )


# --------------------------------------------------------------------------- #
# order blocks
# --------------------------------------------------------------------------- #
def _has_fvg(high: np.ndarray, low: np.ndarray, start: int, end: int, bullish: bool) -> bool:
    """Is there a 3-candle imbalance strictly between ``start`` and ``end``?

    Bullish gap: the low of candle k+1 sits above the high of candle k-1, so the
    middle candle's range never traded against them.
    """
    for k in range(start + 1, end):
        if k - 1 < 0 or k + 1 >= len(high):
            continue
        if bullish and low[k + 1] > high[k - 1]:
            return True
        if not bullish and high[k + 1] < low[k - 1]:
            return True
    return False


def _find_order_block(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
    break_idx: int, p: SignalParams, bullish: bool,
) -> Optional[OrderBlock]:
    """Last opposing candle before the impulse that broke structure.

    ``fvg_search_back`` picks between the two defensible readings of the rule
    when that candle carries no imbalance: strict (no imbalance, no setup) or
    search (walk further back for one that does). On real bars the choice moves
    the setup count by ~3x, so it is a swept parameter, not a house style.
    """
    lo = max(0, break_idx - int(p.ob_lookback))
    for j in range(break_idx - 1, lo - 1, -1):
        opposing = close[j] < open_[j] if bullish else close[j] > open_[j]
        if not opposing:
            continue
        if p.require_fvg and not _has_fvg(high, low, j, break_idx, bullish):
            if p.fvg_search_back:
                continue                     # keep hunting further back
            return None                      # nearest OB has no imbalance -> no setup
        return OrderBlock(top=high[j], bottom=low[j], created_idx=j,
                          bullish=bullish, break_idx=break_idx)
    return None


def _is_reversal_candle(open_: np.ndarray, high: np.ndarray, low: np.ndarray,
                        close: np.ndarray, i: int) -> bool:
    """Level-2 confirmation: bullish engulfing or hammer."""
    if i < 1:
        return False
    body = abs(close[i] - open_[i])
    rng = high[i] - low[i]
    if rng <= 0:
        return False
    engulfing = (
        close[i] > open_[i]
        and close[i - 1] < open_[i - 1]
        and close[i] >= open_[i - 1]
        and open_[i] <= close[i - 1]
    )
    lower_wick = min(open_[i], close[i]) - low[i]
    hammer = body > 0 and lower_wick >= 2 * body and (high[i] - max(open_[i], close[i])) <= body
    return bool(engulfing or hammer)


# --------------------------------------------------------------------------- #
# higher-timeframe filter
# --------------------------------------------------------------------------- #
def htf_trend(bars: pd.DataFrame, p: SignalParams, timeframe: str = "") -> pd.Series:
    """Higher-timeframe bias, forward-filled onto the LTF index.

    Only *completed* HTF bars are used: the resampled series is shifted one bar
    before being reindexed, so the in-progress HTF candle -- whose close the
    live engine cannot know -- never influences an entry.
    """
    idx = bars.index
    if p.htf_filter == "none" or not isinstance(idx, pd.DatetimeIndex) or len(bars) == 0:
        return pd.Series(True, index=idx)

    htf = p.htf or _HTF_FOR.get(timeframe, "")
    rule = _PANDAS_RULE.get(htf)
    if not rule:
        return pd.Series(True, index=idx)     # unknown timeframe -> filter disabled

    agg = bars.resample(rule, label="right", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    if len(agg) < 4 * max(1, int(p.swing_len)):
        return pd.Series(True, index=idx)     # not enough HTF history to judge

    if p.htf_filter == "ema":
        ok_htf = agg["close"] > ind.ema(agg["close"], min(50, max(3, len(agg) // 3)))
    else:
        ok_htf = structure(agg, p.swing_len)["trend"] == "up"

    # shift: bar stamped T describes the interval that *ended* at T, so it is
    # only knowable from T onward; shifting once more keeps us strictly behind.
    # Carried as float so the reindex/fillna path stays numeric (a bool Series
    # reindexed onto a longer index becomes object dtype and warns on fillna).
    shifted = ok_htf.shift(1, fill_value=False).astype(float)
    return shifted.reindex(idx, method="ffill").fillna(0.0).astype(bool)


# --------------------------------------------------------------------------- #
# the signal family
# --------------------------------------------------------------------------- #
def smc_signals(bars: pd.DataFrame, p: SignalParams, timeframe: str = "") -> pd.DataFrame:
    """SMC Level 1 entries, stops and targets as an aligned per-bar frame."""
    n = len(bars)
    close_s = bars["close"]
    atr_s = ind.atr(bars["high"], bars["low"], bars["close"], p.atr_period)

    empty = pd.DataFrame(
        {"entry": pd.Series(False, index=bars.index),
         "hold": pd.Series(False, index=bars.index),
         "exit": pd.Series(False, index=bars.index),
         "score": pd.Series(0.0, index=bars.index),
         "atr": atr_s, "close": close_s,
         "stop": pd.Series(np.nan, index=bars.index),
         "tp1": pd.Series(np.nan, index=bars.index),
         "target": pd.Series(np.nan, index=bars.index),
         "rr_to_structure": pd.Series(np.nan, index=bars.index)},
    )
    if n == 0:
        return empty

    open_ = bars["open"].to_numpy(dtype=float)
    high = bars["high"].to_numpy(dtype=float)
    low = bars["low"].to_numpy(dtype=float)
    close = bars["close"].to_numpy(dtype=float)
    atr = atr_s.to_numpy(dtype=float)

    st = structure(bars, p.swing_len)
    trend_a = st["trend"].to_numpy()
    bull_break_a = st["bull_break"].to_numpy()
    bear_break_a = st["bear_break"].to_numpy()

    adx = ind.adx(bars["high"], bars["low"], bars["close"], p.adx_period)["adx"].to_numpy(dtype=float)
    htf_ok = htf_trend(bars, p, timeframe).to_numpy(dtype=bool)

    entry = np.zeros(n, dtype=bool)
    hold = np.zeros(n, dtype=bool)
    exit_ = bull_break_a.copy()          # a fresh BoS is the "new high" full exit
    score = np.zeros(n, dtype=float)
    stop_a = np.full(n, np.nan, dtype=float)
    tp1_a = np.full(n, np.nan, dtype=float)
    target_a = np.full(n, np.nan, dtype=float)
    rr_a = np.full(n, np.nan, dtype=float)

    bull_obs: List[OrderBlock] = []
    bear_obs: List[OrderBlock] = []
    impulse_high = np.nan                # highest high since the last bullish break

    for i in range(n):
        # --- a break creates the order block behind the impulse ------------- #
        if bull_break_a[i]:
            ob = _find_order_block(open_, high, low, close, i, p, bullish=True)
            if ob is not None:
                bull_obs.append(ob)
            impulse_high = high[i]
        elif not np.isnan(impulse_high):
            impulse_high = max(impulse_high, high[i])

        if bear_break_a[i]:
            ob = _find_order_block(open_, high, low, close, i, p, bullish=False)
            if ob is not None:
                bear_obs.append(ob)

        # --- expire anything outside the golden window --------------------- #
        bull_obs = [o for o in bull_obs
                    if not o.mitigated and o.age(i) <= p.ob_max_age_bars]
        bear_obs = [o for o in bear_obs
                    if not o.mitigated and o.age(i) <= p.ob_max_age_bars]

        # --- first touch of the freshest unmitigated bullish OB ------------ #
        touched: Optional[OrderBlock] = None
        for ob in reversed(bull_obs):
            if i <= ob.break_idx:            # the impulse itself is not a retrace
                continue
            if low[i] <= ob.top:
                ob.mitigated_idx = i         # first touch only, mitigated either way
                touched = ob
                break

        in_uptrend = trend_a[i] == "up"
        hold[i] = bool(in_uptrend)

        if touched is None:
            continue

        # price blew straight through the block: mitigated, but not a setup
        if close[i] <= touched.bottom:
            continue

        a = atr[i]
        stop = touched.bottom - (p.stop_buffer_atr * a if np.isfinite(a) else 0.0)
        if not np.isfinite(stop) or close[i] <= stop:
            continue
        risk = close[i] - stop

        # TP1 = bottom of the nearest unmitigated bearish OB overhead, else the
        # impulse high. Structure target is always the impulse high, which is
        # what the BoS exit is really aiming at.
        overhead = [o.bottom for o in bear_obs if o.bottom > close[i]]
        target = impulse_high if np.isfinite(impulse_high) and impulse_high > close[i] else np.nan
        tp1 = min(overhead) if overhead else target
        if not np.isfinite(target):
            continue
        rr = (target - close[i]) / risk

        stop_a[i] = stop
        tp1_a[i] = tp1 if np.isfinite(tp1) else target
        target_a[i] = target
        rr_a[i] = rr

        ok = (
            in_uptrend
            and bool(htf_ok[i])
            and (not np.isfinite(adx[i]) or adx[i] >= p.adx_min)
            and rr >= p.min_rr
        )
        if ok and p.require_reversal_candle:
            ok = _is_reversal_candle(open_, high, low, close, i)
        if not ok:
            continue

        entry[i] = True
        freshness = 1.0 - min(1.0, touched.age(i) / max(1.0, float(p.ob_max_age_bars)))
        score[i] = rr * freshness

    return pd.DataFrame(
        {"entry": entry, "hold": hold, "exit": exit_, "score": score,
         "atr": atr_s, "close": close_s, "stop": stop_a, "tp1": tp1_a,
         "target": target_a, "rr_to_structure": rr_a},
        index=bars.index,
    )
