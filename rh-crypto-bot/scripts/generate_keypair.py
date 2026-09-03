#!/usr/bin/env python3
"""Generate an Ed25519 keypair for the Robinhood Crypto API.

Usage:
    python scripts/generate_keypair.py

Then:
  1. Sign in to robinhood.com on the web -> Account -> Crypto -> "Crypto API".
  2. Choose "Add key", select the actions you want (start with read-only:
     account + market data), and paste the PUBLIC key below when prompted.
  3. Robinhood shows you an API key string. Put it in .env as RH_API_KEY.
  4. Put the PRIVATE key below in .env as RH_PRIVATE_KEY_B64.

The private key never leaves your machine. Do not commit .env.
"""

from __future__ import annotations

import base64
import sys

import nacl.signing


def main() -> int:
    signing_key = nacl.signing.SigningKey.generate()
    private_b64 = base64.b64encode(bytes(signing_key)).decode("utf-8")
    public_b64 = base64.b64encode(bytes(signing_key.verify_key)).decode("utf-8")

    print("=" * 64)
    print("Ed25519 keypair generated. Store the private key secretly.")
    print("=" * 64)
    print()
    print("PUBLIC KEY  (register this with Robinhood):")
    print(f"  {public_b64}")
    print()
    print("PRIVATE KEY (put in .env as RH_PRIVATE_KEY_B64):")
    print(f"  {private_b64}")
    print()
    print("Next: add to your .env file:")
    print(f"  RH_PRIVATE_KEY_B64={private_b64}")
    print("  RH_API_KEY=<the key string Robinhood gives you>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
