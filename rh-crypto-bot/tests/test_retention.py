from __future__ import annotations

import time

from botcore.store.db import log_event, open_db, record_quote
from botcore.store.state import (
    downsample_equity_snapshots,
    prune_events,
    prune_quotes,
    snapshot_equity,
)


def test_prune_events_only_old(tmp_path):
    conn = open_db(tmp_path / "r.db")
    now = time.time()
    log_event(conn, "info", "k", "old", ts=now - 100 * 86400)
    log_event(conn, "info", "k", "recent", ts=now - 1 * 86400)
    assert prune_events(conn, now - 90 * 86400) == 1
    rows = [r["message"] for r in conn.execute("SELECT message FROM events")]
    assert rows == ["recent"]


def test_prune_quotes_only_old(tmp_path):
    conn = open_db(tmp_path / "r.db")
    now = time.time()
    record_quote(conn, "BTC-USD", 1, 2, ts=now - 30 * 86400)
    record_quote(conn, "BTC-USD", 3, 4, ts=now - 1 * 86400)
    assert prune_quotes(conn, now - 7 * 86400) == 1
    assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1


def test_downsample_keeps_one_per_day(tmp_path):
    conn = open_db(tmp_path / "r.db")
    now = time.time()
    day0 = (now - 400 * 86400)
    day0 -= day0 % 86400
    for h in range(48):                       # 2 full days, hourly
        snapshot_equity(conn, "paper", 100.0 + h, 0.0, ts=day0 + h * 3600)
    snapshot_equity(conn, "paper", 999.0, 0.0, ts=now - 3600)   # inside retention window

    dropped = downsample_equity_snapshots(conn, "paper", now - 365 * 86400)
    assert dropped == 46                        # 48 old rows -> 2 kept (one per day)
    remaining_old = conn.execute(
        "SELECT COUNT(*) FROM equity_snapshots WHERE mode='paper' AND ts < ?",
        (now - 365 * 86400,),
    ).fetchone()[0]
    assert remaining_old == 2
    assert conn.execute("SELECT COUNT(*) FROM equity_snapshots").fetchone()[0] == 3
