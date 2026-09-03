"""brief.db — the measurement log. Written every run from phase 1 so phase 5/6
have data to score. Separate DB from the trading bots; this package never opens
bot.db / bot_crypto.db.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from botcore.store.db import connect   # connection helper only (PRAGMAs); no schema

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS brief_run (
  run_id        TEXT PRIMARY KEY,
  ts_utc        TEXT NOT NULL,
  slot          TEXT NOT NULL,
  session       TEXT NOT NULL,
  session_type  TEXT NOT NULL,
  sources_ok    INTEGER,
  sources_total INTEGER
);
CREATE TABLE IF NOT EXISTS brief_item (
  item_id       TEXT PRIMARY KEY,
  run_id        TEXT NOT NULL,
  lane          TEXT NOT NULL,
  tier          INTEGER NOT NULL,
  tag           TEXT NOT NULL,
  entity        TEXT,
  headline      TEXT,
  source        TEXT,
  published_utc TEXT,
  echo_count    INTEGER DEFAULT 1,
  surfaced      INTEGER NOT NULL,
  score         REAL DEFAULT 0,
  payload       TEXT
);
CREATE TABLE IF NOT EXISTS price_snapshot (
  run_id     TEXT NOT NULL,
  entity     TEXT NOT NULL,
  ref_price  REAL,
  prev_close REAL,
  premarket  REAL,
  PRIMARY KEY (run_id, entity)
);
CREATE TABLE IF NOT EXISTS outcome (
  run_id   TEXT NOT NULL,
  entity   TEXT NOT NULL,
  open_px  REAL,
  close_px REAL,
  ret_oc   REAL,
  ret_cc   REAL,
  PRIMARY KEY (run_id, entity)
);
CREATE TABLE IF NOT EXISTS source_health (
  run_id TEXT NOT NULL,
  name   TEXT NOT NULL,
  ok     INTEGER NOT NULL,
  detail TEXT,
  PRIMARY KEY (run_id, name)
);
CREATE INDEX IF NOT EXISTS ix_item_run   ON brief_item(run_id);
CREATE INDEX IF NOT EXISTS ix_item_lane  ON brief_item(lane);
CREATE INDEX IF NOT EXISTS ix_run_session ON brief_run(session);
"""


def open_brief_db(path: "str | Path"):
    conn = connect(path)
    conn.executescript(SCHEMA)
    return conn


def write_brief(conn, brief) -> None:
    """Persist a fully-assembled Brief (all items, incl. surfaced=False)."""
    conn.execute(
        "INSERT OR REPLACE INTO brief_run VALUES (?,?,?,?,?,?,?)",
        (brief.run_id, brief.generated_at.astimezone(timezone.utc).isoformat(),
         brief.slot, brief.session.isoformat(), brief.session_type,
         sum(1 for s in brief.sources if s.ok), len(brief.sources)),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO source_health VALUES (?,?,?,?)",
        [(brief.run_id, s.name, int(s.ok), s.detail) for s in brief.sources],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO brief_item "
        "(item_id, run_id, lane, tier, tag, entity, headline, source, published_utc, "
        " echo_count, surfaced, score, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (f"{brief.run_id}:{i}", brief.run_id, it.lane, it.tier, it.tag, it.entity,
             it.text, it.source, it.published_utc, it.echo_count, int(it.surfaced),
             it.score, json.dumps(it.payload, default=str))
            for i, it in enumerate(brief.items)
        ],
    )
    conn.executemany(
        "INSERT OR REPLACE INTO price_snapshot VALUES (?,?,?,?,?)",
        [(brief.run_id, ent, d.get("ref_price"), d.get("prev_close"), d.get("premarket"))
         for ent, d in brief.price_snapshot.items()],
    )


def latest_run(conn, session: str):
    row = conn.execute(
        "SELECT * FROM brief_run WHERE session=? ORDER BY ts_utc DESC LIMIT 1", (session,)
    ).fetchone()
    return dict(row) if row else None


def prior_headlines(conn, session: str) -> set[str]:
    """Headlines already surfaced earlier the same session (for NEW SINCE ...)."""
    rows = conn.execute(
        "SELECT DISTINCT headline FROM brief_item bi JOIN brief_run br USING(run_id) "
        "WHERE br.session=? AND bi.lane IN ('headlines','filings') AND bi.surfaced=1",
        (session,),
    ).fetchall()
    return {(r["headline"] or "").strip().lower() for r in rows}
