"""SCHEDULED TODAY (economic releases) — ForexFactory weekly JSON.

Free, no key, needs a browser UA. Carries forecast + previous, and an
``actual`` key once released -> the brief computes the surprise.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime

import httpx

from botcore.brief.httpx_util import BROWSER_UA
from botcore.brief.models import ScheduledEvent, SourceResult

log = logging.getLogger(__name__)

# Only the current-week feed exists on this mirror; a Friday run therefore
# can't see the following Monday. Deterministic events (calendar_rules) and
# the earnings look-ahead cover the near-term gap.
_FEEDS = {
    "thisweek": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
}
_FOREIGN_HIGH_ONLY = True   # non-USD: only High impact (central banks, GDP)
_MULT = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}


def fetch(cfg: dict, on: date | None = None) -> SourceResult:
    on = on or date.today()
    raw: list[dict] = []
    notes: list[str] = []
    for name, url in _FEEDS.items():
        try:
            raw.extend(httpx.get(url, headers={"User-Agent": BROWSER_UA}, timeout=15).json())
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{name}: {exc}")

    events = events_from_raw(raw, on)
    ok = bool(raw)
    return SourceResult("econ", ok, "; ".join(notes) if notes else f"{len(events)} today", events)


def events_from_raw(raw: list[dict], on: date) -> list[ScheduledEvent]:
    """Pure: ForexFactory rows -> today's High/Medium US + High foreign events."""
    events: list[ScheduledEvent] = []
    for e in raw:
        when_dt = _parse_dt(e.get("date"))
        if when_dt is None or when_dt.date() != on:
            continue
        country, impact = e.get("country", ""), e.get("impact", "")
        if country == "USD":
            if impact not in ("High", "Medium"):
                continue
        elif impact != "High":
            continue
        ev = ScheduledEvent(
            label=(e.get("title") or "").strip(),
            date=on, kind="econ",
            when=when_dt.strftime("%H:%M ET"),
            entity=None, impact=impact, country=country,
            forecast=_clean(e.get("forecast")),
            previous=_clean(e.get("previous")),
            actual=_clean(e.get("actual")),
        )
        ev.surprise = compute_surprise(ev.actual, ev.forecast)
        events.append(ev)
    events.sort(key=lambda x: x.when)
    return events


# --------------------------------------------------------------------------- #
def parse_econ_number(s: str | None) -> float | None:
    """'3.1%' -> 3.1 ; '7.33M' -> 7_330_000 ; '-23K' -> -23000 ; '205K' -> 205000."""
    if not s:
        return None
    t = s.strip().replace(",", "").replace("%", "")
    m = re.match(r"^(-?\d+(?:\.\d+)?)\s*([KMBT])?$", t, re.I)
    if not m:
        return None
    val = float(m.group(1))
    if m.group(2):
        val *= _MULT[m.group(2).upper()]
    return val


def compute_surprise(actual: str | None, forecast: str | None) -> str | None:
    a, f = parse_econ_number(actual), parse_econ_number(forecast)
    if a is None or f is None:
        return None
    diff = a - f
    if abs(diff) < 1e-9:
        return f"in line ({actual})"
    sign = "+" if diff > 0 else ""
    # keep the diff in the same visual unit as the inputs
    unit = "%" if (forecast and "%" in forecast) else ""
    if unit != "%" and abs(f) >= 1000:
        diff, a_disp = diff / 1000, a / 1000
        return f"{actual} vs {forecast} exp ({sign}{diff:.1f}K)"
    return f"{actual} vs {forecast} exp ({sign}{diff:.2f}{unit})"


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        try:
            return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
