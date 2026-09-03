"""File-based kill switch.

Any process (a cron job, you at a shell, the dashboard, an auto-trigger) can stop
the bot by creating the HALT file. The engine checks :pyattr:`engaged` every tick
and, when it flips on, cancels open orders and stops entering. FLATTEN also
market-sells to cash.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional


class KillSwitch:
    def __init__(self, path: "str | Path") -> None:
        self.path = Path(path)

    @property
    def engaged(self) -> bool:
        return self.path.exists()

    def engage(self, reason: str, *, source: str = "manual", data: Optional[dict] = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"reason": reason, "source": source, "ts": time.time(),
                   "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "data": data or {}}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def info(self) -> Optional[dict]:
        if not self.engaged:
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {"reason": "unreadable HALT file"}


class DeadSwitch:
    """Permanent kill marker.

    ``KillSwitch`` / HALT is human-clearable at any time (dashboard button, ``rm``).
    ``DEAD`` means the bot lost money past the deposit floor: it flattened to cash,
    disabled its own launchd agents, and exited. It must not trade again until a
    human reviews ``logs/paper.log`` + this certificate and runs
    ``scripts/launchd.sh revive --confirm``.
    """

    def __init__(self, path: "str | Path") -> None:
        self.path = Path(path)

    @property
    def dead(self) -> bool:
        return self.path.exists()

    def kill(self, reason: str, *, source: str = "engine", **data) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"reason": reason, "source": source, "ts": time.time(),
                   "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "data": data}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def certificate(self) -> Optional[dict]:
        if not self.dead:
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {"reason": "unreadable DEAD file"}

    def revive(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
