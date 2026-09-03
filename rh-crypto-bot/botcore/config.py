"""Configuration loading.

Two sources:
  * ``.env`` (secrets, mode, money caps)  -> :class:`Settings`
  * ``config.yaml`` (non-secret knobs)    -> :class:`BotConfig`
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
DEFAULT_ENV_PATH = REPO_ROOT / ".env"
DEFAULT_DB_PATH = REPO_ROOT / "data" / "bot.db"


class Settings(BaseSettings):
    """Secrets and operational flags, sourced from environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    rh_api_key: str = ""
    rh_private_key_b64: str = ""
    rh_api_base_url: str = "https://trading.robinhood.com"

    # Alpaca (simulation broker + market data). Free keys from alpaca.markets.
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
    alpaca_data_feed: str = "iex"  # 'iex' (free) or 'sip' (paid)

    # Finnhub free tier — read-only, pre-market brief bot only (botcore/brief/).
    finnhub_key: str = ""

    # Massive (ex-Polygon.io) Basic tier — read-only news/sentiment/ticker reference
    # for the catalyst scanner (botcore/universe/). 5 calls/min. Never order flow.
    massive_key: str = ""

    # Robinhood Agentic MCP (live equities). OAuth handled out of band.
    rh_mcp_url: str = "https://agent.robinhood.com/mcp/trading"

    anthropic_api_key: str = ""

    dashboard_token: str = "change-me"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787

    ntfy_topic: str = ""
    ntfy_base_url: str = "https://ntfy.sh"
    smtp_url: str = ""
    alert_email_to: str = ""
    notify_events: str = "halt,watchdog,error,reconcile,daily"
    notify_min_interval_s: float = 900.0

    # Logging (botcore.serve). log_dir="" -> <repo>/logs.
    log_dir: str = ""
    log_max_mb: int = 5
    log_backups: int = 5

    bot_mode: str = "paper"
    broker: str = "sim"            # sim | alpaca | robinhood_mcp | robinhood_crypto
    live_max_usd: float = 0.0
    max_trade_usd: float = 20.0
    paper_start_equity: float = 100_000.0

    db_path: str = str(DEFAULT_DB_PATH)

    @field_validator("bot_mode")
    @classmethod
    def _mode_ok(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"paper", "live"}:
            raise ValueError("BOT_MODE must be 'paper' or 'live'")
        return v

    @field_validator("broker")
    @classmethod
    def _broker_ok(cls, v: str) -> str:
        v = v.strip().lower()
        allowed = {"sim", "alpaca", "robinhood_mcp", "robinhood_crypto"}
        if v not in allowed:
            raise ValueError(f"BROKER must be one of {sorted(allowed)}")
        return v

    @field_validator("live_max_usd", "max_trade_usd")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError("money caps must be >= 0")
        return v

    def has_rh_credentials(self) -> bool:
        return bool(self.rh_api_key and self.rh_private_key_b64)

    def has_alpaca_credentials(self) -> bool:
        return bool(self.alpaca_key_id and self.alpaca_secret_key)

    def notifies(self, event: str) -> bool:
        """True if ``event`` (halt|watchdog|error|reconcile|daily|fills|engine) is enabled."""
        return event in {e.strip() for e in self.notify_events.split(",") if e.strip()}


# --------------------------------------------------------------------------- #
# config.yaml models
# --------------------------------------------------------------------------- #
class MarketDataCfg(BaseModel):
    timeframe: str = "1Day"          # bar size for signals: 1Day / 1Hour / 15Min
    quote_poll_seconds: int = 15
    history_days: int = 1400         # how much history to pull for backtests


