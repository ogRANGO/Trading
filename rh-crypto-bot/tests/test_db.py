from __future__ import annotations

from botcore.store.db import log_event, open_db, record_equity, record_quote, upsert_candles


def test_init_creates_tables(tmp_path):
    conn = open_db(tmp_path / "bot.db")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("candles", "quotes", "orders_local", "trades", "positions",
              "equity_snapshots", "llm_decisions", "events", "schema_meta",
              "sim_broker_state", "sim_positions",
              "agent_signals", "agent_trades", "agent_equity"):
        assert t in names
    from botcore.store.db import SCHEMA_VERSION
    assert conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == SCHEMA_VERSION


def test_init_is_idempotent(tmp_path):
    path = tmp_path / "bot.db"
    open_db(path).close()
    conn = open_db(path)  # second run must not raise
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_writes_roundtrip(tmp_path):
    conn = open_db(tmp_path / "bot.db")
    log_event(conn, "info", "boot", "hello", {"x": 1}, ts=100.0)
    record_quote(conn, "BTC-USD", 100.0, 102.0, ts=100.0)
    record_equity(conn, "paper", 1000.0, 250.0, ts=100.0)
    n = upsert_candles(conn, [("BTC-USD", "15m", 100.0, 1, 2, 0.5, 1.5, 10)])
    assert n == 1

    assert conn.execute("SELECT mid FROM quotes WHERE symbol='BTC-USD'").fetchone()[0] == 101.0
    assert conn.execute("SELECT equity FROM equity_snapshots").fetchone()[0] == 1250.0
    # upsert on conflict
    upsert_candles(conn, [("BTC-USD", "15m", 100.0, 1, 5, 0.5, 4.0, 20)])
    assert conn.execute("SELECT high, close FROM candles").fetchone()[0] == 5


def test_events_carry_config_sha(tmp_path):
    """Every event row records which config produced it (drift is visible)."""
    from botcore.store.db import set_config_sha, get_config_sha

    conn = open_db(tmp_path / "bot.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    assert "config_sha" in cols

    set_config_sha("deadbeef1234")
    try:
        log_event(conn, "info", "boot", "started")
        row = conn.execute("SELECT config_sha FROM events").fetchone()
        assert row[0] == "deadbeef1234"
        assert get_config_sha() == "deadbeef1234"
    finally:
        set_config_sha("")
