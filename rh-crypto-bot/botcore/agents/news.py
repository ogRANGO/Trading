"""News agent — free RSS headlines -> crude sentiment -> a small directional vote.

Two scorers:
  * ``lexicon`` (default, free, deterministic) — a hand-rolled finance bull/bear
    word list with recency weighting.
  * ``llm`` (opt-in — needs ``pip install anthropic`` + ``ANTHROPIC_API_KEY``;
    costs credits) — asks Claude for a sentiment per symbol; logged to
    ``llm_decisions``.

Polls at most every ``poll_minutes``; between polls it re-uses the last score.
This agent is experimental and starts at a small weight — free news is thin and
laggy, and it is designed to be one of the first agents killed if it does not earn.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

from botcore.agents.base import Agent, AgentContext, AgentSignal
from botcore.config import AgentCfg
from botcore.data.news import Headline, fetch_headlines

log = logging.getLogger(__name__)

_BULL = {
    "beat", "beats", "surge", "surges", "soar", "soars", "rally", "rallies", "jump",
    "jumps", "record", "upgrade", "upgraded", "outperform", "buy", "bullish", "gain",
    "gains", "profit", "growth", "strong", "raises", "raised", "tops", "approval",
    "wins", "partnership", "expands", "breakthrough", "adoption", "inflows",
}
_BEAR = {
    "miss", "misses", "plunge", "plunges", "slump", "slumps", "tumble", "tumbles",
    "drop", "drops", "fall", "falls", "downgrade", "downgraded", "underperform",
    "sell", "bearish", "loss", "losses", "weak", "cuts", "cut", "lawsuit", "probe",
    "investigation", "recall", "halt", "halts", "bankruptcy", "hack", "hacked",
    "exploit", "outflows", "ban", "warns", "warning", "layoffs", "fraud",
}
_ENTER_THRESH = 0.30


def _score_headlines(items: List[Headline], now: float, half_life_h: float = 12.0) -> float:
    if not items:
        return 0.0
    num = den = 0.0
    for h in items:
        toks = {w.strip(".,!?:;'\"()").lower() for w in (h.title + " " + h.summary).split()}
        s = len(toks & _BULL) - len(toks & _BEAR)
        if s == 0:
            continue
        age_h = max((now - h.ts) / 3600.0, 0.0)
        w = 0.5 ** (age_h / half_life_h)
        num += w * (1 if s > 0 else -1) * min(abs(s), 3) / 3.0
        den += w
    return num / den if den else 0.0


class NewsAgent(Agent):
    id = "news"
    kind = "news"

    def __init__(self, cfg: Optional[AgentCfg] = None) -> None:
        self.cfg = cfg or AgentCfg(weight=0.4, poll_minutes=60, engine="lexicon")
        self.poll_secs = max(self.cfg.poll_minutes, 1) * 60
        self._scores: Dict[str, float] = {}
        self._last_poll = 0.0

    def signals(self, ctx: AgentContext) -> List[AgentSignal]:
        if not self._last_poll or ctx.now - self._last_poll >= self.poll_secs:
            self._refresh(ctx)
            self._last_poll = ctx.now
        out: List[AgentSignal] = []
        for sym in ctx.universe:
            s = self._scores.get(sym, 0.0)
            if s >= _ENTER_THRESH:
                out.append(AgentSignal(sym, 1, min(abs(s), 1.0),
                                       f"news: bullish sentiment {s:+.2f}", ttl_secs=6 * 3600))
            elif s <= -_ENTER_THRESH:
                out.append(AgentSignal(sym, -1, min(abs(s), 1.0),
                                       f"news: bearish sentiment {s:+.2f}", ttl_secs=6 * 3600))
        return out

    def _refresh(self, ctx: AgentContext) -> None:
        since = ctx.now - 36 * 3600
        by_sym: Dict[str, List[Headline]] = {}
        deadline = time.monotonic() + 12.0          # whole refresh budget
        for sym in ctx.universe:
            if time.monotonic() > deadline:
                by_sym.setdefault(sym, [])
                continue
            try:
                by_sym[sym] = fetch_headlines(sym, since, ctx.settings, timeout=5.0)
            except Exception:  # noqa: BLE001
                by_sym[sym] = []
        if self.cfg.engine == "llm":
            scored = self._score_llm(by_sym, ctx)
            if scored is not None:
                self._scores = scored
                return
        self._scores = {sym: _score_headlines(items, ctx.now) for sym, items in by_sym.items()}

    def _score_llm(self, by_sym: Dict[str, List[Headline]], ctx: AgentContext):
        try:
            import anthropic  # noqa: F401
        except Exception:  # noqa: BLE001
            log.warning("news engine=llm but 'anthropic' not installed; using lexicon")
            return None
        key = getattr(ctx.settings, "anthropic_api_key", "")
        if not key:
            log.warning("news engine=llm but ANTHROPIC_API_KEY unset; using lexicon")
            return None
        try:
            import json

            from anthropic import Anthropic

            lines = []
            for sym, items in by_sym.items():
                for h in items[:6]:
                    lines.append(f"[{sym}] {h.title}")
            if not lines:
                return {s: 0.0 for s in by_sym}
            prompt = (
                "You score financial-news sentiment. For each ticker return a number "
                "in [-1,1] (bearish..bullish) as JSON {\"TICKER\": number}. Headlines:\n"
                + "\n".join(lines[:60])
                + f"\n\n{self.brief()}\nRespond with JSON only."
            )
            client = Anthropic(api_key=key)
            msg = client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=400,
                                         messages=[{"role": "user", "content": prompt}])
            txt = msg.content[0].text.strip()
            data = json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
            return {s: float(data.get(s, 0.0)) for s in by_sym}
        except Exception:  # noqa: BLE001
            log.exception("news llm scoring failed; using lexicon")
            return None

    def brief(self) -> str:
        return ("news: I trade on the sentiment of recent headlines. If my shadow "
                "P&L falls past the floor I am permanently disabled and not "
                "revived automatically.")
