"""FILINGS lane — SEC EDGAR per-CIK submissions.

8-K: filtered hard by item code (data is in the submissions JSON, no extra
calls). Form 4: surfaced as a count + date, direction/value NOT parsed in v1
(that needs the Form 4 XML; deferred). Labelled honestly so absence of a
"buy" line is never read as "no insider buying".
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import httpx

from botcore.brief.bconfig import REPO_ROOT
from botcore.brief.httpx_util import UA
from botcore.brief.models import Filing, SourceResult

log = logging.getLogger(__name__)
_SUB = "https://data.sec.gov/submissions/CIK{:010d}.json"
_CACHE = REPO_ROOT / "data" / "sec_cik_cache.json"

_8K_ITEM_NAMES = {
    "1.01": "material agreement", "1.02": "agreement terminated",
    "2.02": "results / guidance", "4.01": "auditor change",
    "5.02": "exec / board change", "7.01": "Reg FD disclosure", "8.01": "other event",
}


@lru_cache(maxsize=1)
def _cik_map() -> dict[str, int]:
    try:
        raw = json.loads(_CACHE.read_text("utf-8"))
        return {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}
    except Exception as exc:  # noqa: BLE001
        log.warning("cik cache load failed: %s", exc)
        return {}


def fetch(cfg: dict, symbols: list[str], since_hours: int = 30) -> SourceResult:
    keep_items = set(cfg["filings"]["keep_8k_items"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    cmap = _cik_map()
    filings: list[Filing] = []
    misses: list[str] = []
    errs: list[str] = []

    for sym in symbols:
        if sym.upper().endswith("-USD"):
            continue                       # crypto: no SEC filings
        cik = cmap.get(sym.upper())
        if not cik:
            misses.append(sym)             # ETF or unlisted — no 8-K/Form 4 anyway
            log.debug("no CIK for %s", sym)
            continue
        try:
            j = httpx.get(_SUB.format(cik), headers={"User-Agent": UA}, timeout=12).json()
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{sym}: {exc}")
            continue
        rec = j.get("filings", {}).get("recent", {})
        forms = rec.get("form", [])
        f4_dates: list[str] = []
        for i, form in enumerate(forms):
            if form not in ("8-K", "4"):
                continue
            fdate = rec["filingDate"][i]
            fdt = datetime.fromisoformat(fdate).replace(tzinfo=timezone.utc)
            if fdt < cutoff:
                continue
            acc = rec["accessionNumber"][i].replace("-", "")
            doc = rec["primaryDocument"][i]
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
            if form == "8-K":
                items = [x.strip() for x in (rec["items"][i] or "").split(",") if x.strip()]
                kept = [it for it in items if it in keep_items]
                if not kept:
                    continue
                note = ", ".join(f"{it} {_8K_ITEM_NAMES.get(it, '')}".strip() for it in kept)
                filings.append(Filing(sym.upper(), "8-K", fdt, url, kept, note))
            elif form == "4":
                f4_dates.append(fdate)
        if f4_dates:
            newest = max(f4_dates)
            filings.append(Filing(
                sym.upper(), "4", datetime.fromisoformat(newest).replace(tzinfo=timezone.utc),
                f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4",
                [], f"{len(f4_dates)} insider Form 4(s) filed {newest} (direction not parsed)",
            ))
        time.sleep(0.12)   # EDGAR: stay well under 10 req/s

    filings.sort(key=lambda f: f.filed, reverse=True)
    ok = not errs or len(errs) < len(symbols)
    detail = f"{len(filings)} filings"
    if errs:
        detail += "; err: " + "; ".join(errs[:3])
    return SourceResult("filings", ok, detail, filings)
