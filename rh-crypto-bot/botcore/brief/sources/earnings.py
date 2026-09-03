"""SCHEDULED — earnings for held + watchlist names.

Finnhub /calendar/earnings (free tier). Returns events for today plus a short
look-ahead so the brief can warn "NVDA reports Thu AMC".
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import httpx

from botcore.brief.httpx_util import UA
from botcore.brief.models import ScheduledEvent, SourceResult

log = logging.getLogger(__name__)
_URL = "https://finnhub.io/api/v1/calendar/earnings"
_HOUR = {"bmo": "BMO", "amc": "AMC", "dmh": "during market", "": "time TBD"}


def fetch(cfg: dict, symbols: list[str], on: date | None = None, lookahead_days: int = 6) -> SourceResult:
    on = on or date.today()
    from botcore.config import get_settings
    key = get_settings().finnhub_key
    if not key:
        return SourceResult("earnings", False, "no FINNHUB_KEY", [])
    if not symbols:
        return SourceResult("earnings", True, "no symbols", [])

    want = {s.upper() for s in symbols}
    try:
        j = httpx.get(_URL, params={
            "from": on.isoformat(), "to": (on + timedelta(days=lookahead_days)).isoformat(),
            "token": key,
        }, headers={"User-Agent": UA}, timeout=15).json()
    except Exception as exc:  # noqa: BLE001
        return SourceResult("earnings", False, str(exc), [])

    events: list[ScheduledEvent] = []
    for row in j.get("earningsCalendar", []) or []:
        sym = str(row.get("symbol", "")).upper()
        if sym not in want:
            continue
        try:
            d = date.fromisoformat(row["date"])
        except (KeyError, ValueError):
            continue
        hour = _HOUR.get((row.get("hour") or "").lower(), row.get("hour") or "")
        est = row.get("epsEstimate")
        when = hour if d == on else f"{d.strftime('%a')} {hour}"
        label = f"{sym} earnings"
        if est is not None:
            label += f" (EPS est {est})"
        events.append(ScheduledEvent(
            label=label, date=d, kind="earnings", when=when, entity=sym,
            impact="High" if d == on else "Medium",
            forecast=str(est) if est is not None else None,
        ))
    events.sort(key=lambda e: (e.date, e.entity or ""))
    today_n = sum(1 for e in events if e.date == on)
    return SourceResult("earnings", True, f"{today_n} today, {len(events)} in window", events)
