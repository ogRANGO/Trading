"""Technical agents — pure price/volume math, no external data.

``TrendAgent`` / ``MeanReversionAgent`` wrap the existing signal families.
``MomentumAgent`` is a Donchian-breakout + rate-of-change agent.
``VolRegimeAgent`` is a *veto* agent: it never opens a position, it only shouts
"-1, get out / stay out" when realised vol spikes or the regime rolls over.
"""

from __future__ import annotations

from typing import List

import pandas as pd

from botcore.agents.base import Agent, AgentContext, AgentSignal
from botcore.config import SignalParams
from botcore.strategy import indicators as ind
from botcore.strategy.signals import mean_reversion_signals, trend_signals

_MIN_BARS = 60


def _last_atr(df: pd.DataFrame, period: int = 14) -> float:
    a = ind.atr(df["high"], df["low"], df["close"], period).dropna()
    return float(a.iloc[-1]) if len(a) else 0.0


class _FamilyAgent(Agent):
    """Shared wrapper: run a signal family, read the last bar, emit one AgentSignal
    per symbol. entry -> +1, exit -> -1, conviction from the family score."""

    kind = "technical"
    _family = staticmethod(trend_signals)
    _score_scale = 3.0                    # score value that maps to full conviction

    def __init__(self, params: SignalParams) -> None:
        self.params = params

    def signals(self, ctx: AgentContext) -> List[AgentSignal]:
        out: List[AgentSignal] = []
        for sym in ctx.universe:
            df = ctx.bars.get(sym)
            if df is None or len(df) < _MIN_BARS:
                continue
            row = self._family(df, self.params).iloc[-1]
            if bool(row.get("exit", False)):
                out.append(AgentSignal(sym, -1, 0.7, f"{self.id}: exit"))
            elif bool(row.get("entry", False)):
                conv = min(float(row.get("score", 0.0)) / self._score_scale, 1.0)
                out.append(AgentSignal(sym, 1, max(conv, 0.25), f"{self.id}: score={float(row.get('score',0)):.2f}"))
        return out

    def brief(self) -> str:
        return f"{self.id}: acts on the '{self._family.__name__}' family, long-only."


class TrendAgent(_FamilyAgent):
    id = "trend"
    _family = staticmethod(trend_signals)
    _score_scale = 3.0


class MeanReversionAgent(_FamilyAgent):
    id = "mean_reversion"
    _family = staticmethod(mean_reversion_signals)
    _score_scale = 2.0


class MomentumAgent(Agent):
    id = "momentum"
    kind = "technical"

    def __init__(self, lookback: int = 20, roc_period: int = 10, atr_period: int = 14) -> None:
        self.lookback = lookback
        self.roc_period = roc_period
        self.atr_period = atr_period

    def signals(self, ctx: AgentContext) -> List[AgentSignal]:
        out: List[AgentSignal] = []
        for sym in ctx.universe:
            df = ctx.bars.get(sym)
            if df is None or len(df) < max(self.lookback, self.roc_period) + 5:
                continue
            close = df["close"]
            prior_high = close.rolling(self.lookback).max().shift(1).iloc[-1]
            last = float(close.iloc[-1])
            roc = last / float(close.iloc[-1 - self.roc_period]) - 1.0
            atr = _last_atr(df, self.atr_period)
            if atr <= 0:
                continue
            if last > prior_high and roc > 0:
                conv = min(roc / (3.0 * atr / last), 1.0) if last else 0.0
                out.append(AgentSignal(sym, 1, max(conv, 0.3),
                                       f"momentum: {self.lookback}-bar breakout, roc={roc*100:.1f}%"))
            elif roc < -0.5 * (atr / last):
                out.append(AgentSignal(sym, -1, 0.5, f"momentum: roc={roc*100:.1f}% rolling over"))
        return out

    def brief(self) -> str:
        return (f"momentum: enter on a {self.lookback}-bar Donchian breakout with positive "
                f"{self.roc_period}-bar ROC; -1 when ROC rolls over.")


class VolRegimeAgent(Agent):
    id = "vol_regime"
    kind = "technical"

    def __init__(self, vol_period: int = 20, spike_mult: float = 1.8,
                 ema_regime: int = 200, regime_lag: int = 20) -> None:
        self.vol_period = vol_period
        self.spike_mult = spike_mult
        self.ema_regime = ema_regime
        self.regime_lag = regime_lag

    def signals(self, ctx: AgentContext) -> List[AgentSignal]:
        risk_off, reasons = False, []
        for sym in ctx.universe:
            df = ctx.bars.get(sym)
            if df is None or len(df) < self.ema_regime + self.regime_lag:
                continue
            close = df["close"]
            v = ind.annualized_vol(close, self.vol_period).dropna()
            if len(v) >= self.vol_period:
                cur, med = float(v.iloc[-1]), float(v.tail(self.vol_period * 3).median())
                if med > 0 and cur > self.spike_mult * med:
                    risk_off = True
                    reasons.append(f"{sym} vol {cur/med:.1f}x")
            reg = ind.ema(close, self.ema_regime)
            if float(reg.iloc[-1]) < float(reg.iloc[-1 - self.regime_lag]):
                risk_off = True
                reasons.append(f"{sym} regime down")
        if not risk_off:
            return []
        why = "vol_regime: risk-off (" + ", ".join(reasons[:4]) + ")"
        return [AgentSignal(sym, -1, 0.8, why) for sym in ctx.universe]

    def brief(self) -> str:
        return ("vol_regime: a veto agent. Emits -1 for every symbol when realised vol "
                f"spikes >{self.spike_mult}x its median or the {self.ema_regime}-EMA slopes down. "
                "Never opens a position.")
