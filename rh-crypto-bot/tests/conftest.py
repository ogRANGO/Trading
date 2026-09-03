"""Test isolation: never read the developer's real .env or hit the network."""

from __future__ import annotations

import pytest

from botcore import config as _config


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Make Settings() ignore any on-disk .env during tests.
    patched = dict(_config.Settings.model_config)
    patched["env_file"] = None
    monkeypatch.setattr(_config.Settings, "model_config", patched)

    for var in (
        "RH_API_KEY", "RH_PRIVATE_KEY_B64", "ANTHROPIC_API_KEY",
        "BOT_MODE", "LIVE_MAX_USD", "MAX_TRADE_USD", "NTFY_TOPIC",
        "NTFY_BASE_URL", "SMTP_URL", "ALERT_EMAIL_TO", "NOTIFY_EVENTS",
        "NOTIFY_MIN_INTERVAL_S", "LOG_DIR", "ALPACA_KEY_ID", "ALPACA_SECRET_KEY",
        "BROKER", "DB_PATH",
    ):
        monkeypatch.delenv(var, raising=False)

    # Most tests were written against the crypto universe; the repo default is now
    # tech_equity. Pin it here; equity-specific tests override with monkeypatch.setenv.
    monkeypatch.setenv("ACTIVE_UNIVERSE", "crypto_major")
    # Legacy tests assume the single-family engine; multi-agent tests opt in.
    monkeypatch.setenv("ENGINE_MODE", "single")

    _config.get_settings.cache_clear()
    _config.get_config.cache_clear()

    from botcore.notify.push import reset_notifier
    reset_notifier()
    yield
    reset_notifier()
    _config.get_settings.cache_clear()
    _config.get_config.cache_clear()
