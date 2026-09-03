"""Signing is deterministic and verifies against the public key."""

from __future__ import annotations

import base64

import nacl.signing
from nacl.exceptions import BadSignatureError

import pytest

from botcore.rh.signing import auth_headers, build_message, load_signing_key, sign

# A fixed throwaway seed (32 bytes) so the test is deterministic.
SEED_B64 = base64.b64encode(bytes(range(32))).decode()
API_KEY = "rh-test-api-key"


def _verify_key_b64() -> str:
    sk = nacl.signing.SigningKey(base64.b64decode(SEED_B64))
    return base64.b64encode(bytes(sk.verify_key)).decode()


def test_message_format():
    msg = build_message("K", 1730000000, "/api/v1/x/", "get", '{"a":1}')
    assert msg == 'K1730000000/api/v1/x/GET{"a":1}'


def test_sign_is_deterministic_with_fixed_timestamp():
    sig1, ts1 = sign(SEED_B64, API_KEY, "/api/v1/crypto/trading/accounts/", "GET", "", timestamp=42)
    sig2, ts2 = sign(SEED_B64, API_KEY, "/api/v1/crypto/trading/accounts/", "GET", "", timestamp=42)
    assert sig1 == sig2 and ts1 == ts2 == 42


def test_signature_verifies_against_public_key():
    path, method, body, ts = "/api/v1/crypto/marketdata/best_bid_ask/?symbol=BTC-USD", "GET", "", 123
    sig_b64, _ = sign(SEED_B64, API_KEY, path, method, body, timestamp=ts)

    verify_key = nacl.signing.VerifyKey(base64.b64decode(_verify_key_b64()))
    message = build_message(API_KEY, ts, path, method, body).encode()
    verify_key.verify(message, base64.b64decode(sig_b64))  # raises on failure

    with pytest.raises(BadSignatureError):
        verify_key.verify(b"tampered", base64.b64decode(sig_b64))


def test_auth_headers_shape():
    h = auth_headers(SEED_B64, API_KEY, "/p/", "GET", "", timestamp=7)
    assert h["x-api-key"] == API_KEY
    assert h["x-timestamp"] == "7"
    assert base64.b64decode(h["x-signature"])  # valid base64


def test_load_signing_key_rejects_bad_length():
    with pytest.raises(ValueError):
        load_signing_key(base64.b64encode(b"too-short").decode())