class SignalParams(BaseModel):
    # trend family
    ema_fast: int = 20
    ema_slow: int = 50
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    adx_period: int = 14
    adx_min: float = 20.0
    # mean-reversion family
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 14
    rsi_buy: float = 30.0
    rsi_exit: float = 55.0
    zscore_period: int = 20
    zscore_entry: float = -1.5
    # smart-money-concepts family (SMC Level 1)
    swing_len: int = 3               # fractal pivot half-width; confirmed swing_len bars late
    ob_lookback: int = 10            # how far back to hunt the order block before a break
    ob_max_age_bars: int = 33        # "golden window" — OB expires if untouched
    require_fvg: bool = True         # order block must have an imbalance after it
    fvg_search_back: bool = False    # if the nearest opposing candle has no FVG,
                                     # keep hunting further back instead of giving up.
                                     # Genuinely ambiguous in the source material and
                                     # it dominates the setup count, so the grid decides.
    stop_buffer_atr: float = 0.1     # stop sits this many ATR under the OB wick low
    htf_filter: str = "structure"    # structure | ema | none
    htf: str = ""                    # "" = auto (15Min->1Hour, 1Hour->1Day)
    require_reversal_candle: bool = False   # Level 2: engulfing/hammer at the touch
    min_rr: float = 1.5              # reject setups whose reward-to-structure is thinner
    # shared
    atr_period: int = 14

    @field_validator("htf_filter")
    @classmethod
    def _htf_filter_ok(cls, v: str) -> str:
        allowed = {"structure", "ema", "none"}
        if v not in allowed:
            raise ValueError(f"htf_filter must be one of {sorted(allowed)}")
        return v


class StrategyCfg(BaseModel):
    signal_family: str = "trend"     # trend | mean_reversion | smc
    params: SignalParams = Field(default_factory=SignalParams)

    # backwards compat: older code / tests read strategy.name
    @property
    def name(self) -> str:
        return self.signal_family

    @field_validator("signal_family")
    @classmethod
    def _family_ok(cls, v: str) -> str:
        v = v.strip().lower()
        allowed = {"trend", "mean_reversion", "smc"}
        if v not in allowed:
            raise ValueError(f"signal_family must be one of {sorted(allowed)}")
        return v


class ExitCfg(BaseModel):
    hard_stop_atr_mult: float = 4.0
    target_atr_mult: float = 0.0      # 0 = no fixed target (let winners run)
    trail_atr_mult: float = 6.0       # 0 = no trailing stop
    time_stop_bars: int = 0           # 0 = no time stop

    # Intraday discipline. flat_by_et closes everything before the bell, which is
    # what makes risk == stop distance: with overnight holds a gap through the
    # stop is a multiple of the risk the position was sized for.
    flat_by_et: Optional[str] = None      # "15:55", or None for 24/7 markets
    entry_cutoff_et: Optional[str] = None  # no new entries after this

    # Partial take-profit. 0 = disabled (whole position exits at once, as before).
    tp1_fraction: float = 0.0         # fraction of the position closed at TP1
    be_after_tp1: bool = True         # then lift the hard stop to breakeven

    @field_validator("flat_by_et", "entry_cutoff_et")
    @classmethod
    def _clock_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        try:
            hh, mm = v.split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
        except Exception:
            raise ValueError(f"expected 'HH:MM' Eastern time, got {v!r}")
        return v

    @field_validator("tp1_fraction")
    @classmethod
    def _tp1_ok(cls, v: float) -> float:
        if not 0.0 <= v < 1.0:
            raise ValueError("tp1_fraction must be in [0, 1) — 1.0 would be a full exit")
        return v


# Sensible per-family defaults; override in config.yaml under portfolio.exit_profiles.
_TREND_EXIT = ExitCfg(hard_stop_atr_mult=4.0, target_atr_mult=0.0, trail_atr_mult=6.0, time_stop_bars=0)
_MR_EXIT = ExitCfg(hard_stop_atr_mult=3.0, target_atr_mult=3.0, trail_atr_mult=0.0, time_stop_bars=15)
# SMC sizes and stops off levels, not ATR multiples, so the ATR fields here are
# only the fallback for a signal row that arrived without a stop.
_SMC_EXIT = ExitCfg(hard_stop_atr_mult=2.0, target_atr_mult=0.0, trail_atr_mult=0.0,
                    time_stop_bars=16, flat_by_et="15:55", entry_cutoff_et="15:15",
                    tp1_fraction=0.25, be_after_tp1=True)


