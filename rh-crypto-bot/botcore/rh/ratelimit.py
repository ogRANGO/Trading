"""A small thread-safe token bucket for outbound API calls.

Robinhood publishes per-minute limits on the Crypto API. We stay well under them
with a conservative default (~1 request/second, small burst) and additionally
honour ``Retry-After`` on 429 responses (handled in the client).
"""

from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, rate_per_sec: float = 1.0, capacity: float = 3.0) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        self.rate = float(rate_per_sec)
        self.capacity = float(capacity)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now

    def acquire(self, tokens: float = 1.0, timeout: "float | None" = None) -> bool:
        """Block until ``tokens`` are available. Returns False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                needed = tokens - self._tokens
                wait = needed / self.rate
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            time.sleep(max(wait, 0.005))

    @property
    def available(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens
