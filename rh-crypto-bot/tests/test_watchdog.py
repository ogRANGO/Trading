from __future__ import annotations

import threading
import time

import numpy as np

from botcore.engine.watchdog import watchdog_verdict
from botcore.store.db import open_db


def test_fresh_progress_no_verdict():
    assert watchdog_verdict(now=1000.0, last_progress_ts=995.0, threshold_s=600,
                            halted=False, paused=False) is None


def test_stale_and_running_trips():
    v = watchdog_verdict(now=2000.0, last_progress_ts=1000.0, threshold_s=600,
                         halted=False, paused=False)
    assert v is not None and "stalled" in v


def test_stale_but_halted_does_not_trip():
    assert watchdog_verdict(now=2000.0, last_progress_ts=1000.0, threshold_s=600,
                            halted=True, paused=False) is None


def test_stale_but_paused_does_not_trip():
    assert watchdog_verdict(now=2000.0, last_progress_ts=1000.0, threshold_s=600,
                            halted=False, paused=True) is None


def test_watchdog_loop_calls_injected_action_when_stalled(tmp_path):
    """The real _watchdog_restart os._exit()s — only ever test with an injected action."""
    from tests.test_engine import _engine

    eng, _ = _engine(tmp_path, {"BTC-USD": 100.0}, _bars300())
    calls = []
    eng._wd_action = calls.append
    eng.cfg.engine.watchdog_stall_seconds = 1
    eng.cfg.engine.watchdog_check_seconds = 1
    eng._last_progress_ts = time.time() - 100          # already stale
    eng._wd_stop = threading.Event()
    eng._wd_conn = open_db(eng.settings.db_path)

    t = threading.Thread(target=eng._watchdog_loop, daemon=True)
    t.start()
    try:
        deadline = time.time() + 6
        while time.time() < deadline and not calls:
            time.sleep(0.2)
        assert calls and "stalled" in calls[0]
    finally:
        eng._wd_stop.set()
        t.join(timeout=3)
        eng._wd_conn.close()


def test_watchdog_loop_quiet_when_progressing(tmp_path):
    from tests.test_engine import _engine

    eng, _ = _engine(tmp_path, {"BTC-USD": 100.0}, _bars300())
    calls = []
    eng._wd_action = calls.append
    eng.cfg.engine.watchdog_stall_seconds = 1
    eng.cfg.engine.watchdog_check_seconds = 1
    eng._wd_stop = threading.Event()
    eng._wd_conn = open_db(eng.settings.db_path)

    t = threading.Thread(target=eng._watchdog_loop, daemon=True)
    t.start()
    try:
        for _ in range(8):
            eng._last_progress_ts = time.time()        # keep it fresh
            time.sleep(0.2)
        assert calls == []
    finally:
        eng._wd_stop.set()
        t.join(timeout=3)
        eng._wd_conn.close()


def _bars300():
    import pandas as pd

    idx = pd.date_range("2024-01-01", periods=300, freq="D", tz="UTC")
    c = pd.Series(np.full(300, 100.0), index=idx)
    return {"BTC-USD": pd.DataFrame(
        {"open": c, "high": c * 1.02, "low": c * 0.98, "close": c, "volume": 1.0})}
