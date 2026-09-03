from __future__ import annotations

import time

import pytest

from botcore.rh.ratelimit import TokenBucket


def test_burst_then_throttle():
    b = TokenBucket(rate_per_sec=10.0, capacity=3.0)
    start = time.monotonic()
    for _ in range(3):  # burst capacity, no wait
        assert b.acquire(timeout=0.01)
    burst_elapsed = time.monotonic() - start
    assert burst_elapsed < 0.05

    t0 = time.monotonic()
    assert b.acquire()  # must wait ~0.1s for one token to refill
    assert time.monotonic() - t0 >= 0.05


def test_timeout_returns_false():
    b = TokenBucket(rate_per_sec=1.0, capacity=1.0)
    assert b.acquire()
    assert b.acquire(timeout=0.05) is False


def test_rejects_bad_rate():
    with pytest.raises(ValueError):
        TokenBucket(rate_per_sec=0)
