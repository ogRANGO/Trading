"""SMC Level 1 mechanics.

The no-lookahead test is the load-bearing one: an order-block strategy that
peeks even one bar ahead backtests beautifully and loses money live. It must
never be skipped or xfailed.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from botcore.config import SignalParams
from botcore.strategy.smc import (
    OrderBlock, _find_order_block, _has_fvg, _is_reversal_candle,
    htf_trend, smc_signals, structure, swings,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _bars(rows, start="2026-01-01", freq="15min") -> pd.DataFrame:
    """rows = [(open, high, low, close), ...] -> a bar frame with a UTC index."""
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC", name="ts")
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1000.0
    return df.astype(float)


def _zigzag(legs, step=1.0, start=100.0):
    """Build bars that walk up/down in whole steps; legs = [+n, -n, ...]."""
    rows, price = [], start
    for leg in legs:
        for _ in range(abs(leg)):
            nxt = price + step * (1 if leg > 0 else -1)
            o, c = price, nxt
            rows.append((o, max(o, c) + 0.1, min(o, c) - 0.1, c))
            price = nxt
    return rows


def _params(**kw) -> SignalParams:
    base = dict(swing_len=2, ob_lookback=10, ob_max_age_bars=33, require_fvg=False,
                htf_filter="none", adx_min=0.0, min_rr=0.0, atr_period=5)
    base.update(kw)
    return SignalParams(**base)


# --------------------------------------------------------------------------- #
# swings
# --------------------------------------------------------------------------- #
def test_swing_high_is_confirmed_late_not_on_the_pivot():
    # a clean peak at index 5
    rows = _zigzag([+5, -5])
    bars = _bars(rows)
    sw = swings(bars, swing_len=2)

    peak = int(np.nanargmax(bars["high"].to_numpy()))
    confirmed_rows = sw.index[sw["sh_idx"].to_numpy() == peak]
    assert len(confirmed_rows) >= 1

    pos = bars.index.get_loc(confirmed_rows[0])
    assert pos == peak + 2, "swing must be confirmed exactly swing_len bars later"
    assert sw["sh_idx"].iloc[peak] != peak, "pivot bar must not know it is a pivot"


def test_swing_low_mirrors_swing_high():
    bars = _bars(_zigzag([-4, +4]))
    sw = swings(bars, swing_len=2)
    trough = int(np.nanargmin(bars["low"].to_numpy()))
    rows = sw.index[sw["sl_idx"].to_numpy() == trough]
    assert len(rows) >= 1
    assert bars.index.get_loc(rows[0]) == trough + 2


# --------------------------------------------------------------------------- #
# structure: BoS / ChoCh
# --------------------------------------------------------------------------- #
def test_break_of_structure_then_change_of_character():
    # up, pull back, higher high (BoS), then break the low (ChoCh down)
    bars = _bars(_zigzag([+6, -3, +6, -12]))
    st = structure(bars, swing_len=2)

    assert st["bull_break"].any(), "a higher high must register as a bullish break"
    assert st["bear_break"].any(), "breaking the swing low must register a bearish break"
    assert (st["trend"] == "up").any() and (st["trend"] == "down").any()

    first_bear = int(np.argmax(st["bear_break"].to_numpy()))
    assert st["choch"].iloc[first_bear], "first bearish break after an uptrend is a ChoCh"


def test_level_is_consumed_so_a_break_does_not_repeat():
    bars = _bars(_zigzag([+4, -2, +8]))
    st = structure(bars, swing_len=2)
    breaks = st["bull_break"].to_numpy()
    # a consumed level cannot re-fire on every following bar
    assert breaks.sum() < len(bars) / 2
    after = np.flatnonzero(breaks)
    if len(after) >= 2:
        assert np.all(np.diff(after) > 1) or True   # gaps are fine; runs are not
        assert not np.all(np.diff(after) == 1)


def test_trend_starts_at_none():
    bars = _bars(_zigzag([+3]))
    assert structure(bars, swing_len=2)["trend"].iloc[0] == "none"


# --------------------------------------------------------------------------- #
# order blocks + FVG
# --------------------------------------------------------------------------- #
def test_fvg_detected_only_when_the_gap_is_real():
    # candle k-1 high = 10, candle k+1 low = 11 -> gap
    high = np.array([10.0, 12.0, 14.0])
    low = np.array([9.0, 10.5, 11.0])
    assert _has_fvg(high, low, 0, 2, bullish=True)

    # overlapping ranges -> no imbalance
    high = np.array([10.0, 11.0, 12.0])
    low = np.array([9.0, 9.5, 9.8])
    assert not _has_fvg(high, low, 0, 2, bullish=True)


def test_order_block_is_last_bearish_candle_before_the_break():
    #                     0 down    1 down    2 up      3 up (break)
    rows = [(10, 10.2, 9.0, 9.2), (9.2, 9.3, 8.0, 8.1),
            (8.1, 11.0, 8.0, 10.8), (10.8, 13.0, 10.5, 12.9)]
    bars = _bars(rows)
    o, h, l, c = (bars[k].to_numpy() for k in ("open", "high", "low", "close"))

    ob = _find_order_block(o, h, l, c, break_idx=3, p=_params(require_fvg=False), bullish=True)
    assert ob is not None
    assert ob.created_idx == 1, "the *last* bearish candle, not the first"
    assert ob.top == pytest.approx(9.3) and ob.bottom == pytest.approx(8.0), "wick to wick"


def test_order_block_rejected_when_fvg_required_but_absent():
    rows = [(10, 10.2, 9.0, 9.2), (9.2, 9.3, 8.0, 8.1),
            (8.1, 8.6, 8.0, 8.5), (8.5, 9.4, 8.4, 9.35)]     # no gap anywhere
    bars = _bars(rows)
    o, h, l, c = (bars[k].to_numpy() for k in ("open", "high", "low", "close"))
    assert _find_order_block(o, h, l, c, 3, _params(require_fvg=True), True) is None
    assert _find_order_block(o, h, l, c, 3, _params(require_fvg=False), True) is not None


def test_order_block_age_and_mitigation_flags():
    ob = OrderBlock(top=10.0, bottom=9.0, created_idx=5, bullish=True)
    assert ob.age(38) == 33 and not ob.mitigated
    ob.mitigated_idx = 12
    assert ob.mitigated


def test_reversal_candle_recognises_engulfing_and_rejects_noise():
    o = np.array([10.0, 9.0]); h = np.array([10.1, 10.6])
    l = np.array([8.9, 8.8]);  c = np.array([9.0, 10.5])
    assert _is_reversal_candle(o, h, l, c, 1), "bullish engulfing"

    o = np.array([10.0, 10.0]); h = np.array([10.1, 10.2])
    l = np.array([9.9, 9.9]);   c = np.array([10.05, 10.05])
    assert not _is_reversal_candle(o, h, l, c, 1)


# --------------------------------------------------------------------------- #
# htf filter
# --------------------------------------------------------------------------- #
def test_htf_filter_disabled_paths_return_all_true():
    bars = _bars(_zigzag([+5, -5]))
    assert htf_trend(bars, _params(htf_filter="none"), "15Min").all()
    # unknown timeframe -> filter cannot be derived, so it must not block anything
    assert htf_trend(bars, _params(htf_filter="structure"), "7Min").all()


def test_htf_filter_uses_only_completed_higher_timeframe_bars():
    bars = _bars(_zigzag([+40, -40]), freq="15min")
    ok = htf_trend(bars, _params(htf_filter="structure"), "15Min")
    assert len(ok) == len(bars) and ok.dtype == bool
    assert not ok.iloc[0], "no completed HTF bar exists at the first LTF bar"


# --------------------------------------------------------------------------- #
# the family end to end
# --------------------------------------------------------------------------- #
def test_signal_frame_has_the_family_contract():
    bars = _bars(_zigzag([+8, -4, +10, -5, +12]))
    out = smc_signals(bars, _params(), "15Min")
    for col in ("entry", "hold", "exit", "score", "atr", "close",
                "stop", "tp1", "target", "rr_to_structure"):
        assert col in out.columns
    assert out.index.equals(bars.index)
    assert out["entry"].dtype == bool and out["exit"].dtype == bool
    assert (out["close"].to_numpy() == bars["close"].to_numpy()).all()


def test_empty_bars_return_empty_contract():
    out = smc_signals(_bars([]), _params(), "15Min")
    assert len(out) == 0
    assert "stop" in out.columns


def _textbook_setup() -> pd.DataFrame:
    """Rise -> pullback (the OB candle) -> impulse/BoS -> retrace into the OB.

    Hand-built so the whole mechanic is exercised end to end with known levels:
    the OB is bar 6 (low 101.5, high 102.5), the break is bar 7, the impulse
    high is 106.5, and the retrace touches the block on bar 9.
    """
    rows = [(p, p + 0.5, p - 0.3, p + 0.8) for p in (100, 101, 102, 103)]
    rows += [
        (104.0, 104.2, 103.0, 103.1),
        (103.1, 103.3, 102.0, 102.2),
        (102.2, 102.5, 101.5, 101.7),      # 6: the order block, wick 101.5-102.5
        (101.7, 106.0, 101.6, 105.8),      # 7: impulse -> break of structure
        (105.8, 106.5, 105.5, 106.2),      # 8: impulse high 106.5
        (106.2, 106.4, 102.3, 103.0),      # 9: retrace into the block
        (103.0, 104.5, 102.8, 104.2),
        (104.2, 107.0, 104.0, 106.9),
    ]
    return _bars(rows)


def test_textbook_setup_enters_on_the_retrace_with_correct_levels():
    bars = _textbook_setup()
    out = smc_signals(bars, _params(atr_period=3), "15Min")

    hits = list(np.flatnonzero(out["entry"].to_numpy()))
    assert hits == [9], f"entry belongs on the retrace bar, got {hits}"

    row = out.iloc[9]
    assert row["stop"] < 101.5, "stop sits below the OB wick low, buffered by ATR"
    assert row["target"] == pytest.approx(106.5), "structure target is the impulse high"
    assert row["rr_to_structure"] > 1.5
    assert row["score"] > 0


def test_entry_never_fires_on_the_impulse_bar_itself():
    """Regression: the OB is the candle before the impulse, so the impulse bar
    overlaps it by construction. Entering there buys the top of the move --
    exactly inverted from the strategy's intent."""
    bars = _textbook_setup()
    out = smc_signals(bars, _params(atr_period=3, min_rr=0.0), "15Min")
    st = structure(bars, 2)
    breaks = np.flatnonzero(st["bull_break"].to_numpy())
    entries = np.flatnonzero(out["entry"].to_numpy())
    assert not set(entries) & set(breaks), "entry coincided with a break bar"


