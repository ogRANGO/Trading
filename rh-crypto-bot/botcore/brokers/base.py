"""Broker-agnostic types and the :class:`BrokerClient` interface.

The strategy and risk layers only ever see this interface, so the exact same
code path runs against the simulator, Alpaca paper, Alpaca live, and the
Robinhood MCP.
"""

from __future__ import annotations

import abc
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

Side = str        # "buy" | "sell"
OrderType = str   # "market" | "limit" | "stop" | "stop_limit"
OrderStatus = str  # "new" | "accepted" | "filled" | "partially_filled" | "canceled" | "rejected"


@dataclass
class Account:
    cash: float
    equity: float
    buying_power: float
    currency: str = "USD"


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    market_price: float = 0.0

    @property
    def market_value(self) -> float:
        return self.qty * self.market_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.market_price - self.avg_price) * self.qty


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    ts: float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.ask or self.bid

    @property
    def spread_pct(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m if m > 0 else 0.0


@dataclass
class OrderRequest:
    symbol: str
    side: Side
    qty: float
    type: OrderType = "market"
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "day"
    client_order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    reason: str = ""          # free-text: why the bot placed this
    strategy: str = ""

    def validate(self) -> None:
        if self.side not in ("buy", "sell"):
            raise ValueError(f"bad side {self.side!r}")
        if self.qty <= 0:
            raise ValueError("qty must be > 0")
        if self.type in ("limit", "stop_limit") and not self.limit_price:
            raise ValueError(f"{self.type} order needs limit_price")
        if self.type in ("stop", "stop_limit") and not self.stop_price:
            raise ValueError(f"{self.type} order needs stop_price")


@dataclass
class Order:
    id: str
    client_order_id: str
    symbol: str
    side: Side
    qty: float
    type: OrderType
    status: OrderStatus
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    filled_qty: float = 0.0
    filled_avg_price: float = 0.0
    fee: float = 0.0
    submitted_at: float = field(default_factory=time.time)
    filled_at: Optional[float] = None
    reason: str = ""
    strategy: str = ""

    @property
    def is_open(self) -> bool:
        return self.status in ("new", "accepted", "partially_filled")

    @property
    def is_done(self) -> bool:
        return self.status in ("filled", "canceled", "rejected")


class BrokerError(RuntimeError):
    pass


class BrokerClient(abc.ABC):
    """Minimal surface the bot needs. Implementations may add more."""

    name: str = "abstract"
    supports_bracket: bool = False

    @abc.abstractmethod
    def get_account(self) -> Account: ...

    @abc.abstractmethod
    def get_positions(self) -> List[Position]: ...

    def get_position(self, symbol: str) -> Optional[Position]:
        for p in self.get_positions():
            if p.symbol == symbol:
                return p
        return None

    @abc.abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    @abc.abstractmethod
    def place_order(self, req: OrderRequest) -> Order: ...

    @abc.abstractmethod
    def cancel_order(self, order_id: str) -> None: ...

    @abc.abstractmethod
    def get_order(self, order_id: str) -> Order: ...

    @abc.abstractmethod
    def list_orders(self, *, open_only: bool = False) -> List[Order]: ...

    # convenience -----------------------------------------------------------
    def positions_by_symbol(self) -> Dict[str, Position]:
        return {p.symbol: p for p in self.get_positions()}

    def close(self) -> None:  # optional
        pass
