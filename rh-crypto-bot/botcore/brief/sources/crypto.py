"""CRYPTO LANE — market-wide crypto state.

CoinGecko for dominance / total mcap / BTC+ETH move (free, no key).
OKX public funding rate (Deribit fallback for BTC). All 24h-fresh.
"""

from __future__ import annotations

import logging

import httpx

from botcore.brief.httpx_util import BROWSER_UA
from botcore.brief.models import CryptoState, SourceResult

log = logging.getLogger(__name__)
_H = {"User-Agent": BROWSER_UA}


def fetch(cfg: dict) -> SourceResult:
    st = CryptoState()
    notes: list[str] = []

    try:
        g = httpx.get("https://api.coingecko.com/api/v3/global", headers=_H, timeout=12).json()["data"]
        st.btc_dominance_pct = round(float(g["market_cap_percentage"]["btc"]), 1)
        st.total_mcap_usd = float(g["total_market_cap"]["usd"])
        st.total_mcap_change_pct = round(float(g["market_cap_change_percentage_24h_usd"]), 2)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"coingecko global: {exc}")

    try:
        sp = httpx.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin,ethereum", "vs_currencies": "usd", "include_24hr_change": "true"},
            headers=_H, timeout=12,
        ).json()
        st.btc_price = _f(sp["bitcoin"]["usd"])
        st.btc_change_pct = round(_f(sp["bitcoin"]["usd_24h_change"]) or 0.0, 2)
        st.eth_price = _f(sp["ethereum"]["usd"])
        st.eth_change_pct = round(_f(sp["ethereum"]["usd_24h_change"]) or 0.0, 2)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"coingecko price: {exc}")

    st.btc_funding_pct = _okx_funding("BTC-USDT-SWAP")
    st.eth_funding_pct = _okx_funding("ETH-USDT-SWAP")
    if st.btc_funding_pct is None:
        st.btc_funding_pct = _deribit_funding()
    if st.btc_funding_pct is None:
        notes.append("funding unavailable")

    ok = st.btc_dominance_pct is not None and st.btc_price is not None
    return SourceResult("crypto", ok, "; ".join(notes) if notes else "ok", st)


def _okx_funding(inst: str) -> float | None:
    try:
        r = httpx.get("https://www.okx.com/api/v5/public/funding-rate",
                      params={"instId": inst}, headers=_H, timeout=10)
        rate = r.json()["data"][0]["fundingRate"]
        return round(float(rate) * 100, 4)   # -> percent per interval
    except Exception as exc:  # noqa: BLE001
        log.debug("okx funding %s: %s", inst, exc)
        return None


def _deribit_funding() -> float | None:
    import time
    try:
        now = int(time.time() * 1000)
        r = httpx.get("https://www.deribit.com/api/v2/public/get_funding_rate_value",
                      params={"instrument_name": "BTC-PERPETUAL",
                              "start_timestamp": now - 28_800_000, "end_timestamp": now},
                      headers=_H, timeout=10)
        return round(float(r.json()["result"]) * 100, 4)
    except Exception:  # noqa: BLE001
        return None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
