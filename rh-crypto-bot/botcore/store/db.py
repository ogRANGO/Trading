"""Thin SQLite helper: connection, migrations, and a few common writes."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

MIGRATIONS = Path(__file__).with_name("migrations.sql")
SCHEMA_VERSION = "4"

# Idempotent column additions for DBs created before a given schema bump.
_ALTERS = [
    "ALTER TABLE positions ADD COLUMN plan_json TEXT",
    "ALTER TABLE positions ADD COLUMN strategy TEXT",
    "ALTER TABLE positions ADD COLUMN entry_reason TEXT",
    "ALTER TABLE orders_local ADD COLUMN role TEXT",       # entry | exit | flatten
    "ALTER TABLE equity_snapshots ADD COLUMN peak_equity REAL",
    "ALTER TABLE events ADD COLUMN config_sha TEXT",
]

# Fingerprint of the config the running engine loaded, stamped onto every event
# row by log_event. Set once at boot via set_config_sha; empty in tests and in
# one-off scripts that never load a BotConfig.
_CONFIG_SHA = ""


def set_config_sha(sha: str) -> None:
    """Record which config the process is running, for event provenance."""
    global _CONFIG_SHA
    _CONFIG_SHA = sha or ""


def get_config_sha() -> str:
    return _CONFIG_SHA


def connect(db_path: "str | Path") -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the engine creates the connection on the main
    # thread but the APScheduler tick runs on a worker thread. Safe here because
    # the scheduler uses a single-worker executor (ticks never overlap) and the
    # connection is in autocommit mode (isolation_level=None) with a busy timeout.
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(MIGRATIONS.read_text(encoding="utf-8"))
    for stmt in _ALTERS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SCHEMA_VERSION,),
    )


def open_db(db_path: "str | Path") -> sqlite3.Connection:
    conn = connect(db_path)
    init_db(conn)
    return conn


# --- common writes ------------------------------------------------------- #
def log_event(
    conn: sqlite3.Connection,
    level: str,
    kind: str,
    message: str,
    data: Optional[dict] = None,
    ts: Optional[float] = None,
) -> None:
    conn.execute(
        "INSERT INTO events(ts, level, kind, message, data, config_sha) VALUES(?,?,?,?,?,?)",
        (ts or time.time(), level, kind, message,
         json.dumps(data) if data else None, _CONFIG_SHA or None),
    )


def record_quote(conn: sqlite3.Connection, symbol: str, bid: float, ask: float, ts: Optional[float] = None) -> None:
    ts = ts or time.time()
    conn.execute(
        "INSERT OR REPLACE INTO quotes(symbol, ts, bid, ask, mid) VALUES(?,?,?,?,?)",
        (symbol, ts, bid, ask, (bid + ask) / 2),
    )


def upsert_candles(conn: sqlite3.Connection, rows: Iterable[tuple], source: str = "rh") -> int:
    """rows: (symbol, interval, ts, open, high, low, close, volume)"""
    rows = list(rows)
    conn.executemany(
        "INSERT INTO candles(symbol, interval, ts, open, high, low, close, volume, source) "
        "VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, interval, ts) DO UPDATE SET "
        "open=excluded.open, high=excluded.high, low=excluded.low, "
        "close=excluded.close, volume=excluded.volume",
        [(*r, source) for r in rows],
    )
    return len(rows)


def record_equity(conn: sqlite3.Connection, mode: str, cash_usd: float, positions_value: float, ts: Optional[float] = None) -> None:
    ts = ts or time.time()
    conn.execute(
        "INSERT OR REPLACE INTO equity_snapshots(ts, mode, cash_usd, positions_value, equity) "
        "VALUES(?,?,?,?,?)",
        (ts, mode, cash_usd, positions_value, cash_usd + positions_value),
    )