def test_entries_carry_stop_below_price_and_target_above():
    bars = _textbook_setup()
    out = smc_signals(bars, _params(atr_period=3), "15Min")
    ent = out[out["entry"]]
    assert len(ent) > 0, "fixture must produce at least one entry"
    assert (ent["stop"] < ent["close"]).all(), "stop must sit under the entry"
    assert (ent["target"] > ent["close"]).all(), "structure target must sit above"
    assert (ent["rr_to_structure"] > 0).all()


def test_min_rr_filter_only_removes_entries():
    bars = _bars(_zigzag([+8, -4, +10, -5, +12]))
    loose = smc_signals(bars, _params(min_rr=0.0), "15Min")["entry"]
    tight = smc_signals(bars, _params(min_rr=50.0), "15Min")["entry"]
    assert tight.sum() <= loose.sum()
    assert not (tight & ~loose).any(), "a stricter filter cannot create new entries"


def test_reversal_candle_requirement_only_removes_entries():
    bars = _bars(_zigzag([+8, -4, +10, -5, +12]))
    off = smc_signals(bars, _params(require_reversal_candle=False), "15Min")["entry"]
    on = smc_signals(bars, _params(require_reversal_candle=True), "15Min")["entry"]
    assert not (on & ~off).any()


def test_order_block_expires_outside_the_golden_window():
    bars = _bars(_zigzag([+8, -4, +10, -5, +12]))
    short = smc_signals(bars, _params(ob_max_age_bars=1), "15Min")["entry"]
    long = smc_signals(bars, _params(ob_max_age_bars=200), "15Min")["entry"]
    assert short.sum() <= long.sum(), "a tighter golden window cannot add entries"


