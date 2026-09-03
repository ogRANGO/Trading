"""Hard pre-trade risk checks + circuit breakers.

The strategy proposes orders; :class:`RiskEngine` is the last gate before the
broker. It can reject an order, shrink it, or HALT the whole bot. It is
deliberately dumb and strict - no cleverness, just bounds.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from botcore.brokers.base import Account, OrderRequest, Position, Quote
from botcore.config import RiskCfg, Settings
from botcore.data.base import asset_class
from botcore.risk.guards import PDTGuard, is_market_open
from botcore.risk.killswitch import KillSwitch


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = "ok"
    adjusted_qty: Optional[float] = None  # set when the order was shrunk

    @property
    def blocked(self) -> bool:
        return not self.allowed


@dataclass
class RiskState:
    peak_equity: float = 0.0
    day_start_equity: float = 0.0
    day_realized_pnl: float = 0.0
    consecutive_losses: int = 0
    cooldown_until: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    session_date: str = ""
    order_times: List[float] = field(default_factory=list)


class RiskEngine:
    def __init__(
        self,
        cfg: RiskCfg,
        settings: Settings,
        *,
        kill_switch: Optional[KillSwitch] = None,
        pdt: Optional[PDTGuard] = None,
    ) -> None:
        self.cfg = cfg
        self.s = settings
        self.kill = kill_switch or KillSwitch(settings.db_path.replace("bot.db", "HALT"))
        self.pdt = pdt or PDTGuard(cfg.pdt_min_equity_usd, cfg.pdt_max_day_trades)
        self.state = RiskState()

    # -- lifecycle updates ------------------------------------------------- #
    def update_equity(self, equity: float, now: Optional[float] = None) -> Optional[str]:
        now = now or time.time()
        st = self.state
        if st.peak_equity == 0.0:
            st.peak_equity = equity
            st.day_start_equity = equity
        st.peak_equity = max(st.peak_equity, equity)

        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != st.session_date:
            st.session_date = day
            st.day_start_equity = equity
            st.day_realized_pnl = 0.0

        dd = equity / st.peak_equity - 1.0 if st.peak_equity else 0.0
        if dd <= -abs(self.cfg.max_drawdown_pct):
            return self.halt(f"max drawdown breached: {dd * 100:.1f}%")

        day_dd = (equity - st.day_start_equity) / st.day_start_equity if st.day_start_equity else 0.0
        if day_dd <= -abs(self.cfg.daily_loss_limit_pct):
            st.cooldown_until = max(st.cooldown_until, _end_of_utc_day(now))
        return None

    def on_trade_closed(self, pnl: float, *, was_day_trade: bool = False, when=None) -> None:
        st = self.state
        st.day_realized_pnl += pnl
        if pnl < 0:
            st.consecutive_losses += 1
            if st.consecutive_losses >= self.cfg.consecutive_loss_limit:
                st.cooldown_until = max(
                    st.cooldown_until, time.time() + self.cfg.cooldown_minutes * 60
                )
        else:
            st.consecutive_losses = 0
        if was_day_trade:
            self.pdt.record_day_trade(when or _now_dt())

    def on_order_submitted(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        cutoff = now - 3600
        self.state.order_times = [t for t in self.state.order_times if t >= cutoff]
        self.state.order_times.append(now)

    def halt(self, reason: str, *, source: str = "risk") -> str:
        self.state.halted = True
        self.state.halt_reason = reason
        self.kill.engage(reason, source=source)
        return reason

    def resume(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
        self.kill.clear()

    # -- the gate -------------------------------------------------------- #
    def pretrade_check(
        self,
        req: OrderRequest,
        *,
        account: Account,
        positions: List[Position],
        quote: Quote,
        now: Optional[float] = None,
    ) -> RiskDecision:
        now = now or time.time()
        st = self.state
        klass = asset_class(req.symbol)

        # Exits reduce risk and must never be blocked -- stops keep protecting the
        # book even during a halt/cooldown. FLATTEN is handled by the engine.
        if req.side == "sell":
            return RiskDecision(True)

        if self.kill.engaged or st.halted:
            return RiskDecision(False, f"halted: {st.halt_reason or 'HALT file present'}")
        if now < st.cooldown_until:
            mins = (st.cooldown_until - now) / 60
            return RiskDecision(False, f"cooldown active ({mins:.0f} min left)")

        if len(st.order_times) >= self.cfg.max_orders_per_hour:
            return RiskDecision(False, f"order-rate cap ({self.cfg.max_orders_per_hour}/h)")

        if req.side == "buy" and klass == "equity" and self.cfg.equities_rth_only:
            if not is_market_open("equity", _dt(now)):
                return RiskDecision(False, "equities: market closed")

        # quote sanity
        if quote.mid <= 0:
            return RiskDecision(False, "no valid quote")
        if quote.spread_pct > self.cfg.max_spread_pct:
            return RiskDecision(False, f"spread {quote.spread_pct * 100:.2f}% too wide")
        if now - quote.ts > self.cfg.stale_quote_seconds:
            return RiskDecision(False, f"stale quote ({now - quote.ts:.0f}s old)")

        pos_by_sym: Dict[str, Position] = {p.symbol: p for p in positions}
        held = [p for p in positions if abs(p.qty) > 1e-12]

        if req.side == "buy":
            ref = quote.ask or quote.mid
            qty = req.qty
            notional = qty * ref

            # per-trade USD cap
            if self.s.max_trade_usd and notional > self.s.max_trade_usd:
                qty = self.s.max_trade_usd / ref
                notional = qty * ref

            # live total notional ceiling
            if self.s.bot_mode == "live":
                gross = sum(p.market_value for p in held) + notional
                if self.s.live_max_usd <= 0:
                    return RiskDecision(False, "live mode but LIVE_MAX_USD is 0")
                if gross > self.s.live_max_usd:
                    room = self.s.live_max_usd - sum(p.market_value for p in held)
                    if room <= 1:
                        return RiskDecision(False, "LIVE_MAX_USD reached")
                    qty = room / ref
                    notional = qty * ref

            # position count
            if req.symbol not in pos_by_sym and len(held) >= self.cfg.max_concurrent_positions:
                return RiskDecision(False, f"max positions ({self.cfg.max_concurrent_positions})")

            # per-name weight
            equity = account.equity or 1.0
            existing = pos_by_sym[req.symbol].market_value if req.symbol in pos_by_sym else 0.0
            if (existing + notional) / equity > self.cfg.max_position_weight + 1e-9:
                cap = self.cfg.max_position_weight * equity - existing
                if cap <= 1:
                    return RiskDecision(False, "per-name weight cap reached")
                qty = cap / ref
                notional = qty * ref

            # total exposure
            gross_after = sum(p.market_value for p in held if p.symbol != req.symbol) + existing + notional
            if gross_after / equity > self.cfg.max_total_exposure_pct + 1e-9:
                return RiskDecision(False, f"total exposure cap ({self.cfg.max_total_exposure_pct:.0%})")

            # buying power
            if notional > account.buying_power + 1e-6:
                qty = max(account.buying_power / ref, 0.0)
                notional = qty * ref
                if qty <= 0:
                    return RiskDecision(False, "no buying power")

        if qty < req.qty - 1e-12:
            return RiskDecision(True, "order shrunk to fit limits", adjusted_qty=_floor_qty(req.symbol, qty))
        return RiskDecision(True)


def deposit_floor_breached(equity: float, initial_equity: float, floor_pct: float = 0.0) -> bool:
    """True when mark-to-market ``equity`` has fallen to/through the deposit floor.

    ``floor_pct`` 0.0   -> breach at ``equity <= initial_equity``;
    ``floor_pct`` -0.05 -> a 5% grace band (breach at 95% of the deposit).
    Returns ``False`` when the anchor is not set (``initial_equity <= 0``).
    """
    if initial_equity <= 0.0:
        return False
    return equity <= initial_equity * (1.0 + floor_pct)


def _floor_qty(symbol: str, qty: float) -> float:
    step = 1e-8 if asset_class(symbol) == "crypto" else 1e-3
    return float(int(qty / step) * step)


def _end_of_utc_day(now: float) -> float:
    import math
    return (math.floor(now / 86400) + 1) * 86400


def _now_dt():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _dt(ts: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc)
