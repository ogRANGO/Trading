"""Read the two bot dashboards + their configured universes.

GET only. Never writes bot state, never posts to a bot endpoint.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import httpx
import yaml

from botcore.brief.bconfig import REPO_ROOT, load_brief_config
from botcore.brief.models import Position

log = logging.getLogger(__name__)


def _dashboard_token() -> str:
    try:
        from botcore.config import get_settings
        return get_settings().dashboard_token or ""
    except Exception:  # noqa: BLE001
        return ""


def fetch_bot_state(port: int, timeout: float = 6.0) -> Optional[dict]:
    """/api/state for one bot, or None if unreachable."""
    tok = _dashboard_token()
    headers = {"X-Dashboard-Token": tok} if tok and tok != "change-me" else {}
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/state", headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("bot :%s unreachable: %s", port, exc)
        return None


def positions_from_state(state: dict, bot: str) -> list[Position]:
    out: list[Position] = []
    for p in state.get("positions", []) or []:
        if str(p.get("status", "OPEN")).upper() != "OPEN":
            continue
        out.append(Position(
            ticker=str(p.get("ticker", "")).upper(),
            bot=bot,
            unrealized_pct=_f(p.get("unrealized_pct")),
            shares=_f(p.get("shares")),
            entry_price=_f(p.get("entry_price")),
            current_price=_f(p.get("current_price")),
        ))
    return out


def load_universe(config_file: str, active_key_fallback: str) -> list[str]:
    """Active universe tickers from a bot config yaml."""
    path = REPO_ROOT / config_file
    try:
        cfg = yaml.safe_load(path.read_text("utf-8"))
        key = cfg.get("active_universe", active_key_fallback)
        return [str(s).upper() for s in cfg.get("universes", {}).get(key, [])]
    except Exception as exc:  # noqa: BLE001
        log.warning("universe load failed for %s: %s", config_file, exc)
        return []


def gather() -> dict:
    """Everything the assembler needs about the bots.

    Returns {reachable: bool, positions: [Position], universes: {bot: [tickers]},
             states: {bot: state-dict|None}}.
    """
    bcfg = load_brief_config()
    bots = {b["name"]: b["port"] for b in bcfg["bots"]}

    states, positions = {}, []
    for name, port in bots.items():
        st = fetch_bot_state(port)
        states[name] = st
        if st:
            positions.extend(positions_from_state(st, name))

    universes = {
        "stock": load_universe("config.yaml", "tech_equity"),
        "meme": load_universe("config_crypto.yaml", "crypto_memes"),
    }
    return {
        "reachable": any(v is not None for v in states.values()),
        "positions": positions,
        "universes": universes,
        "states": states,
    }


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
