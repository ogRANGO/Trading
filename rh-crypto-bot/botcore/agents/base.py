"""Agent interface + the per-tick context passed to every agent."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Set

import pandas as pd

from botcore.brokers.base import Position, Quote
from botcore.config import Settings


@dataclass
class AgentSignal:
    """One agent's read on one symbol, this tick.

    ``direction`` in this long-only system:
        +1  enter / stay long
         0  no opinion
        -1  veto — exit if held, block new entries
    ``conviction`` 0..1 scales the vote and (for -1) the veto weight.
    """

    symbol: str
    direction: int
    conviction: float
    reason: str = ""
    ttl_secs: float = 0.0          # 0 = until superseded next tick

    def __post_init__(self) -> None:
        self.direction = max(-1, min(1, int(self.direction)))
        self.conviction = max(0.0, min(1.0, float(self.conviction)))


@dataclass
class AgentContext:
    bars: Dict[str, pd.DataFrame]          # cleaned OHLCV per symbol
    quotes: Dict[str, Quote]
    positions: Dict[str, Position]         # the coordinator's real book
    equity: float
    universe: List[str]
    now: float
    conn: sqlite3.Connection               # read-only use (cache / events)
    settings: Settings
    klass: str = "crypto"                  # dominant asset class of the universe


class Agent:
    """Base class. Subclasses set ``id`` / ``kind`` / ``asset_classes`` and
    implement :meth:`signals`."""

    id: str = "agent"
    kind: str = "technical"               # technical | news | flow | onchain
    asset_classes: Set[str] = frozenset({"equity", "crypto"})

    def signals(self, ctx: AgentContext) -> List[AgentSignal]:  # pragma: no cover
        raise NotImplementedError

    def brief(self) -> str:
        """What the agent 'knows' — its rules, and (filled in by the ledger) its
        shadow P&L and distance to being disabled. Shown on the dashboard; for an
        LLM-backed agent it is injected into the prompt."""
        return ""

    def applies_to(self, klass: str) -> bool:
        return klass in self.asset_classes
