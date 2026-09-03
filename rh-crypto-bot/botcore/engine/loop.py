"""TradingEngine: one tick = quotes -> mark -> manage exits -> signals -> entries.

The same signal / portfolio / risk / exit-plan code as the backtester, driven by
live quotes instead of historical bars. Broker is chosen by :mod:`execution.router`
(SimBroker for keyless paper, Alpaca for paper/live).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, Optional

import pandas as pd

from botcore.brokers.base import Account, BrokerError, OrderRequest, Position
from botcore.config import BotConfig, Settings, get_config, get_settings
from botcore.data.base import Timeframe, asset_class, timeframe_seconds
from botcore.data.history import load_history
from botcore.engine.watchdog import watchdog_verdict
from botcore.execution.reconcile import startup_reconcile
from botcore.execution.router import Execution, build_execution
from botcore.notify.push import get_notifier
from botcore.notify.summary import build_daily_summary
from botcore.risk.guards import is_market_open
from botcore.risk.killswitch import DeadSwitch, KillSwitch
from botcore.risk.limits import RiskEngine, deposit_floor_breached
from botcore.store.db import log_event, open_db, set_config_sha
from botcore.store.state import (
    checkpoint_wal,
    delete_position,
    downsample_equity_snapshots,
    get_flag,
    is_paused,
    load_positions,
    prune_events,
    prune_quotes,
    record_order,
    record_trade,
    set_flag,
    snapshot_equity,
    upsert_position,
    upsert_quote,
)
from botcore.strategy import indicators as ind
from botcore.strategy.exitplan import (
    ExitPlan, build_plan, check_exit, entries_allowed, take_tp1, update_trail,
)
from botcore.strategy.portfolio import PortfolioManager
from botcore.strategy.signals import get_signal_fn

log = logging.getLogger(__name__)


class TradingEngine:
    def __init__(
        self,
        settings: Optional[Settings] = None,
        cfg: Optional[BotConfig] = None,
        *,
        execution: Optional[Execution] = None,
        conn=None,
        bars: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.cfg = cfg or get_config()
        self.mode = self.settings.bot_mode
        set_config_sha(self.cfg.config_sha)      # stamp provenance on every event row
        self.conn = conn or open_db(self.settings.db_path)

        self.family = self.cfg.strategy.signal_family
        self.signal_fn = get_signal_fn(self.family, self.cfg.strategy.params)
        self.exit_cfg = self.cfg.portfolio.exit_for(self.family)
        self.pm = PortfolioManager(
            self.cfg.portfolio, max_trade_usd=self.settings.max_trade_usd or None
        )
        halt_path = str(Path(self.settings.db_path).with_name("HALT"))
        self.risk = RiskEngine(self.cfg.risk, self.settings, kill_switch=KillSwitch(halt_path))
        self.dead = DeadSwitch(str(Path(self.settings.db_path).with_name("DEAD")))
        self.initial_equity: Optional[float] = None
        self._kill_floor_strikes = 0

        self.tf = Timeframe.parse(self.cfg.market_data.timeframe)
        self.bar_secs = timeframe_seconds(self.tf)
        self.execution = execution or build_execution(self.settings, self.cfg, conn=self.conn)

        self.notifier = get_notifier(self.settings)
        self.coordinator = None
        if self.cfg.engine.engine_mode == "multi":
            from botcore.agents.registry import build_coordinator

            self.coordinator = build_coordinator(self.cfg, self.conn, self.settings,
                                                 event=self._event)
        self.plans: Dict[str, ExitPlan] = {}
        self._entry_meta: Dict[str, dict] = {}
        self._bars: Dict[str, pd.DataFrame] = {}
        self._bars_ts = 0.0
        self._sched = None
        self._last_closed_notice = 0.0
        self._tick_fail_streak = 0
        self._last_progress_ts = time.time()
        self._wd_stop: Optional[threading.Event] = None
        self._wd_thread: Optional[threading.Thread] = None
        self._wd_conn = None
        self._wd_action = self._watchdog_restart

        self.reconcile_report = startup_reconcile(self.conn, self.execution.broker, self.mode)
        self._seed_initial_equity()
        self._load_plans()
        if bars is not None:
            self._bars = {s: df for s, df in bars.items() if not df.empty}
            self._bars_ts = time.time() + 1e12  # pin: never auto-refresh in tests
        else:
            self._refresh_bars(force=True)

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        from apscheduler.executors.pool import ThreadPoolExecutor
        from apscheduler.schedulers.background import BackgroundScheduler

        self._event("info", "engine", f"starting: mode={self.mode} broker={self.execution.broker.name} "
                                      f"family={self.family} universe={self.cfg.active_universe}")
        # one worker: the tick and the bar-refresh job share self.conn, so they
        # must never run concurrently (see db.connect check_same_thread note).
        self._sched = BackgroundScheduler(timezone="UTC",
                                          executors={"default": ThreadPoolExecutor(1)})
        eng = self.cfg.engine
        self._sched.add_job(self.safe_tick, "interval",
                            seconds=eng.strategy_tick_seconds, id="tick",
                            max_instances=1, coalesce=True, next_run_time=_now_dt())
        self._sched.add_job(self._refresh_bars, "interval",
                            seconds=max(self.bar_secs, 300), id="bars",
                            max_instances=1, coalesce=True, misfire_grace_time=120)
        self._sched.add_job(self._periodic_reconcile, "interval",
                            minutes=max(eng.reconcile_tick_minutes, 1), id="reconcile",
                            max_instances=1, coalesce=True, misfire_grace_time=120)
        self._sched.add_job(self._housekeeping, "cron",
                            hour=eng.housekeeping_utc_hour, minute=17, id="housekeeping",
                            max_instances=1, coalesce=True, misfire_grace_time=3600)
        self._sched.add_job(self._daily_summary, "cron",
                            hour=eng.daily_summary_utc_hour, minute=0, id="daily_summary",
                            max_instances=1, coalesce=True, misfire_grace_time=3600)
        self._sched.start()

        # watchdog: hard-exit a wedged tick loop so launchd relaunches it clean
        self._wd_stop = threading.Event()
        self._wd_conn = open_db(self.settings.db_path)
        self._wd_thread = threading.Thread(target=self._watchdog_loop, name="watchdog", daemon=True)
        self._wd_thread.start()

    def stop(self) -> None:
        if self._wd_stop is not None:
            self._wd_stop.set()
        if self._sched:
            self._sched.shutdown(wait=False)
        if self._wd_conn is not None:
            try:
                self._wd_conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._wd_conn = None
        self.execution.close()
        self._event("info", "engine", "stopped")

    def safe_tick(self) -> None:
        try:
            self.tick()
            self._tick_fail_streak = 0
        except Exception as exc:  # noqa: BLE001
            self._tick_fail_streak += 1
            n = self._tick_fail_streak
            log.exception("tick failed")
            log_event(self.conn, "error", "tick", f"{type(exc).__name__}: {exc}")
            if n == self.cfg.engine.tick_fail_streak_notify:
                self._maybe_notify("error", "tick", f"{n} consecutive tick failures: {exc}")
            if n >= self.cfg.engine.tick_fail_streak_halt and not self.risk.kill.engaged:
                try:
                    self.risk.kill.engage(f"{n} consecutive tick failures", source="engine")
                except Exception:  # noqa: BLE001
                    pass
                self._event("halt", "engine", f"HALT after {n} consecutive tick failures")
        finally:
            self._last_progress_ts = time.time()
            try:
                set_flag(self.conn, "engine_heartbeat", str(time.time()))
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # watchdog / periodic jobs
    # ------------------------------------------------------------------ #
    def _watchdog_loop(self) -> None:
        iv = max(self.cfg.engine.watchdog_check_seconds, 1)
        while not self._wd_stop.wait(iv):
            try:
                verdict = watchdog_verdict(
                    time.time(), self._last_progress_ts,
                    self.cfg.engine.watchdog_stall_seconds,
                    self.risk.kill.engaged, is_paused(self._wd_conn),
                )
            except Exception:  # noqa: BLE001
                continue
            if verdict:
                self._wd_action(verdict)
                return

    def _watchdog_restart(self, reason: str) -> None:
        for fn in (
            lambda: log_event(self._wd_conn, "error", "watchdog", reason + "; hard-exit for restart"),
            lambda: self.risk.kill.engage(reason, source="watchdog"),
            lambda: self.settings.notifies("watchdog") and self.notifier.notify(
                reason, title="BOT WATCHDOG", priority="urgent", tags=["skull"]),
        ):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        os._exit(1)

    def _periodic_reconcile(self) -> None:
        rep = startup_reconcile(self.conn, self.execution.broker, self.mode)
        if not rep.clean:
            self._event("warn", "reconcile",
                        f"periodic drift: adopted={rep.adopted} dropped={rep.dropped}")

    def _housekeeping(self) -> None:
        now = time.time()
        eng = self.cfg.engine
        ev = prune_events(self.conn, now - eng.events_retention_days * 86400)
        qz = prune_quotes(self.conn, now - eng.quotes_retention_days * 86400)
        ds = downsample_equity_snapshots(self.conn, self.mode, now - 365 * 86400)
        checkpoint_wal(self.conn)
        self._event("info", "housekeeping",
                    f"pruned {ev} events / {qz} quotes, downsampled {ds} equity rows; WAL checkpointed")

    def _daily_summary(self) -> None:
        s = build_daily_summary(self.conn, self.mode)
        self._event("info", "summary", s.oneline())

    # ------------------------------------------------------------------ #
    # the tick
    # ------------------------------------------------------------------ #
    def tick(self) -> dict:
        now = time.time()
        out: dict = {"ts": now, "entries": [], "exits": [], "blocked": []}

        if get_flag(self.conn, "flatten_requested") == "1":
            set_flag(self.conn, "flatten_requested", "0")
            self.flatten("dashboard request")
            return {**out, "flattened": True}
        if get_flag(self.conn, "resume_requested") == "1":
            set_flag(self.conn, "resume_requested", "0")
            self.risk.resume()
            self.risk.state.peak_equity = 0.0        # re-baseline; update_equity re-seeds
            self.risk.state.cooldown_until = 0.0
            self._tick_fail_streak = 0
            self._event("info", "resume", "resume requested; risk state cleared, drawdown re-baselined")
        if self.risk.kill.engaged:
            self._notice("halt", "kill switch engaged; managing nothing")
            return {**out, "halted": True}
        if is_paused(self.conn):
            return {**out, "paused": True}

        klass = "crypto" if all(asset_class(s) == "crypto" for s in self.cfg.universe) else "equity"
        if klass == "equity" and not is_market_open("equity", _dt(now)):
            self._notice("closed", "equity market closed; idle")
            return {**out, "market_closed": True}

        self._refresh_bars()
        quotes = self.execution.quotes.get_quotes(self.cfg.universe)
        if not quotes:
            self._event("warn", "quotes", "no quotes this tick")
            return {**out, "no_quotes": True}
        for sym, q in quotes.items():
            upsert_quote(self.conn, sym, q.bid, q.ask, q.ts)

        if self.execution.needs_price_feed:
            self.execution.broker.mark(
                {s: {"open": q.mid, "high": q.mid, "low": q.mid, "close": q.mid}
                 for s, q in quotes.items()},
                clock=now,
            )

        acct = self.execution.broker.get_account()
        if halt := self.risk.update_equity(acct.equity, now):
            self._event("halt", "risk", halt)
            self.flatten(f"risk halt: {halt}")
            return {**out, "halted": True}

        if self._check_kill_floor(acct.equity):
            return {**out, "killed": True}   # unreachable past _kill's os._exit, but explicit

        positions = {p.symbol: p for p in self.execution.broker.get_positions()}
        self._reconcile(positions, quotes, now)

        # 1) manage exits
        for sym, pos in list(positions.items()):
            plan = self.plans.get(sym)
            q = quotes.get(sym)
            if plan is None or q is None:
                continue
            update_trail(plan, max(self._recent_high(sym, q.mid), q.mid), self._recent_atr(sym), self.exit_cfg)
            hit = check_exit(plan, q.mid, q.mid, plan.elapsed_bars(now, self.bar_secs),
                             cfg=self.exit_cfg, now=_dt(now))
            if hit:
                # tp1 is the only partial: sell a slice, keep managing the rest
                part = (plan.tp1_fraction * self._entry_meta.get(sym, {}).get("qty", pos.qty)
                        if hit[0] == "tp1" else None)
                self._exit(sym, pos, q, hit[0], acct, list(positions.values()), qty=part)
                out["exits"].append({"symbol": sym, "reason": hit[0]})
            else:
                self._persist_pos(sym, pos, plan)

        # 2) signals -> portfolio -> entries
        positions = {p.symbol: p for p in self.execution.broker.get_positions()}
        self._contributors: Dict[str, dict] = {}
        if self.coordinator is not None:
            net = self.coordinator.decide(self._agent_context(quotes, positions, acct.equity, now, klass))
            self._contributors = {s: n.contributors for s, n in net.items() if n.contributors}
            signals = self.coordinator.to_series(net, self._bars)
        else:
            signals = self._signals_now()
        decision = self.pm.plan(
            signals=signals, holdings={s: p.qty for s, p in positions.items()}, equity=acct.equity
        )
        for sym in decision.signal_exits:
            if sym in positions and sym in quotes:
                self._exit(sym, positions[sym], quotes[sym], "signal_exit", acct, list(positions.values()))
                out["exits"].append({"symbol": sym, "reason": "signal_exit"})

        positions = {p.symbol: p for p in self.execution.broker.get_positions()}
        if not entries_allowed(self.exit_cfg, _dt(now)):
            if decision.entries:
                self._notice("cutoff", f"past {self.exit_cfg.entry_cutoff_et} ET; no new entries")
            decision.entries = []
        for e in decision.entries:
            q = quotes.get(e.symbol)
            if q is None:
                continue
            res = self._enter(e, q, acct, list(positions.values()))
            (out["entries"] if res.get("ok") else out["blocked"]).append({"symbol": e.symbol, **res})

        self._snapshot(now)
        return out

    # ------------------------------------------------------------------ #
    # orders
    # ------------------------------------------------------------------ #
    def _enter(self, e, q, acct: Account, positions) -> dict:
        contributors = getattr(self, "_contributors", {}).get(e.symbol, {})
        strat = (max(contributors, key=contributors.get) if contributors
                 else ("coordinator" if self.coordinator else self.family))
        req = OrderRequest(e.symbol, "buy", e.qty, type="market", reason=e.reason, strategy=strat)
        decision = self.risk.pretrade_check(req, account=acct, positions=positions, quote=q)
        if decision.blocked:
            self._event("warn", "blocked", f"{e.symbol}: {decision.reason}")
            return {"ok": False, "reason": decision.reason}
        if decision.adjusted_qty is not None:
            req.qty = decision.adjusted_qty
        if req.qty * q.ask < self.cfg.portfolio.min_notional_usd:
            return {"ok": False, "reason": "below min notional after risk sizing"}
        try:
            order = self.execution.broker.place_order(req)
        except BrokerError as exc:
            self._event("error", "order", f"buy {e.symbol} failed: {exc}")
            return {"ok": False, "reason": str(exc)}
        self.risk.on_order_submitted()
        record_order(self.conn, order, mode=self.mode, role="entry")

        fill = order.filled_avg_price or q.ask
        plan = build_plan(fill, e.atr, opened_index=0, cfg=self.exit_cfg,
                          stop=getattr(e, "stop", None), target=getattr(e, "target", None),
                          tp1=getattr(e, "tp1", None))
        plan.opened_ts = time.time()
        plan.entry_fee = order.fee
        self.plans[e.symbol] = plan
        self._entry_meta[e.symbol] = {
            "price": fill, "qty": req.qty, "fee": order.fee, "opened_ts": plan.opened_ts,
            "order_id": order.id, "risk_dollars": e.risk_dollars,
            "contributors": contributors, "strategy": strat,
        }
        pos = self.execution.broker.get_position(e.symbol) or Position(e.symbol, req.qty, fill, fill)
        self._persist_pos(e.symbol, pos, plan, strategy=strat, reason=e.reason)
        self._event("info", "entry", f"{e.symbol} x{req.qty:g} @ ~{fill:.4g}  ({e.reason})")
        return {"ok": True, "qty": req.qty, "price": fill}

    def _exit(self, sym: str, pos: Position, q, reason: str, acct: Account, positions,
              qty: Optional[float] = None) -> None:
        """Sell ``qty`` (default: the whole position).

        A partial sell -- only TP1 does this today -- books the realised slice as
        its own trade row, keeps the plan and entry metadata alive for the
        remainder, and lifts the stop to breakeven via :func:`take_tp1`.
        """
        full_qty = pos.qty             # capture before the broker mutates the position object
        qty = full_qty if qty is None else min(qty, full_qty)
        if qty <= 0:
            return
        partial = qty < full_qty - 1e-12
        avg_price = pos.avg_price
        req = OrderRequest(sym, "sell", qty, type="market", reason=reason, strategy=self.family)
        self.risk.pretrade_check(req, account=acct, positions=positions, quote=q)
        try:
            order = self.execution.broker.place_order(req)
        except BrokerError as exc:
            self._event("error", "order", f"sell {sym} failed: {exc}")
            return
        self.risk.on_order_submitted()
        record_order(self.conn, order, mode=self.mode, role="exit")

        fill = order.filled_avg_price or q.bid
        meta = self._entry_meta.get(sym, {})
        entry_px = meta.get("price", avg_price)
        # Charge only the sold slice's share of the entry commission, so a
        # partial does not book the whole entry fee and then book it again at
        # the final exit.
        share = (qty / full_qty) if full_qty > 0 else 1.0
        entry_fee_share = meta.get("fee", 0.0) * share
        fees = entry_fee_share + order.fee
        pnl = (fill - entry_px) * qty - fees
        record_trade(self.conn, {
            "symbol": sym, "qty": qty, "entry_price": entry_px, "exit_price": fill,
            "fees": fees, "pnl": pnl, "strategy": meta.get("strategy") or self.family,
            "entry_order_id": meta.get("order_id"), "exit_order_id": order.id,
            "opened_ts": meta.get("opened_ts"), "closed_ts": time.time(),
        }, self.mode)
        contributors = meta.get("contributors") or {}
        if self.coordinator is not None and contributors:
            try:
                self.coordinator.ledger.attribute(contributors, pnl, sym, time.time())
            except Exception:  # noqa: BLE001
                log.debug("agent attribution failed", exc_info=True)
        self.risk.on_trade_closed(pnl)

        if partial:
            plan = self.plans.get(sym)
            if plan is not None and reason == "tp1":
                take_tp1(plan)
            if meta:
                meta["qty"] = max(full_qty - qty, 0.0)
                meta["fee"] = meta.get("fee", 0.0) - entry_fee_share
            remaining = self.execution.broker.get_position(sym)
            if remaining is not None and plan is not None:
                self._persist_pos(sym, remaining, plan)
            self._event("info", "exit",
                        f"{sym} {reason} x{qty:g}/{full_qty:g} @ ~{fill:.4g}  pnl={pnl:+.2f}"
                        + (f"  stop->BE {plan.hard_stop:.4g}" if plan is not None
                           and plan.tp1_done and plan.be_after_tp1 else ""))
            return

        self.plans.pop(sym, None)
        self._entry_meta.pop(sym, None)
        delete_position(self.conn, self.mode, sym)
        self._event("info", "exit", f"{sym} {reason} @ ~{fill:.4g}  pnl={pnl:+.2f}")

    def flatten(self, reason: str) -> None:
        self._event("halt", "flatten", f"FLATTEN & HALT: {reason}")
        for oid in [o.id for o in self.execution.broker.list_orders(open_only=True)]:
            try:
                self.execution.broker.cancel_order(oid)
            except BrokerError:
                pass
        for pos in self.execution.broker.get_positions():
            try:
                q = self.execution.quotes.get_quote(pos.symbol)
            except Exception:  # noqa: BLE001
                q = None
            acct = self.execution.broker.get_account()
            self._exit(pos.symbol, pos, q or _fake_quote(pos), "flatten", acct,
                       self.execution.broker.get_positions())
        self.risk.halt(reason, source="flatten")

    # ------------------------------------------------------------------ #
    # permanent kill-on-loss
    # ------------------------------------------------------------------ #
    def _seed_initial_equity(self) -> None:
        """Anchor the kill floor ONCE, on the first engine boot, to the broker's
        equity. Persisted in ``bot_flags`` so it survives restarts; never
        auto-reset (``scripts/launchd.sh revive`` deletes it to re-anchor)."""
        raw = get_flag(self.conn, "initial_equity")
        if raw:
            self.initial_equity = float(raw)
            return
        try:
            eq = float(self.execution.broker.get_account().equity)
        except Exception:  # noqa: BLE001 - network blip at boot; retried in tick()
            log.warning("initial_equity seed deferred: get_account failed")
            return
        self.initial_equity = eq
        set_flag(self.conn, "initial_equity", repr(eq))
        self._event("info", "engine", f"anchored initial_equity=${eq:,.2f} (kill floor)")

    def _kill(self, reason: str, equity: float) -> None:
        """Permanent stop: flatten to cash, write DEAD, disable the agents, exit 0."""
        self._event("halt", "kill", f"PERMANENT KILL: {reason}")
        for step in (
            lambda: self.flatten(reason),
            lambda: self.dead.kill(reason, equity=equity, initial_equity=self.initial_equity),
            lambda: set_flag(self.conn, "killed", "1"),
            lambda: self.settings.notifies("halt") and self.notifier.notify(
                f"PERMANENT KILL: {reason}", title="BOT KILLED - DEAD",
                priority="urgent", tags=["skull", "rotating_light"]),
            _disable_launchd_agents,
            self.stop,
        ):
            try:
                step()
            except Exception:  # noqa: BLE001
                log.exception("kill step failed; continuing")
        os._exit(0)   # exit 0 -> KeepAlive{SuccessfulExit=false} leaves it stopped

    def _check_kill_floor(self, equity: float) -> bool:
        """Returns True (and permanently kills) once mark-to-market equity has held
        at/through the deposit floor for ``kill_floor_confirm_ticks`` ticks *or* the
        equivalent wall-clock (restart-safe via the ``kill_floor_since`` flag)."""
        rc = self.cfg.risk
        if not rc.kill_below_deposit:
            return False
        if self.initial_equity is None:
            self._seed_initial_equity()
        if self.initial_equity is None:
            return False
        floor = self.initial_equity * (1.0 + rc.kill_floor_pct)

        if not deposit_floor_breached(equity, self.initial_equity, rc.kill_floor_pct):
            self._kill_floor_strikes = 0
            if get_flag(self.conn, "kill_floor_since"):
                set_flag(self.conn, "kill_floor_since", "")
            return False

        self._kill_floor_strikes += 1
        now = time.time()
        since_raw = get_flag(self.conn, "kill_floor_since")
        if not since_raw:
            set_flag(self.conn, "kill_floor_since", repr(now))
            since = now
        else:
            since = float(since_raw)

        need = max(rc.kill_floor_confirm_ticks, 1)
        need_secs = need * max(self.cfg.engine.strategy_tick_seconds, 1)
        if self._kill_floor_strikes < need and (now - since) < need_secs:
            self._event("warn", "risk",
                        f"equity ${equity:,.2f} <= floor ${floor:,.2f} "
                        f"(strike {self._kill_floor_strikes}/{need}, {now - since:.0f}s below)")
            return False

        self._kill(f"equity ${equity:,.2f} <= deposit floor ${floor:,.2f} "
                   f"(anchor ${self.initial_equity:,.2f})", equity)
        return True

    # ------------------------------------------------------------------ #
    # reconciliation & persistence
    # ------------------------------------------------------------------ #
    def _reconcile(self, positions: Dict[str, Position], quotes, now: float) -> None:
        for sym in list(self.plans):
            if sym not in positions:
                self._event("warn", "reconcile", f"{sym}: plan without position; dropping plan")
                self.plans.pop(sym, None)
                self._entry_meta.pop(sym, None)
                delete_position(self.conn, self.mode, sym)
        for sym, pos in positions.items():
            if sym not in self.plans:
                atr = self._recent_atr(sym) or pos.avg_price * 0.02
                plan = build_plan(pos.avg_price, atr, opened_index=0, cfg=self.exit_cfg)
                plan.opened_ts = now
                self.plans[sym] = plan
                self._event("warn", "reconcile",
                            f"{sym}: position without plan; attached protective stop @ {plan.hard_stop:.4g}")
                self._persist_pos(sym, pos, plan)

    def _persist_pos(self, sym, pos: Position, plan: ExitPlan, *, strategy="", reason="") -> None:
        upsert_position(self.conn, self.mode, pos, plan=plan.as_dict(),
                        strategy=strategy or self.family, entry_reason=reason,
                        opened_ts=plan.opened_ts)

    def _load_plans(self) -> None:
        for sym, row in load_positions(self.conn, self.mode).items():
            if row.get("plan_json"):
                try:
                    plan = ExitPlan.from_dict(json.loads(row["plan_json"]))
                    self.plans[sym] = plan
                    self._entry_meta[sym] = {"price": row["avg_price"], "qty": row["qty"],
                                             "fee": plan.entry_fee, "opened_ts": row.get("opened_ts")}
                except Exception:  # noqa: BLE001
                    log.warning("could not load plan for %s", sym)

    def _snapshot(self, now: float) -> None:
        acct = self.execution.broker.get_account()
        positions_value = sum(p.market_value for p in self.execution.broker.get_positions())
        snapshot_equity(self.conn, self.mode, acct.cash, positions_value,
                        peak=self.risk.state.peak_equity, ts=now)

    # ------------------------------------------------------------------ #
    # data helpers
    # ------------------------------------------------------------------ #
    def _refresh_bars(self, force: bool = False) -> None:
        ttl = min(self.bar_secs, 3600)
        if not force and time.time() - self._bars_ts < ttl:
            return
        try:
            frames = load_history(
                self.cfg.universe, str(self.tf),
                days=self.cfg.market_data.history_days, settings=self.settings,
            )
            self._bars = {s: df for s, df in frames.items() if not df.empty}
            self._bars_ts = time.time()
        except Exception as exc:  # noqa: BLE001
            self._event("warn", "bars", f"refresh failed: {exc}")

    def _signals_now(self) -> Dict[str, pd.Series]:
        out: Dict[str, pd.Series] = {}
        for sym, df in self._bars.items():
            if len(df) < 60:
                continue
            out[sym] = self.signal_fn(df).iloc[-1]
        return out

    def _agent_context(self, quotes, positions, equity, now, klass):
        from botcore.agents.base import AgentContext

        return AgentContext(
            bars=self._bars, quotes=quotes, positions=positions, equity=equity,
            universe=list(self.cfg.universe), now=now, conn=self.conn,
            settings=self.settings, klass=klass,
        )

    def _recent_atr(self, sym: str) -> float:
        df = self._bars.get(sym)
        if df is None or len(df) < self.cfg.strategy.params.atr_period + 2:
            return 0.0
        a = ind.atr(df["high"], df["low"], df["close"], self.cfg.strategy.params.atr_period)
        v = a.dropna()
        return float(v.iloc[-1]) if len(v) else 0.0

    def _recent_high(self, sym: str, fallback: float) -> float:
        df = self._bars.get(sym)
        if df is None or df.empty:
            return fallback
        return float(df["high"].iloc[-1])

    # ------------------------------------------------------------------ #
    def _event(self, level: str, kind: str, message: str, data: Optional[dict] = None) -> None:
        log.log({"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR,
                 "halt": logging.ERROR}.get(level, logging.INFO), "[%s] %s", kind, message)
        log_event(self.conn, level, kind, message, data)
        try:
            self._maybe_notify(level, kind, message)
        except Exception:  # noqa: BLE001
            log.exception("notify failed")

    def _maybe_notify(self, level: str, kind: str, message: str) -> None:
        s = self.settings
        n = self.notifier
        if kind == "kill" and s.notifies("halt"):
            n.notify(message, title="BOT KILLED - DEAD", priority="urgent",
                     tags=["skull"], key="kill")
        elif kind == "agent-kill" and s.notifies("halt"):
            n.notify(message, title="AGENT DISABLED", priority="high",
                     tags=["skull"], key=f"agent-kill:{message[:24]}")
        elif level == "halt" and s.notifies("halt"):
            n.notify(message, title="BOT HALT", priority="urgent", tags=["rotating_light"], key="halt")
        elif kind == "watchdog" and s.notifies("watchdog"):
            n.notify(message, title="BOT WATCHDOG", priority="urgent", tags=["skull"])
        elif kind == "reconcile" and level == "warn" and s.notifies("reconcile"):
            n.notify(message, title="reconcile drift", priority="high", tags=["warning"], key="reconcile")
        elif kind == "tick" and level == "error" and s.notifies("error"):
            n.notify(message, title="tick failure", priority="high", tags=["warning"], key="tick-fail")
        elif kind == "summary" and s.notifies("daily"):
            n.notify(message, title=f"Daily summary - {self.mode}",
                     tags=["chart_with_upwards_trend"])
        elif kind in ("entry", "exit") and s.notifies("fills"):
            n.notify(message, title=kind, tags=["moneybag"])
        elif kind == "engine" and s.notifies("engine"):
            n.notify(message, title="engine", priority="low")

    def _notice(self, kind: str, message: str) -> None:
        if time.time() - self._last_closed_notice > 900:
            self._event("info", kind, message)
            self._last_closed_notice = time.time()


def _now_dt():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _dt(ts: float):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _fake_quote(pos: Position):
    from botcore.brokers.base import Quote
    px = pos.market_price or pos.avg_price
    return Quote(pos.symbol, bid=px * 0.999, ask=px * 1.001, ts=time.time())


def _disable_launchd_agents() -> None:
    """Best-effort `launchctl bootout` of both agents so nothing resurrects a dead bot.
    No-op when not running under launchd (or `launchctl` is unavailable)."""
    uid = os.getuid()
    for label in ("com.rhcryptobot.paper", "com.rhcryptobot.watchdog"):
        try:
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                           capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass
