"""Trade halts — Nasdaq Trader RSS (free, no key).

Surfaces halts on held/watchlist names + a market-wide count. Most mornings
this is empty for the bots' megacap/crypto names; a spike in the count is
still a useful tape-health signal.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from botcore.brief.httpx_util import BROWSER_UA
from botcore.brief.models import SourceResult

log = logging.getLogger(__name__)
_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"
_NS = "{http://www.nasdaqtrader.com/}"
_REASON = {
    "LUDP": "volatility pause (LULD)", "T1": "news pending", "T2": "news released",
    "T12": "additional info requested", "H10": "SEC trading suspension",
    "T3": "news & resumption times", "M": "MWCB market-wide halt",
}


def fetch(cfg: dict, watch: set[str]) -> SourceResult:
    try:
        text = httpx.get(_URL, headers={"User-Agent": BROWSER_UA}, timeout=12,
                         follow_redirects=True).text
        root = ET.fromstring(text.lstrip("﻿ \r\n\t"))
    except Exception as exc:  # noqa: BLE001
        return SourceResult("halts", False, str(exc), {"watch_hits": [], "count": 0, "others": []})

    today = datetime.now(timezone.utc).date()
    watch_hits, others = [], []
    count = 0
    for item in root.iterfind(".//item"):
        sym = (item.findtext(f"{_NS}IssueSymbol") or "").strip().upper()
        if not sym:
            continue
        hdate = (item.findtext(f"{_NS}HaltDate") or "").strip()
        try:
            if datetime.strptime(hdate, "%m/%d/%Y").date() != today:
                continue
        except ValueError:
            pass
        count += 1
        reason = (item.findtext(f"{_NS}ReasonCode") or "").strip()
        rec = {
            "symbol": sym,
            "name": (item.findtext(f"{_NS}IssueName") or "").strip(),
            "reason": _REASON.get(reason, reason or "halt"),
            "halt_time": (item.findtext(f"{_NS}HaltTime") or "").strip()[:8],
            "resumed": bool((item.findtext(f"{_NS}ResumptionTradeTime") or "").strip()),
        }
        (watch_hits if sym in watch else others).append(rec)

    payload = {"watch_hits": watch_hits, "count": count, "others": others[:3]}
    detail = f"{count} halts today" + (f", {len(watch_hits)} on watch" if watch_hits else "")
    return SourceResult("halts", True, detail, payload)
