"""Load ``config_brief.yaml`` + the seed calendars. No secrets here."""

from __future__ import annotations

import functools
import os
from datetime import date
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_CONFIG = REPO_ROOT / "config_brief.yaml"


@functools.lru_cache(maxsize=4)
def load_brief_config(path: "str | Path | None" = None) -> dict[str, Any]:
    p = Path(path or os.environ.get("BRIEF_CONFIG_PATH") or DEFAULT_CONFIG)
    with open(p, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@functools.lru_cache(maxsize=2)
def load_market_calendar(year: int = 2026) -> dict[str, set[date]]:
    raw = yaml.safe_load((DATA_DIR / f"market_calendar_{year}.yaml").read_text("utf-8"))
    return {
        "holidays": {_as_date(d) for d in raw.get("holidays", [])},
        "half_days": {_as_date(d) for d in raw.get("half_days", [])},
    }


@functools.lru_cache(maxsize=2)
def load_econ_calendar(year: int = 2026) -> dict[str, Any]:
    raw = yaml.safe_load((DATA_DIR / f"econ_calendar_{year}.yaml").read_text("utf-8"))
    return {
        "fomc_decisions": {_as_date(d) for d in raw.get("fomc_decisions", [])},
        "index_events": [
            {"date": _as_date(e["date"]), "label": e["label"]}
            for e in raw.get("index_events", []) or []
        ],
        "one_offs": [
            {"date": _as_date(e["date"]), "label": e["label"]}
            for e in raw.get("one_offs", []) or []
        ],
    }


def resolve_path(rel: str) -> Path:
    """Paths in config_brief.yaml are $HOME-relative unless absolute."""
    p = Path(rel).expanduser()
    return p if p.is_absolute() else Path.home() / p


def _as_date(v: Any) -> date:
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v))
