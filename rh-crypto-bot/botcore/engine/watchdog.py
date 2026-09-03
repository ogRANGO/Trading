"""Stalled-tick detection.

The engine bumps a progress timestamp at the end of every ``safe_tick`` (success
or handled failure). If that timestamp stops advancing while the bot is neither
HALTED nor PAUSED, the tick loop is wedged and the process should hard-exit so
launchd relaunches it clean.
"""

from __future__ import annotations

from typing import Optional


def watchdog_verdict(
    now: float,
    last_progress_ts: float,
    threshold_s: float,
    halted: bool,
    paused: bool,
) -> Optional[str]:
    """Return a reason string if the engine looks wedged, else ``None``.

    A stall while HALTED or PAUSED is expected (the tick deliberately returns
    early and does not snapshot), so those never trip the watchdog.
    """
    age = now - last_progress_ts
    if age < threshold_s:
        return None
    if halted or paused:
        return None
    return f"tick stalled {age:.0f}s (threshold {threshold_s:.0f}s)"
