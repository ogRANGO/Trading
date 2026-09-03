from __future__ import annotations

import pytest

from botcore.brokers.base import Position
from botcore.brokers.sim import SimBroker
from botcore.config import Settings, load_bot_config
from botcore.execution.reconcile import startup_reconcile
from botcore.execution.router import Execution, build_execution
from botcore.store.db import open_db
from botcore.store.state import load_positions, upsert_position


# --------------------------------------------------------------------------- #
# router
# --------------------------------------------------------------------------- #
def _settings(tmp_path, **kw):
    return Settings(_env_file=None, db_path=str(tmp_path / "bot.db"), **kw)


def test_router_sim_uses_simbroker_and_needs_price_feed(tmp_path):
    execu = build_execution(_settings(tmp_path, broker="sim"), load_bot_config())
    assert isinstance(execu.broker, SimBroker)
    assert execu.needs_price_feed is True
    assert execu.mode == "paper"
    assert execu.broker._store is None          # no conn -> in-memory
    execu.close()


def test_router_sim_persists_when_conn_given(tmp_path):
    conn = open_db(tmp_path / "bot.db")
    execu = build_execution(_settings(tmp_path, broker="sim"), load_bot_config(), conn=conn)
    assert execu.broker._store is not None
    # first boot seeds sim_broker_state
    assert conn.execute("SELECT COUNT(*) FROM sim_broker_state").fetchone()[0] == 1
    execu.close()


def test_persisted_sim_position_is_matched_not_dropped(tmp_path):
    from botcore.brokers.base import Position
    from botcore.store.state import SimBrokerStore, upsert_position

    conn = open_db(tmp_path / "bot.db")
    broker = SimBroker(10_000.0, store=SimBrokerStore(conn))
    broker.set_price("BTC-USD", 100.0)
    broker.fill_market("BTC-USD", "buy", 2.0, ref_price=100.0)
    # engine would also mirror the position into the `positions` table:
    upsert_position(conn, "paper", Position("BTC-USD", 2.0, 100.0), plan={"effective_stop": 84.0})

    # a fresh broker rehydrates from sim_positions -> reconcile sees agreement
    broker2 = SimBroker(1.0, store=SimBrokerStore(conn))
    rep = startup_reconcile(conn, broker2, "paper")
    assert rep.matched == ["BTC-USD"]
    assert rep.dropped == [] and rep.adopted == []


def test_router_alpaca_builds_without_network(tmp_path):
    s = _settings(tmp_path, broker="alpaca", alpaca_key_id="k", alpaca_secret_key="s")
    execu = build_execution(s, load_bot_config())
    assert execu.broker.name.startswith("alpaca")
    assert execu.needs_price_feed is False        # engine does not push prices to Alpaca
    assert execu.quotes is execu.broker._quotes
    execu.close()


def test_router_alpaca_missing_credentials_raises(tmp_path):
    from botcore.brokers.base import BrokerError

    with pytest.raises(BrokerError) as exc:
        build_execution(_settings(tmp_path, broker="alpaca"), load_bot_config())
    assert "ALPACA_KEY_ID" in str(exc.value)   # message names the env vars to set


@pytest.mark.parametrize("kind", ["robinhood_mcp", "robinhood_crypto"])
def test_router_live_backends_not_yet_implemented(tmp_path, kind):
    with pytest.raises(NotImplementedError):
        build_execution(_settings(tmp_path, broker=kind), load_bot_config())


def test_execution_close_swallows_errors():
    class Boom:
        def close(self):
            raise RuntimeError("nope")

    Execution(Boom(), Boom(), "paper", needs_price_feed=False).close()  # must not raise


# --------------------------------------------------------------------------- #
# startup reconcile
# --------------------------------------------------------------------------- #
def test_reconcile_clean_when_broker_and_db_empty(tmp_path):
    conn = open_db(tmp_path / "bot.db")
    rep = startup_reconcile(conn, SimBroker(1000.0), "paper")
    assert rep.clean and rep.adopted == [] and rep.dropped == []


def test_reconcile_drops_db_row_missing_at_broker(tmp_path):
    conn = open_db(tmp_path / "bot.db")
    upsert_position(conn, "paper", Position("BTC-USD", 1.0, 100.0), plan={"effective_stop": 90.0})
    rep = startup_reconcile(conn, SimBroker(1000.0), "paper")
    assert rep.dropped == ["BTC-USD"]
    assert load_positions(conn, "paper") == {}


def test_reconcile_adopts_broker_position_missing_in_db(tmp_path):
    conn = open_db(tmp_path / "bot.db")
    broker = SimBroker(10_000.0)
    broker.set_price("ETH-USD", 2000.0)
    broker.fill_market("ETH-USD", "buy", 1.0, ref_price=2000.0)

    rep = startup_reconcile(conn, broker, "paper")
    assert rep.adopted == ["ETH-USD"]
    rows = load_positions(conn, "paper")
    assert "ETH-USD" in rows
    assert rows["ETH-USD"]["entry_reason"] == "adopted-on-boot"
    assert rows["ETH-USD"]["plan_json"] is None      # engine attaches the stop next tick


def test_reconcile_matches_agreeing_position(tmp_path):
    conn = open_db(tmp_path / "bot.db")
    broker = SimBroker(10_000.0)
    broker.set_price("BTC-USD", 100.0)
    broker.fill_market("BTC-USD", "buy", 2.0, ref_price=100.0)
    upsert_position(conn, "paper", Position("BTC-USD", 2.0, 100.0), plan={"effective_stop": 84.0})

    rep = startup_reconcile(conn, broker, "paper")
    assert rep.matched == ["BTC-USD"] and rep.clean
    assert "BTC-USD" in load_positions(conn, "paper")
