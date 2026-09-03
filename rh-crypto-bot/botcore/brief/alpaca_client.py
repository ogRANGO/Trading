"""Thin read-only Alpaca market-data client for the brief.

Only GET, only the market-data host. No trading host, no orders.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from botcore.brief.httpx_util import UA

log = logging.getLogger(__name__)

_DATA = "https://data.alpaca.markets"


def _creds() -> tuple[str, str]:
    from botcore.config import get_settings
    s = get_settings()
    return s.alpaca_key_id, s.alpaca_secret_key


def _headers() -> dict:
    kid, sec = _creds()
    return {"APCA-API-KEY-ID": kid, "APCA-API-SECRET-KEY": sec, "User-Agent": UA}


def available() -> bool:
    kid, sec = _creds()
    return bool(kid and sec)


def _get(path: str, params: dict, timeout: float = 15.0) -> dict:
    r = httpx.get(f"{_DATA}{path}", params=params, headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def stock_snapshots(symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    j = _get("/v2/stocks/snapshots", {"symbols": ",".join(sorted(set(symbols))), "feed": "iex"})
    # v2 stocks/snapshots returns {SYM: {...}} directly
    return {k: v for k, v in j.items() if isinstance(v, dict)}


def crypto_snapshots(symbols: list[str]) -> dict[str, dict]:
    if not symbols:
        return {}
    j = _get("/v1beta3/crypto/us/snapshots", {"symbols": ",".join(sorted(set(symbols)))})
    return j.get("snapshots", {})


def snapshot_change(snap: dict) -> tuple[Optional[float], Optional[float]]:
    """(last_price, pct_change_vs_prev_daily_close) from a snapshot dict."""
    last = (snap.get("latestTrade") or {}).get("p")
    if last is None:
        last = (snap.get("dailyBar") or {}).get("c")
    prev = (snap.get("prevDailyBar") or {}).get("c")
    if last is None or not prev:
        return _f(last), None
    return float(last), round((float(last) / float(prev) - 1) * 100, 2)


def news(symbols: list[str], start: datetime, limit: int = 50) -> list[dict]:
    """Alpaca /v1beta1/news (Benzinga). Best-effort; empty on failure."""
    try:
        j = _get("/v1beta1/news", {
            "symbols": ",".join(sorted(set(symbols))),
            "start": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": min(limit, 50),
            "sort": "desc",
        })
        return j.get("news", []) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("alpaca news failed: %s", exc)
        return []


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
