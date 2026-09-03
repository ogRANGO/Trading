"""Ed25519 request signing for the Robinhood Crypto API.

Every authenticated request carries three headers:

    x-api-key    : the API key string issued by Robinhood
    x-timestamp  : current unix time in **seconds**
    x-signature  : base64( Ed25519_sign( f"{api_key}{timestamp}{path}{method}{body}" ) )

``path`` must be exactly the request path *including any query string*, and
``method`` must be upper-case (GET / POST). ``body`` is the raw JSON string for
POST requests, or "" when there is no body.
"""

from __future__ import annotations

import base64
import time
from typing import Dict, Optional, Tuple

import nacl.signing


def current_timestamp() -> int:
    return int(time.time())


def load_signing_key(private_key_b64: str) -> nacl.signing.SigningKey:
    """Build a SigningKey from a base64-encoded 32-byte Ed25519 seed."""
    raw = base64.b64decode(private_key_b64)
    if len(raw) == 64:
        # Some tools export seed+public concatenated; the seed is the first 32B.
        raw = raw[:32]
    if len(raw) != 32:
        raise ValueError(
            f"private key must decode to 32 bytes (got {len(raw)}); "
            "regenerate with scripts/generate_keypair.py"
        )
    return nacl.signing.SigningKey(raw)


def build_message(api_key: str, timestamp: int, path: str, method: str, body: str = "") -> str:
    return f"{api_key}{timestamp}{path}{method.upper()}{body}"


def sign(
    private_key_b64: str,
    api_key: str,
    path: str,
    method: str,
    body: str = "",
    timestamp: Optional[int] = None,
) -> Tuple[str, int]:
    """Return ``(signature_b64, timestamp)``."""
    ts = current_timestamp() if timestamp is None else timestamp
    message = build_message(api_key, ts, path, method, body)
    signing_key = load_signing_key(private_key_b64)
    signed = signing_key.sign(message.encode("utf-8"))
    return base64.b64encode(signed.signature).decode("utf-8"), ts


def auth_headers(
    private_key_b64: str,
    api_key: str,
    path: str,
    method: str,
    body: str = "",
    timestamp: Optional[int] = None,
) -> Dict[str, str]:
    signature, ts = sign(private_key_b64, api_key, path, method, body, timestamp)
    return {
        "x-api-key": api_key,
        "x-timestamp": str(ts),
        "x-signature": signature,
    }
