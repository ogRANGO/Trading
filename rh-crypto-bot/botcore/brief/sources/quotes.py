"""Per-name quotes for held + watchlist -> price_snapshot rows + BOOK/WATCHLIST %.

Equities via Alpaca stock snapshots (pre-market aware). Crypto via Coinbase
public stats (24h open -> change). All GET, best-effort per symbol.
"""

from __future__ import annotations

import logging

import httpx

from botcore.brief import alpaca_client as alp
from botcore.brief.httpx_util import UA
from botcore.brief.models import SourceResult

log = logging.getLogger(__name__)


def fetch(cfg: dict, symbols: list[str]) -> SourceResult:
    syms = sorted({s.upper() for s in symbols})
    equities = [s for s in syms if not s.endswith("-USD")]
    cryptos = [s for s in syms if s.endswith("-USD")]
    out: dict[str, dict] = {}
    notes: list[str] = []

    if equities and alp.available():
        try:
            snaps = alp.stock_snapshots(equities)
            for s in equities:
                snap = snaps.get(s, {})
                last, chg = alp.snapshot_change(snap)
                prev = (snap.get("prevDailyBar") or {}).get("c")
                out[s] = {"ref_price": last, "prev_close": _f(prev), "premarket": last,
                          "change_pct": chg}
        except Exception as exc:  # noqa: BLE001
            notes.append(f"alpaca equities: {exc}")
    elif equities:
        notes.append("no alpaca creds")

    for s in cryptos:
        product = s.replace("/", "-")
        try:
            st = httpx.get(f"https://api.exchange.coinbase.com/products/{product}/stats",
                           headers={"User-Agent": UA}, timeout=10).json()
            last, opn = _f(st.get("last")), _f(st.get("open"))
            chg = round((last / opn - 1) * 100, 2) if last and opn else None
            out[s] = {"ref_price": last, "prev_close": opn, "premarket": last, "change_pct": chg}
        except Exception as exc:  # noqa: BLE001
            notes.append(f"coinbase {s}: {exc}")

    ok = len(out) >= max(1, len(syms) // 2)
    return SourceResult("quotes", ok, "; ".join(notes) if notes else f"{len(out)}/{len(syms)}", out)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
