"""PRE-MARKET TAPE lane.

Alpaca pre-market ETF proxies + crypto (move from 04:00 ET), CBOE delayed
quotes for vol/rates (frozen outside RTH -> shown as "prior close"), and a
best-effort Yahoo pass for 24h futures + international closes.
"""

from __future__ import annotations

import logging

import httpx

from botcore.brief import alpaca_client as alp
from botcore.brief.httpx_util import BROWSER_UA
from botcore.brief.models import SourceResult, TapeQuote

log = logging.getLogger(__name__)

_CBOE = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/{}.json"


def fetch(cfg: dict) -> SourceResult:
    tape_cfg = cfg["tape"]
    quotes: list[TapeQuote] = []
    notes: list[str] = []

    # --- Alpaca ETF proxies ------------------------------------------------
    etf = tape_cfg.get("alpaca_etf", [])
    if etf and alp.available():
        try:
            snaps = alp.stock_snapshots([s for _, s in etf])
            for label, sym in etf:
                last, chg = alp.snapshot_change(snaps.get(sym, {}))
                quotes.append(TapeQuote(sym, label, "equity", last, None, chg))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"alpaca etf: {exc}")
    elif etf:
        notes.append("alpaca creds missing")

    # --- Alpaca crypto ---------------------------------------------------
    cr = tape_cfg.get("alpaca_crypto", [])
    if cr and alp.available():
        try:
            snaps = alp.crypto_snapshots([s for _, s in cr])
            for label, sym in cr:
                last, chg = alp.snapshot_change(snaps.get(sym, {}))
                quotes.append(TapeQuote(sym, label, "crypto", last, None, chg))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"alpaca crypto: {exc}")

    # --- CBOE vol + rates ----------------------------------------------
    for label, sym in tape_cfg.get("cboe", []):
        try:
            r = httpx.get(_CBOE.format(sym), headers={"User-Agent": BROWSER_UA}, timeout=10)
            r.raise_for_status()
            d = r.json().get("data", {})
            px = d.get("current_price")
            prev = _f(d.get("prev_day_close"))
            scale = 0.1 if sym == "_TNX" else 1.0   # _TNX is yield x10
            quotes.append(TapeQuote(
                sym, label, "volatility" if "VIX" in sym else "rates",
                float(px) * scale if px is not None else None,
                prev * scale if prev is not None else None,
                _f(d.get("price_change_percent")),
            ))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"cboe {sym}: {exc}")

    # --- Yahoo bonus (try, don't depend) ------------------------------
    yb = tape_cfg.get("yahoo_bonus", [])
    got_yahoo = 0
    for label, sym in yb:
        q = _yahoo_quote(sym, label)
        if q:
            quotes.append(q)
            got_yahoo += 1
    if yb and got_yahoo == 0:
        notes.append("yahoo futures/intl rate-limited")

    ok = len([q for q in quotes if q.price is not None]) >= 4
    detail = "; ".join(notes) if notes else f"{len(quotes)} quotes"
    return SourceResult("tape", ok, detail, quotes)


def _yahoo_quote(sym: str, label: str) -> TapeQuote | None:
    group = "asia_europe" if sym.startswith("^") else "us_futures"
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            r = httpx.get(
                f"https://{host}/v8/finance/chart/{sym}",
                params={"range": "3d", "interval": "1d"},
                headers={"User-Agent": BROWSER_UA}, timeout=8, follow_redirects=True,
            )
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("application/json"):
                continue
            m = r.json()["chart"]["result"][0]["meta"]
            px, prev = m.get("regularMarketPrice"), m.get("chartPreviousClose") or m.get("previousClose")
            if px is None or not prev:
                continue
            return TapeQuote(sym, label, group, float(px), float(prev),
                             round((float(px) / float(prev) - 1) * 100, 2))
        except Exception:  # noqa: BLE001
            continue
    return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
