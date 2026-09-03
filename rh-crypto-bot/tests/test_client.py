"""Client behaviour against a mocked httpx transport (no network)."""

from __future__ import annotations

import base64

import httpx
import pytest

from botcore.config import Settings
from botcore.rh.client import RobinhoodAPIError, RobinhoodCryptoClient
from botcore.rh.ratelimit import TokenBucket
from botcore.rh.signing import build_message

SEED_B64 = base64.b64encode(bytes(range(32))).decode()


def _settings() -> Settings:
    return Settings(_env_file=None, rh_api_key="k", rh_private_key_b64=SEED_B64)


def _client_with_handler(handler) -> RobinhoodCryptoClient:
    c = RobinhoodCryptoClient(_settings(), bucket=TokenBucket(rate_per_sec=1000, capacity=1000))
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


def test_get_account_signs_and_parses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.raw_path.decode()
        seen["headers"] = request.headers
        return httpx.Response(200, json={"status": "active", "buying_power": "12.34"})

    c = _client_with_handler(handler)
    acct = c.get_account()
    assert acct["status"] == "active"
    assert c.get_buying_power() == pytest.approx(12.34)
    assert seen["path"] == "/api/v1/crypto/trading/accounts/"
    assert "x-signature" in seen["headers"] and seen["headers"]["x-api-key"] == "k"


def test_query_string_is_part_of_signed_path():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw_path"] = request.url.raw_path.decode()
        captured["ts"] = request.headers["x-timestamp"]
        captured["sig"] = request.headers["x-signature"]
        return httpx.Response(200, json={"results": []})

    c = _client_with_handler(handler)
    c.get_best_bid_ask(["BTC-USD", "ETH-USD"])
    assert captured["raw_path"] == (
        "/api/v1/crypto/marketdata/best_bid_ask/?symbol=BTC-USD&symbol=ETH-USD"
    )
    # signed message must include the exact path+query
    msg = build_message("k", int(captured["ts"]), captured["raw_path"], "GET", "")
    import nacl.signing

    vk = nacl.signing.SigningKey(base64.b64decode(SEED_B64)).verify_key
    vk.verify(msg.encode(), base64.b64decode(captured["sig"]))


def test_429_then_success(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"detail": "slow down"})
        return httpx.Response(200, json={"status": "active"})

    c = _client_with_handler(handler)
    assert c.get_account()["status"] == "active"
    assert calls["n"] == 2


def test_4xx_raises_with_context(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "forbidden"})

    c = _client_with_handler(handler)
    with pytest.raises(RobinhoodAPIError) as ei:
        c.get_account()
    assert ei.value.status == 403
    assert "scopes" in str(ei.value)


def test_missing_credentials_refuses_construction():
    with pytest.raises(RuntimeError):
        RobinhoodCryptoClient(Settings(_env_file=None))
