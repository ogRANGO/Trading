"""Run the trading engine and the dashboard together.

    python -m botcore.serve                 # engine + dashboard
    python -m botcore.serve --no-engine      # dashboard only (inspect a DB)
    python -m botcore.serve --once           # one engine tick, print result, exit

Mode / broker / universe come from .env + config.yaml. Defaults are paper + sim +
crypto_major, which needs no API keys.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path
from typing import List, Optional

from botcore.brokers.base import BrokerError
from botcore.config import REPO_ROOT, get_config, get_settings
from botcore.logging_setup import configure_logging
from botcore.risk.killswitch import DeadSwitch

log = logging.getLogger("botcore.serve")


def _notify_once(settings, message: str) -> None:
    try:
        from botcore.notify.push import get_notifier

        get_notifier(settings).notify(message, title="BOT", priority="urgent",
                                      key="serve-degraded")
    except Exception:  # noqa: BLE001
        pass


def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    cfg = get_config()

    # --once prints JSON to stdout; keep its logs off the rotating paper.log unless asked.
    if args.once and not args.log_dir:
        log_dir = None
    else:
        log_dir = Path(args.log_dir or settings.log_dir or (REPO_ROOT / "logs"))
    configure_logging(verbose=args.verbose, log_dir=log_dir,
                      max_mb=settings.log_max_mb, backups=settings.log_backups)

    dead = DeadSwitch(str(Path(settings.db_path).with_name("DEAD")))
    if dead.dead:
        cert = dead.certificate() or {}
        log.error("DEAD marker present (%s) - engine will NOT start. "
                  "Revive: scripts/launchd.sh revive --confirm", cert.get("reason"))
        _notify_once(settings, f"bot is DEAD: {cert.get('reason')}")
        if args.once:
            print(json.dumps({"dead": True, "certificate": cert}, indent=2, default=str))
            return 0
        args.no_engine = True   # dashboard-only so the certificate stays visible

    if args.once:
        from botcore.engine.loop import TradingEngine

        try:
            eng = TradingEngine(settings, cfg)
        except (BrokerError, NotImplementedError) as exc:
            print(json.dumps({"error": str(exc), "engine": "down"}, indent=2))
            return 0
        print(json.dumps(eng.tick(), indent=2, default=str))
        eng.stop()
        return 0

    engine = None
    if not args.no_engine:
        from botcore.engine.loop import TradingEngine

        try:
            engine = TradingEngine(settings, cfg)
            engine.start()
            print(f"engine: mode={settings.bot_mode} broker={settings.broker} "
                  f"universe={cfg.active_universe} family={cfg.strategy.signal_family}")
        except (BrokerError, NotImplementedError) as exc:
            log.error("engine cannot start: %s", exc)
            _notify_once(settings, f"engine down: {exc}")
            engine = None   # dashboard-only; no crash-loop under launchd

    def _shutdown(*_):
        if engine:
            engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    import uvicorn

    url_token = "" if settings.dashboard_token in ("", "change-me") else f"?token={settings.dashboard_token}"
    print(f"dashboard: http://{settings.dashboard_host}:{settings.dashboard_port}/{url_token}")
    uvicorn.run(
        "botcore.dashboard.app:app",
        host=settings.dashboard_host, port=settings.dashboard_port,
        log_level="warning", access_log=False,
    )
    if engine:
        engine.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="botcore.serve")
    p.add_argument("--no-engine", action="store_true", help="dashboard only")
    p.add_argument("--once", action="store_true", help="run one engine tick and exit")
    p.add_argument("--log-dir", default="", help="rotating log dir (default <repo>/logs)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    return _run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
