"""HEADLINES lane — merge Finnhub company-news + Alpaca (Benzinga) news for
held + watchlist names. Dedupe/scoring happens in assemble.py; this just
normalises to NewsItem and tags analyst-action headlines.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

from botcore.brief import alpaca_client as alp
from botcore.brief.httpx_util import UA
from botcore.brief.models import NewsItem, SourceResult

log = logging.getLogger(__name__)
_FINNHUB = "https://finnhub.io/api/v1/company-news"


def fetch(cfg: dict, symbols: list[str], since: datetime) -> SourceResult:
    if not symbols:
        return SourceResult("news", True, "no symbols", [])
    kw = [k.lower() for k in cfg["headlines"]["analyst_keywords"]]
    items: list[NewsItem] = []
    notes: list[str] = []

    # --- Finnhub company-news (per symbol) --------------------------------
    fh_key = _finnhub_key()
    fh_hit = 0
    if fh_key:
        frm = since.date().isoformat()
        to = (since.date() + timedelta(days=2)).isoformat()
        for sym in symbols:
            try:
                rows = httpx.get(_FINNHUB, params={"symbol": sym, "from": frm, "to": to, "token": fh_key},
                                 headers={"User-Agent": UA}, timeout=12).json()
            except Exception as exc:  # noqa: BLE001
                notes.append(f"finnhub {sym}: {exc}")
                continue
            for r in rows if isinstance(rows, list) else []:
                ts = _epoch(r.get("datetime"))
                if ts is None or ts < since:
                    continue
                items.append(_mk(r.get("headline", ""), r.get("url", ""),
                                 r.get("source", "finnhub"), ts, [sym],
                                 r.get("summary", ""), kw))
                fh_hit += 1
    else:
        notes.append("no FINNHUB_KEY")

    # --- Alpaca / Benzinga (batched) ------------------------------------
    al_hit = 0
    if alp.available():
        for row in alp.news(symbols, since, limit=50):
            ts = _iso(row.get("created_at"))
            if ts is None or ts < since:
                continue
            items.append(_mk(row.get("headline", ""), row.get("url", ""),
                             row.get("source", "benzinga"), ts,
                             [s.upper() for s in row.get("symbols", []) if s.upper() in {x.upper() for x in symbols}],
                             row.get("summary", ""), kw))
            al_hit += 1
    else:
        notes.append("no alpaca creds")

    ok = bool(items) or (fh_key is not None and not notes)
    detail = f"finnhub {fh_hit} + alpaca {al_hit}" + ("; " + "; ".join(notes) if notes else "")
    return SourceResult("news", ok, detail, items)


def _mk(headline, url, source, ts, entities, summary, kw) -> NewsItem:
    hl = (headline or "").strip()
    kind = "analyst" if any(k in hl.lower() for k in kw) else "headline"
    return NewsItem(headline=hl, url=url or "", source=source or "", published=ts,
                    entities=entities, summary=(summary or "").strip(), kind=kind)


def _finnhub_key():
    from botcore.config import get_settings
    return get_settings().finnhub_key or None


def _epoch(v) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(v), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
