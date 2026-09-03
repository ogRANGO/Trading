"""Alpaca broker: symbol + status mapping and order marshalling, no network."""

from __future__ import annotations

import pytest

from botcore.brokers.alpaca import (
    AlpacaBroker,
    _from_alpaca_symbol,
    _to_alpaca_symbol,
)
from botcore.brokers.base import BrokerError, OrderRequest
from botcore.config import Settings


def _broker(**kw):
    s = Settings(_env_file=None, alpaca_key_id="k", alpaca_secret_key="s", **kw)
    return AlpacaBroker(s, paper=True)


def test_missing_credentials_raises():
    with pytest.raises(BrokerError):
        AlpacaBroker(Settings(_env_file=None), paper=True)


@pytest.mark.parametrize("ours,theirs", [
    ("BTC-USD", "BTC/USD"),
    ("ETH-USD", "ETH/USD"),
    ("AAPL", "AAPL"),
    ("SMH", "SMH"),
])
def test_symbol_mapping_roundtrip(ours, theirs):
    assert _to_alpaca_symbol(ours) == theirs
    assert _from_alpaca_symbol(theirs) == ours


def test_paper_vs_live_base_url():
    s = Settings(_env_file=None, alpaca_key_id="k", alpaca_secret_key="s")
    assert AlpacaBroker(s, paper=True)._base.startswith("https://paper-api")
    assert AlpacaBroker(s, paper=False)._base == "https://api.alpaca.markets"


def test_order_status_mapping():
    b = _broker()
    mk = lambda st: b._order({"id": "1", "symbol": "AAPL", "side": "buy", "status": st})
    assert mk("filled").status == "filled"
    assert mk("partially_filled").status == "partially_filled"
    assert mk("pending_new").status == "new"
    assert mk("expired").status == "canceled"
    assert mk("rejected").status == "rejected"
    assert mk("some_new_status_we_never_saw").status == "accepted"
    b.close()


def test_order_marshalling_fills_from_request_when_absent():
    b = _broker()
    req = OrderRequest("BTC-USD", "buy", 0.25, type="market", reason="test", strategy="trend")
    o = b._order({"id": "abc", "symbol": "BTC/USD", "side": "buy", "status": "accepted"}, req)
    assert o.symbol == "BTC-USD"          # normalised back to our form
    assert o.qty == 0.25                  # taken from the request
    assert o.reason == "test" and o.strategy == "trend"
    b.close()


def test_crypto_day_order_becomes_gtc(monkeypatch):
    b = _broker()
    captured = {}

    def fake_req(method, path, **kw):
        captured["method"], captured["path"], captured["body"] = method, path, kw.get("json")
        return {"id": "1", "symbol": kw["json"]["symbol"], "side": kw["json"]["side"], "status": "accepted"}

    monkeypatch.setattr(b, "_req", fake_req)
    b.place_order(OrderRequest("BTC-USD", "buy", 0.1, type="market", time_in_force="day"))
    assert captured["body"]["time_in_force"] == "gtc"     # crypto can't use day
    assert captured["body"]["symbol"] == "BTC/USD"
    b.close()


def test_equity_day_order_stays_day(monkeypatch):
    b = _broker()
    captured = {}
    monkeypatch.setattr(b, "_req", lambda m, p, **kw: (
        captured.update(body=kw.get("json"))
        or {"id": "1", "symbol": kw["json"]["symbol"], "side": kw["json"]["side"], "status": "accepted"}
    ))
    b.place_order(OrderRequest("AAPL", "buy", 3, type="market", time_in_force="day"))
    assert captured["body"]["time_in_force"] == "day"
    b.close()
