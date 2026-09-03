"""Per-position exit plans: hard stop, profit target, ATR trailing stop, time stop.

Used identically by the backtester and the live engine. The live dashboard also
uses :func:`assess` to render the exit-plan status column from your screenshot.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from botcore.config import ExitCfg
from botcore.risk.guards import past_et_time


@dataclass
class ExitPlan:
    entry_price: float
    atr_at_entry: float
    opened_index: int
    hard_stop: float
    target: Optional[float]
    trail_stop: Optional[float]
    time_stop_index: Optional[int]
    high_water: float
    opened_ts: float = field(default_factory=time.time)
    entry_fee: float = 0.0            # commission paid on entry; kept so restarts don't lose it
    # Partial take-profit. tp1_done flips once and only once; the remainder then
    # runs to the trail/target/BoS exit with the stop lifted to breakeven.
    tp1: Optional[float] = None
    tp1_fraction: float = 0.0
    tp1_done: bool = False
    be_after_tp1: bool = True

    def as_dict(self) -> dict:
        return {
            "entry_price": self.entry_price,
            "atr_at_entry": self.atr_at_entry,
            "opened_index": self.opened_index,
            "hard_stop": self.hard_stop,
            "target": self.target,
            "trail_stop": self.trail_stop,
            "effective_stop": self.effective_stop,
            "time_stop_index": self.time_stop_index,
            "high_water": self.high_water,
            "opened_ts": self.opened_ts,
            "entry_fee": self.entry_fee,
            "tp1": self.tp1,
            "tp1_fraction": self.tp1_fraction,
            "tp1_done": self.tp1_done,
            "be_after_tp1": self.be_after_tp1,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExitPlan":
        return cls(
            entry_price=d["entry_price"], atr_at_entry=d["atr_at_entry"],
            opened_index=int(d.get("opened_index", 0)), hard_stop=d["hard_stop"],
            target=d.get("target"), trail_stop=d.get("trail_stop"),
            time_stop_index=d.get("time_stop_index"), high_water=d["high_water"],
            opened_ts=d.get("opened_ts", time.time()),
            entry_fee=d.get("entry_fee", 0.0),
            # defaults keep plans persisted before TP1 existed loadable
            tp1=d.get("tp1"), tp1_fraction=d.get("tp1_fraction", 0.0),
            tp1_done=bool(d.get("tp1_done", False)),
            be_after_tp1=bool(d.get("be_after_tp1", True)),
        )

    def elapsed_bars(self, now: float, bar_seconds: float) -> int:
        return self.opened_index + int(max(now - self.opened_ts, 0) / max(bar_seconds, 1))

    @property
    def effective_stop(self) -> float:
        """The stop actually in force = the higher of hard stop and trail stop."""
        if self.trail_stop is None:
            return self.hard_stop
        return max(self.hard_stop, self.trail_stop)


def build_plan(
    entry_price: float,
    atr: float,
    opened_index: int,
    cfg: ExitCfg,
    *,
    stop: Optional[float] = None,
    target: Optional[float] = None,
    tp1: Optional[float] = None,
) -> ExitPlan:
    """Build the plan for a new position.

    ``stop``/``target``/``tp1`` are the level-based overrides emitted by the SMC
    family. When they are absent the ATR multiples are used exactly as before,
    so trend and mean_reversion behave identically to the pre-TP1 code.
    """
    atr = max(atr, entry_price * 1e-4)  # guard against zero ATR

    hard = entry_price - cfg.hard_stop_atr_mult * atr
    if stop is not None and stop > 0 and stop < entry_price:
        hard = stop                      # the OB wick, not an ATR multiple

    fixed_target = entry_price + cfg.target_atr_mult * atr if cfg.target_atr_mult > 0 else None
    if target is not None and target > entry_price:
        fixed_target = target

    tp1_level = tp1 if (tp1 is not None and tp1 > entry_price) else None
    tp1_fraction = cfg.tp1_fraction if tp1_level is not None else 0.0

    time_idx = opened_index + cfg.time_stop_bars if cfg.time_stop_bars > 0 else None
    return ExitPlan(
        entry_price=entry_price, atr_at_entry=atr, opened_index=opened_index,
        hard_stop=hard, target=fixed_target, trail_stop=None, time_stop_index=time_idx,
        high_water=entry_price,
        tp1=tp1_level, tp1_fraction=tp1_fraction, tp1_done=False,
        be_after_tp1=cfg.be_after_tp1,
    )


def take_tp1(plan: ExitPlan) -> ExitPlan:
    """Mark TP1 as taken and lift the stop to breakeven if configured.

    Called by whoever actually executed the partial sell, so the flag only flips
    once the fill exists -- never speculatively inside check_exit.
    """
    plan.tp1_done = True
    if plan.be_after_tp1:
        plan.hard_stop = max(plan.hard_stop, plan.entry_price)
    return plan


def update_trail(plan: ExitPlan, bar_high: float, atr_now: float, cfg: ExitCfg) -> ExitPlan:
    """Ratchet the trailing stop up as price makes new highs. Never lowers it."""
    if bar_high > plan.high_water:
        plan.high_water = bar_high
    if cfg.trail_atr_mult > 0:
        candidate = plan.high_water - cfg.trail_atr_mult * max(atr_now, plan.atr_at_entry * 0.25)
        plan.trail_stop = candidate if plan.trail_stop is None else max(plan.trail_stop, candidate)
    return plan


def check_exit(
    plan: ExitPlan,
    bar_low: float,
    bar_high: float,
    bar_index: int,
    *,
    cfg: Optional[ExitCfg] = None,
    now: Optional[datetime] = None,
) -> Optional[Tuple[str, Optional[float]]]:
    """Return ``(reason, fill_price)`` if something should be sold on this bar.

    Priority: flat-by-close, hard stop, TP1, trailing stop, target, time stop.
    A ``None`` fill price means "fill at market".

    ``"tp1"`` is a *partial* exit -- the only reason here that does not close the
    whole position. It is returned at most once per plan, because the caller
    flips ``tp1_done`` via :func:`take_tp1` once the fill exists.

    Flat-by-close outranks everything: an intraday strategy that holds overnight
    is no longer the strategy that was sized and backtested.
    """
    if cfg is not None and cfg.flat_by_et and now is not None:
        if past_et_time(now, cfg.flat_by_et):
            return "flat_close", None

    if bar_low <= plan.hard_stop:
        return "hard_stop", plan.hard_stop
    if (not plan.tp1_done and plan.tp1 is not None
            and plan.tp1_fraction > 0 and bar_high >= plan.tp1):
        return "tp1", plan.tp1
    if plan.trail_stop is not None and bar_low <= plan.trail_stop:
        return "trail_stop", plan.trail_stop
    if plan.target is not None and bar_high >= plan.target:
        return "target", plan.target
    if plan.time_stop_index is not None and bar_index >= plan.time_stop_index:
        return "time_stop", None  # engine fills at market
    return None


def entries_allowed(cfg: ExitCfg, now: Optional[datetime]) -> bool:
    """False once past ``entry_cutoff_et`` — no new risk into the close."""
    if cfg.entry_cutoff_et is None or now is None:
        return True
    return not past_et_time(now, cfg.entry_cutoff_et)


# --------------------------------------------------------------------------- #
# dashboard-facing assessment
# --------------------------------------------------------------------------- #
@dataclass
class ExitAssessment:
    status: str            # "OK" | "MISSING_STOP" | "INVALID" | "NO_PLAN"
    issues: List[str]
    stop_distance_pct: Optional[float]
    to_target_pct: Optional[float]


def assess(entry_price: float, current_price: float, plan: Optional[ExitPlan]) -> ExitAssessment:
    issues: List[str] = []
    if plan is None:
        return ExitAssessment("NO_PLAN", ["no exit plan attached"], None, None)

    stop = plan.effective_stop
    if stop is None or stop <= 0:
        issues.append("missing/invalid stop")
    elif stop >= current_price:
        issues.append("stop is at or above current price")
    if plan.target is not None and plan.target <= current_price and plan.target <= entry_price:
        issues.append("target below entry")

    stop_dist = (current_price - stop) / current_price if stop and current_price else None
    to_target = (
        (plan.target - current_price) / current_price
        if plan.target and current_price else None
    )

    if not issues:
        status = "OK"
    elif any("stop" in i for i in issues):
        status = "MISSING_STOP"
    else:
        status = "INVALID"
    return ExitAssessment(status, issues, stop_dist, to_target)
