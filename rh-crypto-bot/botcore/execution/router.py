"""Pick the broker + quote feed for the current mode/config."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from botcore.brokers.base import BrokerClient, BrokerError
from botcore.brokers.sim import SimBroker
from botcore.config import BotConfig, Settings
from botcore.data.base import asset_class
from botcore.data.quotes import QuoteFeed, get_quote_feed

log = logging.getLogger(__name__)


@dataclass
class Execution:
    broker: BrokerClient
    quotes: QuoteFeed
    mode: str          # "paper" | "live"
    needs_price_feed: bool  # True for SimBroker (engine pushes prices each tick)

    def close(self) -> None:
        for obj in (self.broker, self.quotes):
            try:
                obj.close()
            except Exception:  # noqa: BLE001
                pass


def build_execution(settings: Settings, cfg: BotConfig, *, conn=None) -> Execution:
    mode = settings.bot_mode
    broker_kind = settings.broker
    universe_is_crypto = all(asset_class(s) == "crypto" for s in cfg.universe)

    if broker_kind == "sim":
        store = None
        if conn is not None:
            from botcore.store.state import SimBrokerStore

            store = SimBrokerStore(conn)
        broker: BrokerClient = SimBroker(
            starting_cash=settings.paper_start_equity, fees=cfg.fees, store=store
        )
        prefer = "coinbase" if (universe_is_crypto and not settings.has_alpaca_credentials()) else "auto"
        quotes = get_quote_feed(settings, prefer=prefer)
        log.info("execution: SimBroker (%s) + %s quotes, mode=%s", broker.name, quotes.name, mode)
        return Execution(broker, quotes, mode, needs_price_feed=True)

    if broker_kind == "alpaca":
        if not settings.has_alpaca_credentials():
            raise BrokerError(
                "BROKER=alpaca needs ALPACA_KEY_ID and ALPACA_SECRET_KEY in .env "
                "(free paper keys at alpaca.markets)"
            )
        from botcore.brokers.alpaca import AlpacaBroker

        broker = AlpacaBroker(settings, paper=(mode == "paper" or settings.alpaca_paper))
        return Execution(broker, broker._quotes, mode, needs_price_feed=False)

    if broker_kind == "robinhood_crypto":
        raise NotImplementedError("robinhood_crypto broker lands in Phase 4")
    if broker_kind == "robinhood_mcp":
        raise NotImplementedError("robinhood_mcp broker lands in Phase 4")

    raise ValueError(f"unknown broker {broker_kind!r}")
