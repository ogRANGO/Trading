"""One shared GET helper for every fetcher.

Read-only by construction: only ``client.get`` is exposed. Short timeouts,
a couple of retries on throttle, and a descriptive User-Agent (SEC and a few
others require a real contact string).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

UA = "rh-crypto-bot pre-market-brief (contact: thomastarango23@gmail.com)"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")


def get_json(
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 12.0,
    retries: int = 2,
    browser_ua: bool = False,
) -> Any:
    """GET and parse JSON. Raises on final failure; callers catch and degrade."""
    h = {"User-Agent": BROWSER_UA if browser_ua else UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = httpx.get(url, params=params, headers=h, timeout=timeout, follow_redirects=True)
            if r.status_code in (429, 999, 503):
                raise RuntimeError(f"throttled {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def get_text(
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 12.0,
    retries: int = 2,
    browser_ua: bool = False,
) -> str:
    h = {"User-Agent": BROWSER_UA if browser_ua else UA}
    if headers:
        h.update(headers)
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            r = httpx.get(url, params=params, headers=h, timeout=timeout, follow_redirects=True)
            if r.status_code in (429, 999, 503):
                raise RuntimeError(f"throttled {r.status_code}")
            r.raise_for_status()
            return r.text
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")
