"""Plain dataclasses passed between fetch -> assemble -> render.

Everything here is inert data. No network, no I/O, no trading imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# source-level payloads
# --------------------------------------------------------------------------- #
@dataclass
class SourceResult:
    """What one fetcher returns. ``ok=False`` degrades exactly one lane."""

    name: str
    ok: bool
    detail: str = ""
    payload: Any = None


@dataclass
class TapeQuote:
    symbol: str
    label: str
    group: str                       # us_futures | rates_fx | commodities | volatility | asia_europe | crypto
    price: Optional[float] = None
    prev_close: Optional[float] = None
    change_pct: Optional[float] = None


@dataclass
class NewsItem:
    headline: str
    url: str
    source: str
    published: datetime              # tz-aware UTC
    entities: list[str] = field(default_factory=list)
    summary: str = ""
    echo_count: int = 1
    kind: str = "headline"           # headline | analyst | tail

    def key(self) -> str:
        return (self.headline or "").strip().lower()


@dataclass
class Filing:
    entity: str
    form: str                        # "8-K" | "4" | ...
    filed: datetime                  # tz-aware UTC
    url: str
    items: list[str] = field(default_factory=list)   # 8-K item codes
    note: str = ""


@dataclass
class ScheduledEvent:
    label: str
    date: date
    kind: str                        # econ | earnings | fomc | opex | triple_witching | rebalance | russell | index
    when: str = ""                   # "08:30 ET" | "BMO" | "AMC" | "at close"
    entity: Optional[str] = None
    impact: Optional[str] = None     # High | Medium | Low
    country: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None
    actual: Optional[str] = None
    surprise: Optional[str] = None   # rendered string, e.g. "+0.2 vs 2.9% exp"


@dataclass
class Position:
    ticker: str
    bot: str                         # stock | meme
    unrealized_pct: Optional[float] = None
    # full-detail only — stripped from the public web payload:
    shares: Optional[float] = None
    entry_price: Optional[float] = None
    current_price: Optional[float] = None


@dataclass
class CryptoState:
    btc_dominance_pct: Optional[float] = None
    total_mcap_usd: Optional[float] = None
    total_mcap_change_pct: Optional[float] = None
    btc_price: Optional[float] = None
    btc_change_pct: Optional[float] = None
    eth_price: Optional[float] = None
    eth_change_pct: Optional[float] = None
    btc_funding_pct: Optional[float] = None
    eth_funding_pct: Optional[float] = None


# --------------------------------------------------------------------------- #
# assembled brief
# --------------------------------------------------------------------------- #
@dataclass
class BriefItem:
    """One rendered/logged line. assemble.py produces these; render + store consume."""

    lane: str                        # what_matters|tape|crypto|book|watchlist|scheduled|filings|headlines|tail
    tier: int                        # 0..3 proximity
    tag: str                         # SCHEDULED | SURPRISE | TAPE
    text: str
    entity: Optional[str] = None
    source: str = ""
    published_utc: Optional[str] = None
    echo_count: int = 1
    score: float = 0.0
    surfaced: bool = True
    payload: dict = field(default_factory=dict)


@dataclass
class Brief:
    generated_at: datetime
    slot: str                        # overnight | premarket | weekend
    session: date
    session_type: str                # full | half | holiday | weekend
    run_id: str

    what_matters: list[str] = field(default_factory=list)
    tape: list[TapeQuote] = field(default_factory=list)
    crypto: CryptoState = field(default_factory=CryptoState)
    positions: list[Position] = field(default_factory=list)
    scheduled: list[ScheduledEvent] = field(default_factory=list)
    filings: list[Filing] = field(default_factory=list)
    headlines: list[NewsItem] = field(default_factory=list)
    watchlist: list[NewsItem] = field(default_factory=list)
    tail: list[NewsItem] = field(default_factory=list)

    items: list[BriefItem] = field(default_factory=list)          # everything, incl. surfaced=False
    sources: list[SourceResult] = field(default_factory=list)
    universes: dict[str, list[str]] = field(default_factory=dict)  # bot -> tickers
    price_snapshot: dict[str, dict] = field(default_factory=dict)  # entity -> {ref_price, prev_close, premarket}

    def source_line(self) -> str:
        ok = [s for s in self.sources if s.ok]
        bad = [s for s in self.sources if not s.ok]
        line = f"sources {len(ok)}/{len(self.sources)}"
        if bad:
            line += " -- degraded: " + ", ".join(f"{s.name} ({s.detail})" for s in bad)
        return line