class PortfolioCfg(BaseModel):
    max_positions: int = 5
    risk_fraction: float = 0.01      # equity fraction risked to the hard stop
    max_position_weight: float = 0.30
    min_notional_usd: float = 1.0
    exit: ExitCfg = Field(default_factory=ExitCfg)
    exit_profiles: "dict[str, ExitCfg]" = Field(
        default_factory=lambda: {"trend": _TREND_EXIT, "mean_reversion": _MR_EXIT,
                                 "smc": _SMC_EXIT}
    )

    def exit_for(self, family: str) -> ExitCfg:
        return self.exit_profiles.get(family, self.exit)


class FeesCfg(BaseModel):
    commission_pct: float = 0.0      # Alpaca/Robinhood equities: 0
    commission_min_usd: float = 0.0
    slippage_pct: float = 0.0005
    crypto_spread_pct: float = 0.0020  # extra cost applied to crypto fills


class RiskCfg(BaseModel):
    max_concurrent_positions: int = 5
    max_position_weight: float = 0.30      # hard per-name ceiling (risk side)
    max_total_exposure_pct: float = 0.90
    daily_loss_limit_pct: float = 0.05
    max_drawdown_pct: float = 0.20
    consecutive_loss_limit: int = 5
    cooldown_minutes: int = 120
    anomaly_move_pct: float = 0.20
    max_spread_pct: float = 0.01
    stale_quote_seconds: int = 300
    max_orders_per_hour: int = 20
    equities_rth_only: bool = True
    pdt_min_equity_usd: float = 25000.0
    pdt_max_day_trades: int = 3
    # Permanent kill-on-loss (Phase 6). Anchored to the deposit on first boot.
    kill_below_deposit: bool = True
    kill_floor_pct: float = -0.05         # 0.0 = kill at deposit; -0.05 = 5% grace band
    kill_floor_confirm_ticks: int = 3     # consecutive breaching ticks before the permanent kill


class EngineCfg(BaseModel):
    strategy_tick_seconds: int = 60
    advisor_tick_minutes: int = 120       # Phase 3 LLM advisor — not yet consumed
    reconcile_tick_minutes: int = 5       # periodic broker<->DB reconcile job
    # Phase 5 unattended hardening
    watchdog_stall_seconds: int = 600     # no tick progress this long -> restart
    watchdog_check_seconds: int = 30
    tick_fail_streak_notify: int = 3      # consecutive tick exceptions before alerting
    tick_fail_streak_halt: int = 10       # consecutive tick exceptions before HALT
    events_retention_days: int = 90
    quotes_retention_days: int = 7
    housekeeping_utc_hour: int = 8        # daily prune + WAL checkpoint
    daily_summary_utc_hour: int = 13      # daily P&L digest push
    # Phase 7 multi-agent trading floor
    engine_mode: str = "single"           # single = one signal family | multi = agent coordinator

    @field_validator("engine_mode")
    @classmethod
    def _mode_ok(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"single", "multi"}:
            raise ValueError("engine_mode must be 'single' or 'multi'")
        return v


# --------------------------------------------------------------------------- #
# Phase 7 — multi-agent config
# --------------------------------------------------------------------------- #
class AgentCfg(BaseModel):
    enabled: bool = True
    weight: float = 1.0                   # base vote weight in the coordinator blend
    asset_classes: List[str] = Field(default_factory=lambda: ["equity", "crypto"])
    poll_minutes: int = 0                 # 0 = every tick; >0 = throttled (news/flow)
    engine: str = ""                      # news agent: "lexicon" | "llm"


_DEFAULT_AGENTS = {
    "trend":          AgentCfg(weight=1.0),
    "mean_reversion": AgentCfg(weight=0.8),
    "momentum":       AgentCfg(weight=0.8),
    "vol_regime":     AgentCfg(weight=0.6),
    "news":           AgentCfg(weight=0.4, poll_minutes=60, engine="lexicon"),
    "flow":           AgentCfg(enabled=False, weight=0.3, asset_classes=["crypto"], poll_minutes=15),
}


