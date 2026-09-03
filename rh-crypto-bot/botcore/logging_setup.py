"""Central logging config for ``botcore.serve`` — rotating file + quiet third parties.

Never attaches a handler to **stdout**: ``botcore.serve --once`` prints its JSON
result there and log lines must not corrupt it. Console logging goes to stderr.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_NOISY = (
    "httpx",
    "httpcore",
    "apscheduler.executors.default",
    "apscheduler.scheduler",
    "uvicorn.access",
)


def configure_logging(
    *,
    verbose: bool = False,
    log_dir: Optional[Path] = None,
    console: bool = True,
    max_mb: int = 5,
    backups: int = 5,
) -> None:
    """(Re)configure the root logger. Idempotent — clears existing handlers first."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter(_FORMAT)

    if console:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_dir / "paper.log",
            maxBytes=max(int(max_mb), 1) * 1_000_000,
            backupCount=max(int(backups), 1),
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