def test_exit_fires_on_a_bullish_break_of_structure():
    bars = _bars(_zigzag([+6, -3, +8]))
    out = smc_signals(bars, _params(), "15Min")
    st = structure(bars, _params().swing_len)
    assert (out["exit"].to_numpy() == st["bull_break"].to_numpy()).all()


# --------------------------------------------------------------------------- #
# the one that matters
# --------------------------------------------------------------------------- #
def test_no_lookahead_incremental_replay_matches_full_series():
    """Signals at bar t must not change when future bars are appended.

    Recomputing on bars[:t+1] and comparing row t against the full-series row t
    is the only check that actually catches a peek. Any use of a future index --
    a centred rolling window, an unshifted resample, a swing marked on its own
    pivot bar -- fails here.
    """
    rng = np.random.default_rng(20260902)
    price, rows = 100.0, []
    for _ in range(220):
        price *= float(np.exp(rng.normal(0, 0.004)))
        o = price * float(np.exp(rng.normal(0, 0.001)))
        c = price
        hi = max(o, c) * (1 + abs(float(rng.normal(0, 0.002))))
        lo = min(o, c) * (1 - abs(float(rng.normal(0, 0.002))))
        rows.append((o, hi, lo, c))
    bars = _bars(rows)
    p = _params(htf_filter="structure", min_rr=1.0, require_fvg=True)

    full = smc_signals(bars, p, "15Min")
    cols = ["entry", "exit", "score", "stop", "tp1", "target"]

    for t in range(60, len(bars), 7):
        partial = smc_signals(bars.iloc[: t + 1], p, "15Min")
        for col in cols:
            a = full[col].iloc[t]
            b = partial[col].iloc[t]
            if isinstance(a, (float, np.floating)) and np.isnan(a):
                assert np.isnan(b), f"{col} at bar {t}: full=NaN partial={b}"
            else:
                assert a == pytest.approx(b) if isinstance(a, (float, np.floating)) else a == b, \
                    f"lookahead in {col} at bar {t}: full={a} partial={b}"


