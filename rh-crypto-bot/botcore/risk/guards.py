"""Market-hours gate (equities) and the Pattern Day Trader guard.

Crypto trades 24/7. US equities trade the regular session 09:30-16:00 America/
New_York, Mon-Fri. A short list of full-day market holidays is included; this is
approximate (early closes and every future holiday are not modelled) and the
live engine should prefer the broker's own clock when available.
"""

from __future__ import annotations

from collections import deque
from datetime import date, datetime, time, timedelta, timezone
from typing import Deque, Iterable

try:
    from zoneinfo import ZoneInfo
    _NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - zoneinfo/tzdata missing
    _NY = timezone(timedelta(hours=-5))

_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)

# Full-day US market closures (extend as needed).
_HOLIDAYS = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}


def _to_ny(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(_NY)


def past_et_time(now: datetime, hhmm: str) -> bool:
    """Is ``now`` at or past ``HH:MM`` Eastern on its own calendar day?

    Used by the intraday exit clock (flat-by-close, entry cutoff). Kept here so
    there is exactly one DST-aware conversion in the codebase.
    """
    hh, mm = (int(x) for x in hhmm.split(":"))
    return _to_ny(now).time() >= time(hh, mm)


def is_market_open(asset_class: str, now: "datetime | None" = None) -> bool:
    if asset_class == "crypto":
        return True
    n = _to_ny(now or datetime.now(timezone.utc))
    if n.weekday() >= 5 or n.date() in _HOLIDAYS:
        return False
    return _RTH_OPEN <= n.time() < _RTH_CLOSE


def next_market_open(asset_class: str, now: "datetime | None" = None) -> datetime:
    if asset_class == "crypto":
        return now or datetime.now(timezone.utc)
    n = _to_ny(now or datetime.now(timezone.utc))
    candidate = n.replace(hour=_RTH_OPEN.hour, minute=_RTH_OPEN.minute, second=0, microsecond=0)
    if n.time() >= _RTH_OPEN:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5 or candidate.date() in _HOLIDAYS:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


class PDTGuard:
    """Rolling-5-business-day day-trade counter.

    A *day trade* = opening and closing the same security within one session.
    Accounts below ``min_equity`` are limited to ``max_day_trades`` in any
    rolling 5-business-day window (FINRA rule). Above it, unlimited.
    """

    def __init__(self, min_equity: float = 25_000.0, max_day_trades: int = 3) -> None:
        self.min_equity = min_equity
        self.max_day_trades = max_day_trades
        self._marks: Deque[date] = deque(maxlen=64)

    def record_day_trade(self, when: "datetime | date") -> None:
        d = when.date() if isinstance(when, datetime) else when
        self._marks.append(d)

    def load(self, days: Iterable["datetime | date"]) -> None:
        for d in days:
            self.record_day_trade(d)

    def count_in_window(self, now: "datetime | None" = None) -> int:
        ref = _to_ny(now or datetime.now(timezone.utc)).date()
        cutoff = ref - timedelta(days=7)  # ~5 business days
        return sum(1 for d in self._marks if d >= cutoff)

    def can_day_trade(self, equity: float, now: "datetime | None" = None) -> bool:
        if equity >= self.min_equity:
            return True
        return self.count_in_window(now) < self.max_day_trades
