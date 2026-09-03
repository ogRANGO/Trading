from __future__ import annotations

import time

from botcore.notify.summary import build_daily_summary
from botcore.store.db import open_db
from botcore.store.state import record_trade, snapshot_equity
from botcore.store.db import log_event


def _seed(conn, now):
    # equity: 24h ago 100_000 -> now 100_500
    snapshot_equity(conn, "paper", 100_000.0, 0.0, ts=now - 86_000)
    snapshot_equity(conn, "paper", 100_500.0, 0.0, ts=now - 60)
    # one trade inside the window, one outside
    record_trade(conn, {"symbol": "BTC-USD", "qty": 1, "entry_price": 100, "exit_price": 110,
                        "fees": 1.0, "pnl": 9.0, "closed_ts": now - 3600, "opened_ts": now - 7200}, "paper")
    record_trade(conn, {"symbol": "ETH-USD", "qty": 1, "entry_price": 50, "exit_price": 40,
                        "fees": 1.0, "pnl": -11.0, "closed_ts": now - 200_000, "opened_ts": now - 210_000}, "paper")
    log_event(conn, "warn", "reconcile", "drift", ts=now - 100)
    log_event(conn, "error", "tick", "boom", ts=now - 100)
    log_event(conn, "info", "engine", "noise", ts=now - 100)


def test_build_daily_summary_windows_correctly(tmp_path):
    conn = open_db(tmp_path / "s.db")
    now = time.time()
    _seed(conn, now)
    s = build_daily_summary(conn, "paper", now=now)
    assert s.trades_24h == 1
    assert s.realized_pnl_24h == 9.0          # stale -11 trade excluded
    assert round(s.equity_delta_24h, 2) == 500.0
    assert s.equity_now == 100_500.0
    assert s.warn_24h == 1 and s.error_24h == 1 and s.halt_24h == 0


def test_daily_summary_as_ntfy_shape(tmp_path):
    conn = open_db(tmp_path / "s.db")
    now = time.time()
    _seed(conn, now)
    title, msg, tags = build_daily_summary(conn, "paper", now=now).as_ntfy()
    assert "Daily summary" in title
    assert isinstance(msg, str) and msg
    assert isinstance(tags, list) and tags


def test_daily_summary_empty_db(tmp_path):
    conn = open_db(tmp_path / "s.db")
    s = build_daily_summary(conn, "paper")
    assert s.trades_24h == 0 and s.equity_now == 0.0
