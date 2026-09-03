"""Flow agent — crypto perp funding + open interest.

Extreme positive funding = crowded longs -> a mild ``-1`` (fade). Funding flips
negative while price holds above its short EMA -> ``+1``. Rising OI + price
confirms the direction. Crypto-only, off by default, small weight — the funding
edge is widely watched. Designed to be an early kill candidate.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from botcore.agents.base import Agent, AgentContext, AgentSignal
from botcore.config import AgentCfg
from botcore.data.flow import FundingSnapshot, fetch_flow
from botcore.strategy import indicators as ind

log = logging.getLogger(__name__)

_HOT_FUNDING = 0.0004       # per-8h; ~0.04% is already crowded
_COOL_FUNDING = -0.0001


class FlowAgent(Agent):
    id = "flow"
    kind = "flow"
    asset_classes = frozenset({"crypto"})

    def __init__(self, cfg: Optional[AgentCfg] = None) -> None:
        self.cfg = cfg or AgentCfg(enabled=False, weight=0.3, asset_classes=["crypto"], poll_minutes=15)
        self.poll_secs = max(self.cfg.poll_minutes, 1) * 60
        self._snap: Dict[str, FundingSnapshot] = {}
        self._last_poll = 0.0

    def signals(self, ctx: AgentContext) -> List[AgentSignal]:
        if ctx.klass != "crypto":
            return []
        if not self._last_poll or ctx.now - self._last_poll >= self.poll_secs:
            for sym in ctx.universe:
                try:
                    s = fetch_flow(sym)
                except Exception:  # noqa: BLE001
                    s = None
                if s is not None:
                    self._snap[sym] = s
            self._last_poll = ctx.now

        out: List[AgentSignal] = []
        for sym in ctx.universe:
            snap = self._snap.get(sym)
            df = ctx.bars.get(sym)
            if snap is None or df is None or len(df) < 40:
                continue
            close = df["close"]
            ema20 = float(ind.ema(close, 20).iloc[-1])
            last = float(close.iloc[-1])
            fr = snap.funding_rate
            if fr >= _HOT_FUNDING:
                conv = min((fr - _HOT_FUNDING) / _HOT_FUNDING + 0.4, 1.0)
                out.append(AgentSignal(sym, -1, conv,
                                       f"flow: funding hot {fr*100:.3f}% (crowded longs)"))
            elif fr <= _COOL_FUNDING and last > ema20:
                conv = 0.5 + min(snap.oi_change_pct / 20.0, 0.4) if snap.oi_change_pct > 0 else 0.4
                out.append(AgentSignal(sym, 1, conv,
                                       f"flow: funding {fr*100:.3f}%, price holding, OI {snap.oi_change_pct:+.1f}%"))
        return out

    def brief(self) -> str:
        return ("flow: I fade crowded funding and follow funding flips on crypto perps. "
                "Disabled permanently if my shadow P&L falls past the floor.")
