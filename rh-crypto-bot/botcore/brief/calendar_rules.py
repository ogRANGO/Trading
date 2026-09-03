"""Trading-session logic + the Friday-derived scheduled events.

Pure date math + the two seed YAMLs. No network.
"""

from __future__ import annotations

import calendar as _cal
from datetime import date, timedelta

from botcore.brief.bconfig import load_econ_calendar, load_market_calendar
from botcore.brief.models import ScheduledEvent

_QUARTERLY_MONTHS = {3, 6, 9, 12}


def _cal_for(d: date):
    return load_market_calendar(d.year)


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def session_type(d: date) -> str:
    """weekend | holiday | half | full"""
    if is_weekend(d):
        return "weekend"
    mc = _cal_for(d)
    if d in mc["holidays"]:
        return "holiday"
    if d in mc["half_days"]:
        return "half"
    return "full"


def is_trading_day(d: date) -> bool:
    return session_type(d) in ("full", "half")


def next_session(d: date) -> date:
    n = d + timedelta(days=1)
    while not is_trading_day(n):
        n += timedelta(days=1)
    return n


def prev_session(d: date) -> date:
    p = d - timedelta(days=1)
    while not is_trading_day(p):
        p -= timedelta(days=1)
    return p


def third_friday(year: int, month: int) -> date:
    """The 3rd Friday of a month — monthly options expiration."""
    first = date(year, month, 1)
    # weekday(): Mon=0 .. Fri=4
    offset = (4 - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


def _effective_expiration(third_fri: date) -> date:
    """If the 3rd Friday isn't a trading day (e.g. Juneteenth 2026), expiration
    moves to the prior trading day."""
    return third_fri if is_trading_day(third_fri) else prev_session(third_fri)


def scheduled_calendar_events(d: date) -> list[ScheduledEvent]:
    """Deterministic events landing on ``d`` — FOMC, OpEx, triple witching,
    S&P rebalance, Russell recon, hand-listed one-offs."""
    out: list[ScheduledEvent] = []
    econ = load_econ_calendar(d.year)

    if d in econ["fomc_decisions"]:
        out.append(ScheduledEvent(
            label="FOMC rate decision + press conference",
            date=d, kind="fomc", when="14:00 ET", impact="High", country="USD",
        ))

    tf = third_friday(d.year, d.month)
    eff = _effective_expiration(tf)
    if d == eff:
        if d.month in _QUARTERLY_MONTHS:
            out.append(ScheduledEvent(
                label="Triple witching (index fut/opts + single-stock opts expire)",
                date=d, kind="triple_witching", when="at close", impact="High",
            ))
            out.append(ScheduledEvent(
                label="S&P Dow Jones quarterly index rebalance effective",
                date=d, kind="rebalance", when="at close", impact="Medium",
            ))
        else:
            out.append(ScheduledEvent(
                label="Monthly options expiration (OpEx)",
                date=d, kind="opex", when="at close", impact="Medium",
            ))

    for e in econ["index_events"] + econ["one_offs"]:
        if e["date"] == d:
            out.append(ScheduledEvent(label=e["label"], date=d, kind="index",
                                     when="", impact="Medium"))
    return out


def upcoming_calendar_events(d: date, days: int = 5) -> list[ScheduledEvent]:
    out: list[ScheduledEvent] = []
    for i in range(days + 1):
        out.extend(scheduled_calendar_events(d + timedelta(days=i)))
    return out
