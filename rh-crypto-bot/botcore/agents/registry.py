"""Build the coordinator from config, and read the agent roster for the dashboard."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional

from botcore.agents.base import Agent
from botcore.agents.coordinator import Coordinator
from botcore.agents.ledger import AgentLedger
from botcore.agents.technical import (
    MeanReversionAgent,
    MomentumAgent,
    TrendAgent,
    VolRegimeAgent,
)
from botcore.config import BotConfig, Settings
from botcore.risk.killswitch import DeadSwitch
from botcore.store.state import agent_equity_series, agent_pnl_summary, last_agent_signal

log = logging.getLogger(__name__)


def _make_agent(agent_id: str, cfg: BotConfig) -> Optional[Agent]:
    p = cfg.strategy.params
    if agent_id == "trend":
        return TrendAgent(p)
    if agent_id == "mean_reversion":
        return MeanReversionAgent(p)
    if agent_id == "momentum":
        return MomentumAgent()
    if agent_id == "vol_regime":
        return VolRegimeAgent()
    if agent_id == "news":
        try:
            from botcore.agents.news import NewsAgent
        except Exception:  # noqa: BLE001 - Part B not built / import error
            log.warning("news agent unavailable; skipping")
            return None
        return NewsAgent(cfg.agents["news"])
    if agent_id == "flow":
        try:
            from botcore.agents.flow import FlowAgent
        except Exception:  # noqa: BLE001 - Part C not built
            log.warning("flow agent unavailable; skipping")
            return None
        return FlowAgent(cfg.agents["flow"])
    log.warning("unknown agent id %r in config; skipping", agent_id)
    return None


def build_agents(cfg: BotConfig) -> List[Agent]:
    agents: List[Agent] = []
    for agent_id, acfg in cfg.agents.items():
        if not acfg.enabled:
            continue
        a = _make_agent(agent_id, cfg)
        if a is None:
            continue
        a.asset_classes = frozenset(acfg.asset_classes)
        agents.append(a)
    return agents


def build_coordinator(cfg: BotConfig, conn, settings: Settings,
                      *, event: Optional[Callable[[str, str, str], None]] = None) -> Coordinator:
    agents = build_agents(cfg)
    weights = {a.id: cfg.agents[a.id].weight for a in agents}
    ledger = AgentLedger(conn, cfg.agent_kill, mode=settings.bot_mode, fees=cfg.fees)
    coord = Coordinator(agents, weights, cfg.coordinator, ledger, event=event)
    log.info("coordinator: %d agents active (%s)", len(agents), ", ".join(a.id for a in agents))
    return coord


def _dead_dir(db_path: str) -> Path:
    return Path(db_path).parent / "agents"


def agent_roster(conn, mode: str, cfg: BotConfig, db_path: str) -> List[dict]:
    """Pure read: config + agent_* tables + data/agents/*.DEAD. Used by /api/agents and the CLI."""
    summ = agent_pnl_summary(conn, mode)
    ddir = _dead_dir(db_path)
    stake = cfg.agent_kill.stake_usd
    floor_pct = cfg.agent_kill.kill_floor_pct
    out: List[dict] = []
    for agent_id, acfg in cfg.agents.items():
        d = summ.get(agent_id, {})
        eq_rows = agent_equity_series(conn, agent_id, mode, limit=1)
        shadow_eq = float(eq_rows[-1]["equity"]) if eq_rows else stake
        ds = DeadSwitch(str(ddir / f"{agent_id}.DEAD"))
        trades = int(d.get("shadow_trades", 0))
        wins = int(d.get("wins", 0))
        floor = stake * (1.0 + floor_pct)
        dist = (shadow_eq - floor) / stake * 100.0 if stake else 0.0
        kind = ("news" if agent_id == "news" else "flow" if agent_id == "flow"
                else "onchain" if agent_id == "onchain" else "technical")
        out.append({
            "id": agent_id,
            "kind": kind,
            "enabled": bool(acfg.enabled),
            "dead": ds.dead,
            "dead_certificate": ds.certificate(),
            "weight": acfg.weight,
            "shadow_equity": round(shadow_eq, 2),
            "shadow_return_pct": round((shadow_eq / stake - 1.0) * 100.0, 2) if stake else 0.0,
            "shadow_trades": trades,
            "win_rate": round(wins / trades, 3) if trades else 0.0,
            "attributed_pnl": round(float(d.get("attributed_pnl", 0.0)), 2),
            "last_signal": last_agent_signal(conn, agent_id, mode),
            "distance_to_kill_pct": round(dist, 1),
        })
    return out
