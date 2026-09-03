"""The CHIEF: blend the enabled agents into one target book.

``decide()`` collects each agent's raw signals, records them, advances every
agent's shadow P&L, disables any agent that has lost too much, then blends the
survivors into a per-symbol :class:`NetSignal`. ``to_series()`` adapts that to the
``{symbol: pd.Series}`` shape :meth:`PortfolioManager.plan` already consumes, so
the portfolio / entry / exit code is untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import pandas as pd

from botcore.agents.base import Agent, AgentContext, AgentSignal
from botcore.agents.ledger import AgentLedger
from botcore.config import CoordinatorCfg
from botcore.strategy import indicators as ind

log = logging.getLogger(__name__)


@dataclass
class NetSignal:
    symbol: str
    enter: bool = False
    veto: bool = False                        # exit if held / block new entries
    score: float = 0.0                        # weighted net conviction (>=0 for entries)
    contributors: Dict[str, float] = field(default_factory=dict)   # agent_id -> weighted vote


class Coordinator:
    def __init__(self, agents: List[Agent], weights: Dict[str, float],
                 cfg: CoordinatorCfg, ledger: AgentLedger,
                 event: Optional[Callable[[str, str, str], None]] = None) -> None:
        self.agents = agents
        self.weights = weights
        self.cfg = cfg
        self.ledger = ledger
        self._event = event or (lambda *a: None)
        self._last_signals: Dict[str, AgentSignal] = {}   # agent_id -> most recent (any symbol)

    # ------------------------------------------------------------------ #
    def active_agents(self, klass: str) -> List[Agent]:
        return [a for a in self.agents
                if self.weights.get(a.id, 0) > 0
                and a.applies_to(klass)
                and not self.ledger.is_dead(a.id)]

    def decide(self, ctx: AgentContext) -> Dict[str, NetSignal]:
        per_agent: Dict[str, List[AgentSignal]] = {}
        for agent in self.active_agents(ctx.klass):
            try:
                sigs = agent.signals(ctx) or []
            except Exception:  # noqa: BLE001 - one bad agent must not stop the tick
                log.exception("agent %s signals() failed", agent.id)
                sigs = []
            per_agent[agent.id] = sigs
            for s in sigs:
                self.ledger.record_signal(agent.id, s, ctx)
                self._last_signals[agent.id] = s

        # advance shadow books + disable losers
        self.ledger.tick(ctx, per_agent)
        for aid, reason in self.ledger.check_kills(ctx):
            self._event("warn", "agent-kill", reason)

        return self._blend(ctx, per_agent)

    def _blend(self, ctx: AgentContext, per_agent: Dict[str, List[AgentSignal]]) -> Dict[str, NetSignal]:
        by_symbol: Dict[str, List[tuple]] = {s: [] for s in ctx.universe}
        for aid, sigs in per_agent.items():
            w = self.weights.get(aid, 0.0)
            for s in sigs:
                if s.symbol in by_symbol:
                    by_symbol[s.symbol].append((aid, w, s))

        out: Dict[str, NetSignal] = {}
        c = self.cfg
        for sym, votes in by_symbol.items():
            if not votes:
                continue
            longs = [(aid, w * s.conviction) for aid, w, s in votes if s.direction > 0]
            vetoes = [(aid, w * s.conviction) for aid, w, s in votes
                      if s.direction < 0 and s.conviction >= c.veto_conviction]
            neg = sum(w * s.conviction for _, w, s in votes if s.direction < 0)
            net = sum(v for _, v in longs) - neg
            enter = (len(longs) >= c.min_agents_agree and net >= c.min_net_conviction and not vetoes)
            out[sym] = NetSignal(
                symbol=sym, enter=enter, veto=bool(vetoes),
                score=max(net, 0.0), contributors={aid: v for aid, v in longs},
            )
        return out

    # ------------------------------------------------------------------ #
    def to_series(self, net: Dict[str, NetSignal], bars: Dict[str, pd.DataFrame]) -> Dict[str, pd.Series]:
        out: Dict[str, pd.Series] = {}
        for sym, df in bars.items():
            if df is None or len(df) < 60:
                continue
            n = net.get(sym)
            a = ind.atr(df["high"], df["low"], df["close"], 14).dropna()
            out[sym] = pd.Series({
                "entry": bool(n and n.enter),
                "hold": bool(n and n.enter),
                "exit": bool(n and n.veto),
                "score": float(n.score) if n else 0.0,
                "atr": float(a.iloc[-1]) if len(a) else 0.0,
                "close": float(df["close"].iloc[-1]),
            })
        return out
