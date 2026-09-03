from __future__ import annotations

import pytest

from botcore.brokers.base import OrderRequest
from botcore.brokers.sim import SimBroker
from botcore.config import FeesCfg


def _broker(**fee_kw):
    kw = dict(commission_pct=0.0, slippage_pct=0.001, crypto_spread_pct=0.0)
    kw.update(fee_kw)
    return SimBroker(starting_cash=10_000.0, fees=FeesCfg(**kw))


def test_market_buy_then_sell_accounts_cash_and_pnl():
    b = _broker()
    b.set_price("AAPL", 100.0)
    o = b.fill_market("AAPL", "buy", 10, ref_price=100.0)
    assert o.filled_avg_price == pytest.approx(100.1)          # +0.1% slippage
    assert b.cash == pytest.approx(10_000 - 1001.0)
    pos = b.get_position("AAPL")
    assert pos.qty == 10 and pos.avg_price == pytest.approx(100.1)

    b.set_price("AAPL", 110.0)
    b.fill_market("AAPL", "sell", 10, ref_price=110.0)
    assert b.get_position("AAPL") is None
    assert b.realized_pnl == pytest.approx((110.0 * 0.999 - 100.1) * 10)
    assert b.get_account().equity == pytest.approx(b.cash)


def test_commission_applied():
    b = _broker(commission_pct=0.01)
    b.set_price("X-USD", 100.0)
    o = b.fill_market("X-USD", "buy", 1, ref_price=100.0)
    assert o.fee == pytest.approx(0.01 * o.filled_avg_price)
    assert b.total_fees == pytest.approx(o.fee)


def test_crypto_spread_widens_cost():
    fees = FeesCfg(slippage_pct=0.0, crypto_spread_pct=0.002)
    b = SimBroker(1_000, fees)
    b.set_price("BTC-USD", 100.0)
    o = b.fill_market("BTC-USD", "buy", 1, ref_price=100.0)
    assert o.filled_avg_price == pytest.approx(100.2)


def test_resting_limit_buy_fills_when_bar_trades_through():
    b = _broker()
    b.set_price("AAPL", 100.0)
    o = b.place_order(OrderRequest("AAPL", "buy", 5, type="limit", limit_price=95.0))
    assert o.status == "accepted"
    filled = b.mark({"AAPL": {"open": 99, "high": 99.5, "low": 94.0, "close": 96}})
    assert filled and b.get_order(o.id).status == "filled"
    assert b.get_position("AAPL").qty == 5


def test_resting_stop_sell_triggers_on_low():
    b = _broker()
    b.set_price("AAPL", 100.0)
    b.fill_market("AAPL", "buy", 10, ref_price=100.0)
    stop = b.place_order(OrderRequest("AAPL", "sell", 10, type="stop", stop_price=95.0))
    b.mark({"AAPL": {"open": 98, "high": 98, "low": 93, "close": 94}})
    assert b.get_order(stop.id).status == "filled"
    assert b.get_position("AAPL") is None


def test_cannot_oversell():
    b = _broker()
    b.set_price("AAPL", 100.0)
    b.fill_market("AAPL", "buy", 3, ref_price=100.0)
    with pytest.raises(Exception):
        b.fill_market("AAPL", "sell", 5, ref_price=100.0)


def test_quote_has_spread_from_costs():
    b = _broker()
    b.set_price("BTC-USD", 100.0)
    q = b.get_quote("BTC-USD")
    assert q.bid < 100.0 < q.ask and q.mid == pytest.approx(100.0)


# -- Phase 5: SQLite persistence ------------------------------------------ #
def test_sim_state_round_trips_across_instances(tmp_path):
    from botcore.store.db import open_db
    from botcore.store.state import SimBrokerStore

    path = tmp_path / "sim.db"
    fees = FeesCfg(commission_pct=0.01, slippage_pct=0.0, crypto_spread_pct=0.0)

    b1 = SimBroker(10_000.0, fees, store=SimBrokerStore(open_db(path)))
    b1.set_price("BTC-USD", 100.0)
    b1.fill_market("BTC-USD", "buy", 3, ref_price=100.0)
    b1.set_price("BTC-USD", 120.0)
    b1.fill_market("BTC-USD", "sell", 1, ref_price=120.0)

    b2 = SimBroker(999.0, fees, store=SimBrokerStore(open_db(path)))
    assert b2.cash == pytest.approx(b1.cash)
    assert b2.realized_pnl == pytest.approx(b1.realized_pnl)
    assert b2.total_fees == pytest.approx(b1.total_fees)
    p1, p2 = b1.get_position("BTC-USD"), b2.get_position("BTC-USD")
    assert p2 is not None and p2.qty == pytest.approx(p1.qty)
    assert p2.avg_price == pytest.approx(p1.avg_price)


def test_sim_state_position_deleted_on_full_exit(tmp_path):
    from botcore.store.db import open_db
    from botcore.store.state import SimBrokerStore, load_sim_state

    path = tmp_path / "sim.db"
    b = SimBroker(10_000.0, FeesCfg(slippage_pct=0.0, crypto_spread_pct=0.0),
                  store=SimBrokerStore(open_db(path)))
    b.set_price("ETH-USD", 50.0)
    b.fill_market("ETH-USD", "buy", 2, ref_price=50.0)
    b.fill_market("ETH-USD", "sell", 2, ref_price=55.0)

    snap = load_sim_state(open_db(path))
    assert snap is not None and snap.positions == {}
    assert snap.realized_pnl == pytest.approx(10.0)


def test_no_store_is_pure_in_memory(tmp_path):
    # the default path — nothing touches disk
    b = SimBroker(1_000.0)
    b.set_price("X-USD", 10.0)
    b.fill_market("X-USD", "buy", 1, ref_price=10.0)
    assert b._store is None
