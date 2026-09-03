from __future__ import annotations

import textwrap

import pytest
from pydantic import ValidationError

from botcore.config import BotConfig, Settings, load_bot_config


def test_repo_config_loads():
    cfg = load_bot_config()
    assert cfg.universe  # resolves active_universe
    assert all(s == s.upper() for s in cfg.universe)
    assert cfg.strategy.name == cfg.strategy.signal_family
    assert cfg.strategy.signal_family in {"trend", "mean_reversion"}
    assert cfg.risk.max_drawdown_pct > 0
    assert cfg.portfolio.max_positions >= 1


def test_active_universe_must_exist(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent("""
        universes:
          a: [AAPL, MSFT]
        active_universe: nope
    """))
    with pytest.raises(ValidationError):
        load_bot_config(p)


def test_empty_universe_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("universes: {a: []}\nactive_universe: a\n")
    with pytest.raises(ValidationError):
        load_bot_config(p)


def test_bad_signal_family_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(textwrap.dedent("""
        universes: {a: [AAPL]}
        active_universe: a
        strategy: {signal_family: astrology}
    """))
    with pytest.raises(ValidationError):
        load_bot_config(p)


def test_settings_mode_validation(monkeypatch):
    monkeypatch.setenv("BOT_MODE", "gambling")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_defaults(monkeypatch):
    for k in ("BOT_MODE", "LIVE_MAX_USD", "MAX_TRADE_USD", "RH_API_KEY", "RH_PRIVATE_KEY_B64"):
        monkeypatch.delenv(k, raising=False)
    s = Settings(_env_file=None)
    assert s.bot_mode == "paper"
    assert s.live_max_usd == 0.0
    assert s.has_rh_credentials() is False


def test_negative_money_cap_rejected(monkeypatch):
    monkeypatch.setenv("LIVE_MAX_USD", "-1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_phase5_settings_defaults():
    s = Settings(_env_file=None)
    assert s.ntfy_base_url == "https://ntfy.sh"
    assert s.notify_min_interval_s == 900.0
    assert s.log_max_mb == 5 and s.log_backups == 5
    assert s.notifies("halt") is True
    assert s.notifies("daily") is True
    assert s.notifies("fills") is False


def test_notifies_parses_custom_list():
    s = Settings(_env_file=None, notify_events="halt, fills ,engine")
    assert s.notifies("halt") and s.notifies("fills") and s.notifies("engine")
    assert not s.notifies("reconcile")


def test_phase5_engine_cfg_defaults():
    cfg = load_bot_config()
    assert cfg.engine.watchdog_stall_seconds == 600
    assert cfg.engine.events_retention_days == 90
    assert cfg.engine.daily_summary_utc_hour == 13


def test_kill_floor_defaults():
    from botcore.config import RiskCfg

    rc = RiskCfg()
    assert rc.kill_below_deposit is True
    assert rc.kill_floor_pct == -0.05
    assert rc.kill_floor_confirm_ticks == 3


def test_active_universe_env_override(monkeypatch):
    monkeypatch.setenv("ACTIVE_UNIVERSE", "tech_equity")
    cfg = load_bot_config()
    assert cfg.active_universe == "tech_equity"
    from botcore.data.base import asset_class

    assert all(asset_class(s) == "equity" for s in cfg.universe)


def test_engine_mode_default_and_override(monkeypatch):
    from botcore.config import EngineCfg

    assert EngineCfg().engine_mode == "single"
    with pytest.raises(ValidationError):
        EngineCfg(engine_mode="turbo")
    monkeypatch.setenv("ENGINE_MODE", "multi")
    assert load_bot_config().engine.engine_mode == "multi"


def test_agents_config_merges_defaults():
    cfg = load_bot_config()
    # config.yaml may name a subset; the 6 defaults are always present
    for aid in ("trend", "mean_reversion", "momentum", "vol_regime", "news", "flow"):
        assert aid in cfg.agents
    assert cfg.agents["flow"].enabled is False
    assert cfg.agent_kill.stake_usd == 1000.0
    assert cfg.coordinator.min_agents_agree == 2


def test_config_sha_is_stable_and_env_sensitive(monkeypatch):
    """The fingerprint must track the *effective* config, not just the file."""
    from botcore.config import config_fingerprint

    a = load_bot_config()
    b = load_bot_config()
    assert a.config_sha and len(a.config_sha) == 12
    assert a.config_sha == b.config_sha              # same inputs -> same sha

    # an env override that changes behaviour must change the fingerprint
    monkeypatch.setenv("ACTIVE_UNIVERSE", "crypto_major")
    other = load_bot_config()
    if other.active_universe != a.active_universe:
        assert other.config_sha != a.config_sha

    # the sha field itself is excluded from the hash (no self-reference)
    a.config_sha = "tampered"
    assert config_fingerprint(a) == config_fingerprint(b)