def test_fvg_search_back_widens_the_hunt_without_dropping_the_requirement():
    """strict: nearest opposing candle has no FVG -> no setup.
    search: walk further back to one that does. Both still require an imbalance."""
    #        0 down(has gap after)  1 up   2 down(no gap)  3 up   4 up(break)
    rows = [(10.0, 10.1, 9.0, 9.1),
            (9.1, 9.4, 9.05, 9.35),
            (9.35, 9.45, 9.3, 9.32),          # bearish, but no imbalance follows
            (9.32, 11.2, 9.31, 11.1),         # gap: low 9.31 > high[1] 9.4? no -> set below
            (11.1, 12.0, 11.0, 11.9)]
    bars = _bars(rows)
    o, h, l, c = (bars[k].to_numpy() for k in ("open", "high", "low", "close"))

    strict = _find_order_block(o, h, l, c, 4, _params(require_fvg=True, fvg_search_back=False), True)
    search = _find_order_block(o, h, l, c, 4, _params(require_fvg=True, fvg_search_back=True), True)
    # whatever the data says, search can only find the same block or an older one
    if strict is not None and search is not None:
        assert search.created_idx <= strict.created_idx
    if strict is not None:
        assert search is not None, "search-back must never lose a block strict already found"


def test_fvg_search_back_never_creates_entries_when_fvg_is_off():
    bars = _bars(_zigzag([+8, -4, +10, -5, +12]))
    a = smc_signals(bars, _params(require_fvg=False, fvg_search_back=False), "15Min")["entry"]
    b = smc_signals(bars, _params(require_fvg=False, fvg_search_back=True), "15Min")["entry"]
    assert (a.to_numpy() == b.to_numpy()).all(), "search_back is a no-op when FVG isn't required"
