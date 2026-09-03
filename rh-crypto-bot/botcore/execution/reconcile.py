"""Startup reconciliation: the broker is the source of truth, the local DB is a cache.

Run once when the engine boots, before the first tick, so the dashboard and the
risk engine start from what the broker actually holds:

  * broker has a position the DB doesn't  -> adopt it (the per-tick reconcile then
    attaches a protective stop);
  * DB has a position the broker doesn't   -> it closed while we were offline;
    drop the stale row and log it so a human can reconcile the P&L by hand.

Plans are owned by the engine, so this module never touches ``engine.plans`` -
it only squares the ``positions`` table with the broker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List

from botcore.brokers.base import BrokerClient
from botcore.store.db import log_event
from botcore.store.state import delete_position, load_positions, upsert_position

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    adopted: List[str] = field(default_factory=list)   # at broker, added to DB
    dropped: List[str] = field(default_factory=list)   # stale DB rows removed
    matched: List[str] = field(default_factory=list)   # already agreed

    @property
    def clean(self) -> bool:
        return not self.adopted and not self.dropped

    def as_dict(self) -> dict:
        return {"adopted": self.adopted, "dropped": self.dropped, "matched": self.matched}


def startup_reconcile(conn, broker: BrokerClient, mode: str) -> ReconcileReport:
    """Square the ``positions`` table for ``mode`` with the broker's holdings."""
    report = ReconcileReport()
    try:
        broker_pos = {p.symbol: p for p in broker.get_positions()}
    except Exception as exc:  # noqa: BLE001 - never let a flaky broker block boot
        log.warning("startup reconcile skipped: broker.get_positions failed: %s", exc)
        log_event(conn, "warn", "reconcile", f"startup skipped: {exc}")
        return report

    db_pos = load_positions(conn, mode)

    for sym in db_pos:
        if sym not in broker_pos:
            delete_position(conn, mode, sym)
            report.dropped.append(sym)
            log_event(conn, "warn", "reconcile",
                      f"{sym}: in DB but not at broker on boot; dropped stale row "
                      f"(qty={db_pos[sym].get('qty')}) - check P&L manually")

    for sym, pos in broker_pos.items():
        if sym in db_pos:
            report.matched.append(sym)
            continue
        upsert_position(conn, mode, pos, plan=None, strategy="", entry_reason="adopted-on-boot")
        report.adopted.append(sym)
        log_event(conn, "warn", "reconcile",
                  f"{sym}: held at broker but not in DB on boot; adopted "
                  f"(qty={pos.qty:g} @ {pos.avg_price:.6g}) - protective stop attached next tick")

    if report.clean:
        log_event(conn, "info", "reconcile",
                  f"startup reconcile clean: {len(report.matched)} position(s) agree")
    return report
