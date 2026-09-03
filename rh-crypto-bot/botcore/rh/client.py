"""Read-only client for the Robinhood Crypto Trading API.

Phase 0 deliberately exposes **no** order-placement methods — there is no write
path yet. Order methods are added in Phase 3 (`execution/live.py`).

Endpoints (base ``https://trading.robinhood.com``):
    GET /api/v1/crypto/trading/accounts/
    GET /api/v1/crypto/trading/holdings/
    GET /api/v1/crypto/trading/trading_pairs/
    GET /api/v1/crypto/trading/orders/
    GET /api/v1/crypto/trading/orders/{id}/
    GET /api/v1/crypto/marketdata/best_bid_ask/?symbol=BTC-USD
    GET /api/v1/crypto/marketdata/estimated_price/?symbol=BTC-USD&side=buy&quantity=0.1
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlencode

import httpx

from botcore.config import Settings
from botcore.rh.ratelimit import TokenBucket
from botcore.rh.signing import auth_headers

log = logging.getLogger(__name__)

ACCOUNTS_PATH = "/api/v1/crypto/trading/accounts/"
HOLDINGS_PATH = "/api/v1/crypto/trading/holdings/"
TRADING_PAIRS_PATH = "/api/v1/crypto/trading/trading_pairs/"
ORDERS_PATH = "/api/v1/crypto/trading/orders/"
BEST_BID_ASK_PATH = "/api/v1/crypto/marketdata/best_bid_ask/"
ESTIMATED_PRICE_PATH = "/api/v1/crypto/marketdata/estimated_price/"


class RobinhoodAPIError(RuntimeError):
    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.body = body


class RobinhoodCryptoClient:
    def __init__(
        self,
        settings: Settings,
        *,
        bucket: Optional[TokenBucket] = None,
        timeout: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        if not settings.has_rh_credentials():
            raise RuntimeError(
                "Robinhood API credentials missing. Set RH_API_KEY and "
                "RH_PRIVATE_KEY_B64 in .env (see .env.example)."
            )
        self._s = settings
        self._base = settings.rh_api_base_url.rstrip("/")
        self._bucket = bucket or TokenBucket(rate_per_sec=1.0, capacity=3.0)
        self._max_retries = max_retries
        self._http = httpx.Client(timeout=timeout, headers={"User-Agent": "rh-crypto-bot/0.0.1"})

    # -- lifecycle ------------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "RobinhoodCryptoClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- core request -------------------------------------------------------- #
    @staticmethod
    def _build_path(path: str, params: "Optional[Iterable[tuple]]" = None) -> str:
        if not params:
            return path
        query = urlencode(list(params), doseq=True)
        return f"{path}?{query}" if query else path

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: "Optional[Iterable[tuple]]" = None,
        json_body: Optional[dict] = None,
    ) -> Any:
        import json as _json

        full_path = self._build_path(path, params)
        body_str = _json.dumps(json_body, separators=(",", ":")) if json_body is not None else ""
        url = f"{self._base}{full_path}"

        last_exc: Optional[Exception] = None
        for attempt in range(1, self._max_retries + 1):
            self._bucket.acquire()
            headers = auth_headers(
                self._s.rh_private_key_b64, self._s.rh_api_key, full_path, method, body_str
            )
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            try:
                resp = self._http.request(
                    method, url, headers=headers,
                    content=body_str.encode("utf-8") if body_str else None,
                )
            except httpx.HTTPError as exc:  # network-level
                last_exc = exc
                log.warning("request error (%s/%s): %s", attempt, self._max_retries, exc)
                time.sleep(min(2 ** attempt, 10))
                continue

            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", "2"))
                log.warning("429 rate limited; sleeping %.1fs", retry_after)
                last_exc = RobinhoodAPIError(429, "rate limited (retries exhausted)", resp.text)
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500:
                last_exc = RobinhoodAPIError(resp.status_code, "server error", resp.text)
                time.sleep(min(2 ** attempt, 10))
                continue
            if resp.status_code >= 400:
                self._raise_for_response(resp)

            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError:
                return resp.text

        if last_exc:
            raise last_exc
        raise RobinhoodAPIError(0, "exhausted retries")

    @staticmethod
    def _raise_for_response(resp: httpx.Response) -> None:
        try:
            payload = resp.json()
            message = payload.get("detail") or payload.get("error") or str(payload)
        except ValueError:
            payload = resp.text
            message = resp.text[:300]
        if resp.status_code in (401, 403):
            message = (
                f"{message}\nAuth failed. Check RH_API_KEY / RH_PRIVATE_KEY_B64, that the "
                "public key is registered on Robinhood, and that the key has the needed "
                "scopes (read account / read market data)."
            )
        raise RobinhoodAPIError(resp.status_code, message, payload)

    # -- read endpoints ---------------------------------------------------- #
    def get_account(self) -> Dict[str, Any]:
        return self._request("GET", ACCOUNTS_PATH)

    def get_holdings(self, asset_codes: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        params = [("asset_code", c) for c in asset_codes] if asset_codes else None
        return self._request("GET", HOLDINGS_PATH, params=params)

    def get_trading_pairs(self, symbols: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        params = [("symbol", s.upper()) for s in symbols] if symbols else None
        return self._request("GET", TRADING_PAIRS_PATH, params=params)

    def get_best_bid_ask(self, symbols: Iterable[str]) -> Dict[str, Any]:
        params = [("symbol", s.upper()) for s in symbols]
        return self._request("GET", BEST_BID_ASK_PATH, params=params)

    def get_estimated_price(self, symbol: str, side: str, quantity: str) -> Dict[str, Any]:
        params = [("symbol", symbol.upper()), ("side", side), ("quantity", str(quantity))]
        return self._request("GET", ESTIMATED_PRICE_PATH, params=params)

    def get_orders(self, **filters: Any) -> Dict[str, Any]:
        params = [(k, v) for k, v in filters.items() if v is not None]
        return self._request("GET", ORDERS_PATH, params=params or None)

    def get_order(self, order_id: str) -> Dict[str, Any]:
        return self._request("GET", f"{ORDERS_PATH}{order_id}/")

    # -- convenience ----------------------------------------------------- #
    def get_buying_power(self) -> float:
        acct = self.get_account()
        for key in ("buying_power", "crypto_buying_power", "cash_available_for_withdrawal"):
            if key in acct:
                try:
                    return float(acct[key])
                except (TypeError, ValueError):
                    pass
        return 0.0

    def list_universe_prices(self, symbols: List[str]) -> Dict[str, Dict[str, float]]:
        """Return {symbol: {bid, ask, mid}} for the given pairs."""
        data = self.get_best_bid_ask(symbols)
        out: Dict[str, Dict[str, float]] = {}
        for row in data.get("results", data if isinstance(data, list) else []):
            sym = row.get("symbol")
            try:
                bid = float(row.get("bid_inclusive_of_sell_spread", row.get("bid_price", 0)) or 0)
                ask = float(row.get("ask_inclusive_of_buy_spread", row.get("ask_price", 0)) or 0)
            except (TypeError, ValueError):
                continue
            if sym and bid and ask:
                out[sym] = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}
        return out