class CoordinatorCfg(BaseModel):
    min_agents_agree: int = 2             # this many +1 votes before an entry
    min_net_conviction: float = 0.35      # and the weighted net must clear this
    veto_conviction: float = 0.6          # a -1 at/above this conviction blocks the symbol


class AgentKillCfg(BaseModel):
    stake_usd: float = 1000.0             # notional solo book per agent
    kill_floor_pct: float = -0.15         # disable when shadow equity <= stake*(1+pct)
    confirm_ticks: int = 3
    min_trades: int = 10                  # don't judge an agent before this many shadow trades


class BotConfig(BaseModel):
    universes: "dict[str, List[str]]"
    active_universe: str
    market_data: MarketDataCfg = Field(default_factory=MarketDataCfg)
    strategy: StrategyCfg = Field(default_factory=StrategyCfg)
    portfolio: PortfolioCfg = Field(default_factory=PortfolioCfg)
    fees: FeesCfg = Field(default_factory=FeesCfg)
    risk: RiskCfg = Field(default_factory=RiskCfg)
    engine: EngineCfg = Field(default_factory=EngineCfg)
    agents: "dict[str, AgentCfg]" = Field(default_factory=lambda: dict(_DEFAULT_AGENTS))
    coordinator: CoordinatorCfg = Field(default_factory=CoordinatorCfg)
    agent_kill: AgentKillCfg = Field(default_factory=AgentKillCfg)

    # Fingerprint of the *effective* config (file + env overrides), set by
    # load_bot_config. Stamped on every events row, every backtest result, and
    # status.json so config drift is visible instead of silent -- the bot ran a
    # churn config for days because nothing recorded which config was live.
    config_sha: str = ""

    @field_validator("universes")
    @classmethod
    def _universes_ok(cls, v: "dict[str, List[str]]") -> "dict[str, List[str]]":
        if not v:
            raise ValueError("define at least one universe")
        out = {}
        for name, syms in v.items():
            if not syms:
                raise ValueError(f"universe '{name}' is empty")
            out[name] = [s.strip().upper() for s in syms]
        return out

    @field_validator("agents")
    @classmethod
    def _agents_defaults(cls, v: "dict[str, AgentCfg]") -> "dict[str, AgentCfg]":
        merged = dict(_DEFAULT_AGENTS)
        merged.update(v or {})
        return merged

    def model_post_init(self, _ctx) -> None:
        if self.active_universe not in self.universes:
            raise ValueError(
                f"active_universe '{self.active_universe}' not in {list(self.universes)}"
            )

    @property
    def universe(self) -> List[str]:
        return self.universes[self.active_universe]


def load_bot_config(path: "str | Path | None" = None) -> BotConfig:
    # CONFIG_PATH lets a second instance (e.g. the crypto bot) run its own config.
    path = Path(path or os.environ.get("CONFIG_PATH") or DEFAULT_CONFIG_PATH)
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    override = os.environ.get("ACTIVE_UNIVERSE", "").strip()
    if override:
        raw["active_universe"] = override
    mode = os.environ.get("ENGINE_MODE", "").strip()
    if mode:
        raw.setdefault("engine", {})["engine_mode"] = mode
    cfg = BotConfig.model_validate(raw)
    cfg.config_sha = config_fingerprint(cfg)
    return cfg


def config_fingerprint(cfg: BotConfig) -> str:
    """Short stable hash of the effective config.

    Hashes the validated model, not the YAML text, so ACTIVE_UNIVERSE /
    ENGINE_MODE overrides and defaults are included -- two bots sharing a file
    but running different universes get different fingerprints, which is the
    case that matters.
    """
    payload = cfg.model_dump(mode="json", exclude={"config_sha"})
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@functools.lru_cache(maxsize=1)
def get_config() -> BotConfig:
    return load_bot_config()
