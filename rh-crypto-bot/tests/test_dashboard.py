"""Dashboard API: read endpoints reflect DB state; controls toggle the flags."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from botcore import config as _config
from botcore.brokers.base import Order, Position
from botcore.config import ExitCfg
from botcore.store.db import open_db
from botcore.strategy.exitplan import build_plan
from botcore.store.state import (
    get_flag,
    record_order,
    snapshot_equity,
    upsert_position,
    upsert_quote,
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("BOT_MODE", "paper")
    monkeypatch.setenv("BROKER", "sim")
    # dashboard_token stays "change-me" -> controls are open (no token enforced)
    _config.get_settings.cache_clear()
    _config.get_config.cache_clear()

    plan = build_plan(entry_price=100.0, atr=4.0, opened_index=0,
                      cfg=ExitCfg(hard_stop_atr_mult=4.0, target_atr_mult=0.0,
                                  trail_atr_mult=6.0, time_stop_bars=0))

    conn = open_db(db_path)
    upsert_quote(conn, "BTC-USD", 99.0, 101.0, ts=1_000_000.0)
    upsert_position(conn, "paper", Position("BTC-USD", 2.0, 100.0),
                    plan=plan.as_dict(), strategy="trend", entry_reason="ema cross")
    snapshot_equity(conn, "paper", cash=50_000.0, positions_value=50_200.0, peak=100_200.0, ts=1_000_000.0)
    record_order(conn, Order(id="o1", client_order_id="c1", symbol="BTC-USD", side="buy",
                             qty=2.0, type="market", status="filled", filled_qty=2.0,
                             filled_avg_price=100.0, reason="ema cross", strategy="trend"),
                 mode="paper", role="entry")
    conn.close()

    from botcore.dashboard.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()


def test_state_reports_position_and_kpis(client):
    s = client.get("/api/state").json()
    assert s["mode"] == "paper" and s["broker"] == "sim"
    assert s["kpis"]["active_positions"] == 1
    pos = s["positions"][0]
    assert pos["ticker"] == "BTC-USD"
    assert pos["entry_price"] == 100.0
    assert pos["current_price"] == 100.0          # mid of 99/101
    assert pos["stop_price"] == 84.0
    assert s["equity"]["now"] == 100_200.0


def test_equity_and_orders_endpoints(client):
    eq = client.get("/api/equity").json()
    assert eq and eq[-1]["equity"] == 100_200.0
    orders = client.get("/api/orders").json()
    assert orders[0]["id"] == "o1" and orders[0]["role"] == "entry"


def test_quotes_endpoint_adds_spread_and_age(client):
    q = client.get("/api/quotes").json()
    assert q[0]["symbol"] == "BTC-USD"
    assert q[0]["spread_pct"] == pytest.approx(2.0, abs=1e-6)   # (101-99)/100 * 100
    assert q[0]["age_s"] > 0


def test_pause_resume_flatten_flags(client):
    assert client.post("/api/pause").json() == {"paused": True}
    assert client.get("/api/state").json()["paused"] is True
    assert client.post("/api/resume").json() == {"paused": False}

    assert client.post("/api/flatten").json()["flatten_requested"] is True
    conn = open_db(_db_path(client))
    assert get_flag(conn, "flatten_requested") == "1"
    conn.close()


def test_halt_and_clear_halt(client):
    assert client.post("/api/halt").json() == {"halted": True}
    assert client.get("/api/state").json()["halted"] is True
    assert client.post("/api/clear-halt").json() == {"halted": False}
    assert client.get("/api/state").json()["halted"] is False
    # engine picks this up next tick -> RiskState.halted cleared + drawdown re-baselined
    conn = open_db(_db_path(client))
    assert get_flag(conn, "resume_requested") == "1"
    conn.close()


def test_token_enforced_when_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setenv("DASHBOARD_TOKEN", "s3cr3t-long-token")
    _config.get_settings.cache_clear()
    _config.get_config.cache_clear()
    open_db(tmp_path / "bot.db").close()

    from botcore.dashboard.app import create_app

    with TestClient(create_app()) as c:
        assert c.post("/api/pause").status_code == 401
        assert c.post("/api/pause", headers={"X-Dashboard-Token": "s3cr3t-long-token"}).status_code == 200
        # reads stay open
        assert c.get("/api/state").status_code == 200


def _db_path(client) -> str:
    from botcore.config import get_settings

    return get_settings().db_path


def test_state_reports_dead(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    _config.get_settings.cache_clear()
    _config.get_config.cache_clear()
    open_db(tmp_path / "bot.db").close()
    (tmp_path / "DEAD").write_text(
        '{"reason": "equity $90,000 <= deposit floor $95,000", "iso": "2026-08-27T12:00:00"}')

    from botcore.dashboard.app import create_app

    with TestClient(create_app()) as c:
        s = c.get("/api/state").json()
        assert s["dead"] is True
        assert s["kpis"]["protective_state"] == "DEAD"
        assert "deposit floor" in s["dead_certificate"]["reason"]


def test_api_agents_empty_in_single_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    _config.get_settings.cache_clear(); _config.get_config.cache_clear()
    open_db(tmp_path / "bot.db").close()
    from botcore.dashboard.app import create_app
    with TestClient(create_app()) as c:
        assert c.get("/api/agents").json() == []
        assert c.get("/api/state").json()["engine_mode"] == "single"


def test_api_agents_roster_and_dead_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setenv("ENGINE_MODE", "multi")
    _config.get_settings.cache_clear(); _config.get_config.cache_clear()
    conn = open_db(tmp_path / "bot.db")
    from botcore.store.state import record_agent_trade, snapshot_agent_equity
    snapshot_agent_equity(conn, "trend", "paper", 1120.0)
    record_agent_trade(conn, "trend", "BTC-USD", 1, 100, 112, 12.0, kind="shadow", mode="paper")
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents" / "flow.DEAD").write_text('{"reason": "flow lost money", "iso": "x"}')
    conn.close()

    from botcore.dashboard.app import create_app
    with TestClient(create_app()) as c:
        roster = {a["id"]: a for a in c.get("/api/agents").json()}
        assert roster["trend"]["shadow_trades"] == 1
        assert roster["trend"]["shadow_return_pct"] == 12.0
        assert roster["flow"]["dead"] is True
        s = c.get("/api/state").json()
        assert s["engine_mode"] == "multi"
        assert s["kpis"]["agents_dead"] == 1
