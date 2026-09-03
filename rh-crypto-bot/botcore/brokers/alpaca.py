"""Alpaca broker - paper (`paper-api.alpaca.markets`) and live (`api.alpaca.markets`).

Selected by ``BOT_MODE`` + ``ALPACA_PAPER``. Same ``BrokerClient`` surface as the
simulator, so strategy/risk code is identical between backtest, paper and live.

Exit management (stops / targets / trailing) is done by the engine, not by Alpaca
bracket orders, to keep parity with the backtester.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from botcore.brokers.base import (
    Account,
    BrokerClient,
    BrokerError,
    Order,
    OrderRequest,
    Position,
    Quote,
)
from botcore.config import Settings
from botcore.data.base import asset_class
from botcore.data.quotes import AlpacaQuoteFeed

log = logging.getLogger(__name__)

_STATUS = {
    "new": "new", "accepted": "accepted", "pending_new": "new", "accepted_for_bidding": "accepted",
    "partially_filled": "partially_filled", "filled": "filled",
    "done_for_day": "canceled", "canceled": "canceled", "expired": "canceled",
    "replaced": "canceled", "pending_cancel": "accepted", "pending_replace": "accepted",
    "rejected": "rejected", "suspended": "accepted", "stopped": "accepted", "calculated": "accepted",
}


def _to_alpaca_symbol(sym: str) -> str:
    return sym.upper().replace("-", "/") if asset_class(sym) == "crypto" else sym.upper()


def _from_alpaca_symbol(sym: str) -> str:
    return sym.replace("/", "-") if "/" in sym else sym


class AlpacaBroker(BrokerClient):
    supports_bracket = True

    def __init__(self, settings: Settings, *, paper: bool = True) -> None:
        if not settings.has_alpaca_credentials():
            raise BrokerError("Alpaca credentials missing (ALPACA_KEY_ID / ALPACA_SECRET_KEY)")
        self.s = settings
        self.paper = paper
        self.name = "alpaca-paper" if paper else "alpaca-live"
        self._base = "https://paper-api.alpaca.markets" if paper else "https://api.alpaca.markets"
        self._http = httpx.Client(
            base_url=self._base,
            headers={"APCA-API-KEY-ID": settings.alpaca_key_id,
                     "APCA-API-SECRET-KEY": settings.alpaca_secret_key,
                     "User-Agent": "rh-crypto-bot/0.2"},
            timeout=15.0,
        )
        self._quotes = AlpacaQuoteFeed(settings)

    def close(self) -> None:
        self._http.close()
        self._quotes.close()

    # -- http ---------------------------------------------------------------
    def _req(self, method: str, path: str, **kw) -> Any:
        r = self._http.request(method, path, **kw)
        if r.status_code == 404 and method == "GET":
            return None
        if r.status_code >= 400:
            raise BrokerError(f"alpaca {method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else None

    # -- BrokerClient -----------------------------------------------------
    def get_account(self) -> Account:
        a = self._req("GET", "/v2/account")
        return Account(
            cash=float(a["cash"]), equity=float(a["equity"]),
            buying_power=float(a.get("buying_power", a["cash"])),
        )

    def get_positions(self) -> List[Position]:
        rows = self._req("GET", "/v2/positions") or []
        out = []
        for p in rows:
            out.append(Position(
                symbol=_from_alpaca_symbol(p["symbol"]),
                qty=float(p["qty"]),
                avg_price=float(p["avg_entry_price"]),
                market_price=float(p.get("current_price") or p.get("avg_entry_price") or 0.0),
            ))
        return out

    def get_quote(self, symbol: str) -> Quote:
        return self._quotes.get_quote(symbol)

    def place_order(self, req: OrderRequest) -> Order:
        req.validate()
        klass = asset_class(req.symbol)
        tif = req.time_in_force
        if klass == "crypto" and tif == "day":
            tif = "gtc"
        body: Dict[str, Any] = {
            "symbol": _to_alpaca_symbol(req.symbol),
            "side": req.side,
            "type": req.type if req.type != "stop_limit" else "stop_limit",
            "time_in_force": tif,
            "qty": str(req.qty),
            "client_order_id": req.client_order_id,
        }
        if req.limit_price:
            body["limit_price"] = str(req.limit_price)
        if req.stop_price:
            body["stop_price"] = str(req.stop_price)
        data = self._req("POST", "/v2/orders", json=body)
        return self._order(data, req)

    def cancel_order(self, order_id: str) -> None:
        self._req("DELETE", f"/v2/orders/{order_id}")

    def get_order(self, order_id: str) -> Order:
        data = self._req("GET", f"/v2/orders/{order_id}")
        if data is None:
            raise BrokerError(f"alpaca: unknown order {order_id}")
        return self._order(data)

    def list_orders(self, *, open_only: bool = False) -> List[Order]:
        params = {"status": "open" if open_only else "all", "limit": 100, "nested": "false"}
        rows = self._req("GET", "/v2/orders", params=params) or []
        return [self._order(r) for r in rows]

    # -- mapping --------------------------------------------------------
    @staticmethod
    def _order(data: dict, req: "OrderRequest | None" = None) -> Order:
        return Order(
            id=str(data["id"]),
            client_order_id=data.get("client_order_id", ""),
            symbol=_from_alpaca_symbol(data["symbol"]),
            side=data["side"],
            qty=float(data.get("qty") or (req.qty if req else 0.0) or 0.0),
            type=data.get("type", "market"),
            status=_STATUS.get(data.get("status", ""), "accepted"),
            limit_price=_f(data.get("limit_price")),
            stop_price=_f(data.get("stop_price")),
            filled_qty=float(data.get("filled_qty") or 0.0),
            filled_avg_price=float(data.get("filled_avg_price") or 0.0),
            reason=req.reason if req else "",
            strategy=req.strategy if req else "",
        )


def _f(v):
    return float(v) if v not in (None, "") else None
