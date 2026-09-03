-- Schema for rh-crypto-bot. Applied idempotently by store.db.init_db().
-- All timestamps are unix seconds (REAL) in UTC.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Runtime control flags set by the dashboard / operator (e.g. paused=1).
CREATE TABLE IF NOT EXISTS bot_flags (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_ts REAL NOT NULL
);

-- Rolling OHLCV candles built from polled quotes and/or backfilled history.
CREATE TABLE IF NOT EXISTS candles (
    symbol     TEXT NOT NULL,
    interval   TEXT NOT NULL,          -- e.g. '15m', '60m'
    ts         REAL NOT NULL,          -- candle open time
    open       REAL NOT NULL,
    high       REAL NOT NULL,
    low        REAL NOT NULL,
    close      REAL NOT NULL,
    volume     REAL NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT 'rh',
    PRIMARY KEY (symbol, interval, ts)
);

-- Raw quote snapshots (audit + candle construction).
CREATE TABLE IF NOT EXISTS quotes (
    symbol TEXT NOT NULL,
    ts     REAL NOT NULL,
    bid    REAL NOT NULL,
    ask    REAL NOT NULL,
    mid    REAL NOT NULL,
    PRIMARY KEY (symbol, ts)
);

-- Orders the bot created (paper or live).
CREATE TABLE IF NOT EXISTS orders_local (
    id               TEXT PRIMARY KEY,     -- Robinhood order id (paper: uuid)
    client_order_id  TEXT NOT NULL UNIQUE,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,        -- buy | sell
    type             TEXT NOT NULL,        -- market | limit | stop_loss | stop_limit
    asset_quantity   REAL,
    limit_price      REAL,
    stop_price       REAL,
    status           TEXT NOT NULL,        -- new | open | filled | canceled | rejected
    filled_quantity  REAL DEFAULT 0,
    average_price    REAL,
    fee              REAL DEFAULT 0,
    mode             TEXT NOT NULL,        -- paper | live
    strategy         TEXT,
    role             TEXT,                 -- entry | exit | flatten
    created_ts       REAL NOT NULL,
    updated_ts       REAL NOT NULL,
    raw              TEXT                  -- JSON blob of last API payload
);
CREATE INDEX IF NOT EXISTS idx_orders_symbol_ts ON orders_local (symbol, created_ts);

-- Round-trip trades (entry -> exit), realized P&L.
CREATE TABLE IF NOT EXISTS trades (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol         TEXT NOT NULL,
    qty            REAL NOT NULL,
    entry_price    REAL NOT NULL,
    exit_price     REAL,
    fees           REAL NOT NULL DEFAULT 0,
    pnl            REAL,
    mode           TEXT NOT NULL,
    strategy       TEXT,
    entry_order_id TEXT,
    exit_order_id  TEXT,
    opened_ts      REAL NOT NULL,
    closed_ts      REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_closed_ts ON trades (closed_ts);

-- Current open positions the bot manages.
CREATE TABLE IF NOT EXISTS positions (
    symbol       TEXT NOT NULL,
    mode         TEXT NOT NULL,
    qty          REAL NOT NULL,
    avg_price    REAL NOT NULL,
    stop_price   REAL,
    high_water   REAL,                     -- for trailing stop
    plan_json    TEXT,                     -- serialised ExitPlan
    strategy     TEXT,
    entry_reason TEXT,
    opened_ts    REAL NOT NULL,
    updated_ts   REAL NOT NULL,
    PRIMARY KEY (symbol, mode)
);

-- Periodic equity snapshots for the equity curve.
CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts               REAL NOT NULL,
    mode             TEXT NOT NULL,
    cash_usd         REAL NOT NULL,
    positions_value  REAL NOT NULL,
    equity           REAL NOT NULL,
    peak_equity      REAL,
    PRIMARY KEY (ts, mode)
);

-- LLM risk-governor decisions.
CREATE TABLE IF NOT EXISTS llm_decisions (
    ts              REAL PRIMARY KEY,
    risk_state      TEXT NOT NULL,         -- normal | reduced | risk_off
    allowed_symbols TEXT NOT NULL,         -- JSON array
    size_multiplier REAL NOT NULL,
    rationale       TEXT,
    raw             TEXT,
    ok              INTEGER NOT NULL DEFAULT 1  -- 0 = fell back to conservative
);

-- Append-only audit / event log.
CREATE TABLE IF NOT EXISTS events (
    ts         REAL NOT NULL,
    level      TEXT NOT NULL,              -- info | warn | error | halt
    kind       TEXT NOT NULL,
    message    TEXT NOT NULL,
    data       TEXT,
    config_sha TEXT                        -- which config produced this event
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);

-- SimBroker persistence so the paper book survives a restart (Phase 5).
CREATE TABLE IF NOT EXISTS sim_broker_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    cash         REAL NOT NULL,
    realized_pnl REAL NOT NULL DEFAULT 0,
    total_fees   REAL NOT NULL DEFAULT 0,
    updated_ts   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sim_positions (
    symbol       TEXT PRIMARY KEY,
    qty          REAL NOT NULL,
    avg_price    REAL NOT NULL,
    market_price REAL NOT NULL DEFAULT 0,
    updated_ts   REAL NOT NULL
);

-- Multi-agent trading floor (Phase 7).
CREATE TABLE IF NOT EXISTS agent_signals (
    ts         REAL NOT NULL,
    agent_id   TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    direction  INTEGER NOT NULL,          -- -1 | 0 | +1
    conviction REAL NOT NULL,
    reason     TEXT,
    mode       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_signals ON agent_signals (agent_id, ts);

CREATE TABLE IF NOT EXISTS agent_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id    TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    qty         REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price  REAL,
    pnl         REAL,
    fees        REAL NOT NULL DEFAULT 0,
    kind        TEXT NOT NULL,             -- shadow | attributed
    mode        TEXT NOT NULL,
    opened_ts   REAL NOT NULL,
    closed_ts   REAL
);
CREATE INDEX IF NOT EXISTS idx_agent_trades ON agent_trades (agent_id, kind, closed_ts);

CREATE TABLE IF NOT EXISTS agent_equity (
    ts        REAL NOT NULL,
    agent_id  TEXT NOT NULL,
    mode      TEXT NOT NULL,
    equity    REAL NOT NULL,
    PRIMARY KEY (ts, agent_id, mode)
);
