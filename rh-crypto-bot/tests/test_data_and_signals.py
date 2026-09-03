from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from botcore.config import SignalParams
from botcore.data.base import Timeframe, asset_class, bars_per_year, clean_bars, combine
from botcore.strategy.signals import get_signal_fn


# -- data.base ----------------------------------------------------------
@pytest.mark.parametrize("sym,klass", [
    ("BTC-USD", "crypto"), ("ETH/USD", "crypto"), ("SOL-USDC", "crypto"),
    ("AAPL", "equity"), ("QQQ", "equity"), ("BRK.B", "equity"),
])
def test_asset_class(sym, klass):
    assert asset_class(sym) == klass


def test_timeframe_parse_and_bars_per_year():
    assert str(Timeframe.parse("1Day")) == "1Day"
    assert Timeframe.parse("15m").unit == "Min"
    assert bars_per_year(Timeframe.parse("1Day"), "crypto") == pytest.approx(365)
    assert bars_per_year(Timeframe.parse("1Day"), "equity") == pytest.approx(252)
    assert bars_per_year(Timeframe.parse("1Hour"), "crypto") == pytest.approx(365 * 24)


def test_clean_bars_normalises():
    raw = pd.DataFrame(
        {"Open": [1, 2], "High": [2, 3], "Low": [0.5, 1], "Close": [1.5, 2.5], "Volume": [10, 20]},
        index=pd.to_datetime(["2021-01-02", "2021-01-01"]),
    )
    out = clean_bars(raw)
    assert list(out.columns) == ["open", "high", "low", "close", "volume"]
    assert out.index.is_monotonic_increasing
    assert str(out.index.tz) == "UTC"


def test_combine_builds_field_symbol_columns():
    idx = pd.date_range("2021-01-01", periods=3, freq="D", tz="UTC")
    a = pd.DataFrame({"open": 1.0, "high": 1, "low": 1, "close": 1, "volume": 1}, index=idx)
    wide = combine({"A": a, "B": a})
    assert ("close", "A") in wide.columns and ("close", "B") in wide.columns


# -- load_history cache recency --------------------------------------
def _seed_candles(db_path, sym, interval, newest_epoch, n=300, step=900):
    from botcore.store.db import open_db, upsert_candles
    conn = open_db(db_path)
    upsert_candles(conn, [
        (sym, interval, newest_epoch - i * step, 100.0, 101.0, 99.0, 100.0, 1.0)
        for i in range(n)
    ])
    conn.close()


def _patch_no_alpaca_and_coinbase(monkeypatch):
    """Route crypto through fetch_coinbase and count the calls."""
    import time as _t
    from botcore.data import history as H
    from botcore.data.base import clean_bars
    monkeypatch.setattr("botcore.config.Settings.has_alpaca_credentials", lambda self: False)
    calls = []

    def fake_coinbase(sym, tf, start, end):
        calls.append(sym)
        idx = pd.date_range(end=pd.Timestamp(_t.time(), unit="s", tz="UTC"),
                            periods=80, freq="15min")
        return clean_bars(pd.DataFrame(
            {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0}, index=idx))

    monkeypatch.setattr(H, "fetch_coinbase", fake_coinbase)
    return calls


def test_load_history_refetches_stale_cache(tmp_path, monkeypatch):
    """A full-but-stale candle cache must trigger a re-fetch, not be served forever."""
    import time as _t
    from botcore.data.history import load_history
    db = str(tmp_path / "c.db")
    _seed_candles(db, "BTC-USD", "15Min", newest_epoch=_t.time() - 4 * 3600)  # 4h stale
    calls = _patch_no_alpaca_and_coinbase(monkeypatch)

    load_history(["BTC-USD"], "15Min", days=40, db_path=db)
    assert calls == ["BTC-USD"], "stale cache tip should force a re-fetch"


def test_load_history_serves_fresh_cache(tmp_path, monkeypatch):
    """A cache whose newest bar is recent is served without any fetch."""
    import time as _t
    from botcore.data.history import load_history
    db = str(tmp_path / "c.db")
    _seed_candles(db, "BTC-USD", "15Min", newest_epoch=_t.time() - 300)  # 5 min old
    calls = _patch_no_alpaca_and_coinbase(monkeypatch)

    load_history(["BTC-USD"], "15Min", days=40, db_path=db)
    assert calls == [], "fresh cache should be served with no fetch"


# -- signals ----------------------------------------------------------
def _bars(path):
    idx = pd.date_range("2020-01-01", periods=len(path), freq="D", tz="UTC")
    close = pd.Series(path, index=idx)
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99,
                         "close": close, "volume": 1.0})


def test_trend_signal_fires_long_in_uptrend_not_downtrend():
    fn = get_signal_fn("trend", SignalParams())
    up = fn(_bars(np.linspace(100, 200, 300)))
    down = fn(_bars(np.linspace(200, 100, 300)))
    assert up["hold"].iloc[-1] and up["entry"].any()
    assert not down["hold"].iloc[-1]
    assert down["exit"].iloc[-1]


def test_signal_output_contract():
    fn = get_signal_fn("mean_reversion", SignalParams())
    out = fn(_bars(100 + 10 * np.sin(np.linspace(0, 20, 400))))
    assert set(out.columns) == {"entry", "hold", "exit", "score", "atr", "close"}
    assert out[["entry", "hold", "exit"]].dtypes.apply(lambda d: d == bool).all()
    assert np.isfinite(out["score"]).all()


def test_signals_have_no_lookahead():
    """Signal at bar t must not change when future bars are appended."""
    fn = get_signal_fn("trend", SignalParams())
    path = np.linspace(100, 180, 320) + np.random.default_rng(1).normal(0, 2, 320)
    full = fn(_bars(path))
    truncated = fn(_bars(path[:250]))
    common = truncated.index
    pd.testing.assert_series_equal(
        full.loc[common, "entry"], truncated["entry"], check_names=False
    )
