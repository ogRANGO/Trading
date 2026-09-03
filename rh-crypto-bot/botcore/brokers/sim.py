"""In-process fill simulator.

Drives the backtester and can back an offline paper session. Fill model:

  * market order      -> fills at the current reference price, moved against you
                         by ``slippage_pct`` (+ ``crypto_spread_pct`` for crypto)
  * limit order       -> fills when the bar's traded range reaches the limit
  * stop / stop_limit  -> arms at the stop, then fills (market/limit) with slippage
  * commission         -> ``max(commission_pct * notional, commission_min_usd)``

The backtester calls :meth:`mark` once per bar with that bar's OHLC so stop /
limit / target logic can use the intrabar high and low.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Dict, List, Optional

if TYPE_CHECKING:
    from botcore.store.state import SimBrokerStore

from botcore.brokers.base import (
    Account,
    BrokerClient,
    BrokerError,
    Order,
    OrderRequest,
    Position,
    Quote,
)
from botcore.config import FeesCfg
from botcore.data.base import asset_class


class _Bar:
    __slots__ = ("open", "high", "low", "close")

    def __init__(self, o: float, h: float, low: float, c: float) -> None:
        self.open, self.high, self.low, self.close = o, h, low, c


class SimBroker(BrokerClient):
    name = "sim"

    def __init__(
        self,
        starting_cash: float = 100_000.0,
        fees: Optional[FeesCfg] = None,
        *,
        store: Optional["SimBrokerStore"] = None,
    ) -> None:
        self.cash = float(starting_cash)
        self.fees = fees or FeesCfg()
        self._positions: Dict[str, Position] = {}
        self._orders: Dict[str, Order] = {}
        self._open_ids: List[str] = []
        self._bars: Dict[str, _Bar] = {}
        self._prices: Dict[str, float] = {}
        self.realized_pnl = 0.0
        self.total_fees = 0.0
        self.fill_log: List[Order] = []
        self._clock = time.time()

        # Phase 5: optional SQLite persistence so the paper book survives a restart.
        self._store = store
        if store is not None:
            snap = store.load()
            if snap is None:
                store.save_account(self.cash, self.realized_pnl, self.total_fees)  # seed first boot
            else:
                self.cash = snap.cash
                self.realized_pnl = snap.realized_pnl
                self.total_fees = snap.total_fees
                self._positions = dict(snap.positions)

    def _persist(self, symbol: str) -> None:
        if self._store is None:
            return
        self._store.save_account(self.cash, self.realized_pnl, self.total_fees)
        if symbol in self._positions:
            self._store.save_position(self._positions[symbol])
        else:
            self._store.delete_position(symbol)

    # -- backtest plumbing ------------------------------------------------- #
    def mark(self, bars: Dict[str, dict], *, clock: Optional[float] = None) -> List[Order]:
        """Set the current bar for each symbol and process resting orders.

        ``bars`` maps symbol -> {open, high, low, close}. Returns orders that
        filled on this mark.
        """
        if clock is not None:
            self._clock = clock
        for sym, b in bars.items():
            self._bars[sym] = _Bar(b["open"], b["high"], b["low"], b["close"])
            self._prices[sym] = b["close"]
        for p in self._positions.values():
            if p.symbol in self._prices:
                p.market_price = self._prices[p.symbol]
        filled: List[Order] = []
        for oid in list(self._open_ids):
            o = self._orders[oid]
            if self._try_fill_resting(o):
                filled.append(o)
        return filled

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price
        self._bars[symbol] = _Bar(price, price, price, price)
        if symbol in self._positions:
            self._positions[symbol].market_price = price

    # -- BrokerClient ---------------------------------------------------- #
    def get_account(self) -> Account:
        eq = self.cash + sum(p.market_value for p in self._positions.values())
        return Account(cash=self.cash, equity=eq, buying_power=max(self.cash, 0.0))

    def get_positions(self) -> List[Position]:
        return [p for p in self._positions.values() if abs(p.qty) > 1e-12]

    def get_quote(self, symbol: str) -> Quote:
        px = self._prices.get(symbol)
        if px is None:
            raise BrokerError(f"sim has no price for {symbol}")
        half = px * self._cost_pct(symbol) / 2.0
        return Quote(symbol=symbol, bid=px - half, ask=px + half, ts=self._clock)

    def place_order(self, req: OrderRequest) -> Order:
        req.validate()
        oid = str(uuid.uuid4())
        o = Order(
            id=oid, client_order_id=req.client_order_id, symbol=req.symbol, side=req.side,
            qty=req.qty, type=req.type, status="new", limit_price=req.limit_price,
            stop_price=req.stop_price, submitted_at=self._clock, reason=req.reason,
            strategy=req.strategy,
        )
        self._orders[oid] = o
        if req.type == "market":
            self._fill(o, self._ref_price(req.symbol), reason="market")
        else:
            o.status = "accepted"
            self._open_ids.append(oid)
        return o

    def fill_market(
        self, symbol: str, side: str, qty: float, *, ref_price: Optional[float] = None
    ) -> Order:
        """Immediately fill a market order at ``ref_price`` (default: last price),
        with slippage + commission applied. Used by the backtester for entries and
        for stop/target exits at a specified level."""
        ref = ref_price if ref_price is not None else self._ref_price(symbol)
        o = Order(
            id=str(uuid.uuid4()), client_order_id=str(uuid.uuid4()), symbol=symbol,
            side=side, qty=qty, type="market", status="new", submitted_at=self._clock,
        )
        self._orders[o.id] = o
        self._fill(o, ref, reason="market")
        return o

    def cancel_order(self, order_id: str) -> None:
        o = self._orders.get(order_id)
        if o and o.is_open:
            o.status = "canceled"
            if order_id in self._open_ids:
                self._open_ids.remove(order_id)

    def get_order(self, order_id: str) -> Order:
        if order_id not in self._orders:
            raise BrokerError(f"unknown order {order_id}")
        return self._orders[order_id]

    def list_orders(self, *, open_only: bool = False) -> List[Order]:
        vals = list(self._orders.values())
        return [o for o in vals if o.is_open] if open_only else vals

    # -- internals ------------------------------------------------------ #
    def _cost_pct(self, symbol: str) -> float:
        c = self.fees.slippage_pct
        if asset_class(symbol) == "crypto":
            c += self.fees.crypto_spread_pct
        return c

    def _ref_price(self, symbol: str) -> float:
        if symbol not in self._prices:
            raise BrokerError(f"sim has no price for {symbol}")
        return self._prices[symbol]

    def _try_fill_resting(self, o: Order) -> bool:
        bar = self._bars.get(o.symbol)
        if bar is None:
            return False
        if o.type == "limit":
            if o.side == "buy" and bar.low <= o.limit_price:
                return self._fill(o, min(o.limit_price, bar.open), reason="limit")
            if o.side == "sell" and bar.high >= o.limit_price:
                return self._fill(o, max(o.limit_price, bar.open), reason="limit")
            return False
        if o.type in ("stop", "stop_limit"):
            triggered = (o.side == "buy" and bar.high >= o.stop_price) or (
                o.side == "sell" and bar.low <= o.stop_price
            )
            if not triggered:
                return False
            if o.type == "stop":
                return self._fill(o, o.stop_price, reason="stop")
            # stop_limit: becomes a limit at limit_price
            o.type = "limit"
            return self._try_fill_resting(o)
        return False

    def _fill(self, o: Order, ref_price: float, *, reason: str) -> bool:
        slip = ref_price * self._cost_pct(o.symbol)
        price = ref_price + slip if o.side == "buy" else ref_price - slip
        notional = price * o.qty
        fee = max(self.fees.commission_pct * notional, self.fees.commission_min_usd)

        if o.side == "buy":
            self.cash -= notional + fee
            self._apply_buy(o.symbol, o.qty, price)
        else:
            self.cash += notional - fee
            self._apply_sell(o.symbol, o.qty, price)

        self.total_fees += fee
        o.status = "filled"
        o.filled_qty = o.qty
        o.filled_avg_price = price
        o.fee = fee
        o.filled_at = self._clock
        if o.id in self._open_ids:
            self._open_ids.remove(o.id)
        self.fill_log.append(o)
        self._persist(o.symbol)
        return True

    def _apply_buy(self, symbol: str, qty: float, price: float) -> None:
        p = self._positions.get(symbol)
        if p is None or abs(p.qty) < 1e-12:
            self._positions[symbol] = Position(symbol, qty, price, price)
        else:
            new_qty = p.qty + qty
            p.avg_price = (p.avg_price * p.qty + price * qty) / new_qty
            p.qty = new_qty

    def _apply_sell(self, symbol: str, qty: float, price: float) -> None:
        p = self._positions.get(symbol)
        if p is None or p.qty < qty - 1e-9:
            raise BrokerError(f"sim: cannot sell {qty} {symbol}; hold {0 if p is None else p.qty}")
        self.realized_pnl += (price - p.avg_price) * qty
        p.qty -= qty
        p.market_price = price
        if p.qty < 1e-12:
            self._positions.pop(symbol, None)
