"""Command-line entry point for read-only checks (Phase 0).

    python -m botcore.cli check                 # validate config + env + DB
    python -m botcore.cli account               # print account / buying power
    python -m botcore.cli holdings              # print crypto holdings
    python -m botcore.cli pairs [SYM ...]       # trading pairs (defaults: universe)
    python -m botcore.cli price  [SYM ...]      # best bid/ask (defaults: universe)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import List, Optional

from botcore.config import get_config, get_settings
from botcore.store.db import open_db


def _pp(obj) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _client():
    from botcore.rh.client import RobinhoodCryptoClient

    return RobinhoodCryptoClient(get_settings())


def cmd_check(_: argparse.Namespace) -> int:
    settings = get_settings()
    cfg = get_config()
    print(f"mode           : {settings.bot_mode}")
    print(f"live_max_usd   : {settings.live_max_usd}")
    print(f"max_trade_usd  : {settings.max_trade_usd}")
    print(f"active universe: {cfg.active_universe} -> {', '.join(cfg.universe)}")
    print(f"timeframe      : {cfg.market_data.timeframe}")
    print(f"signal family  : {cfg.strategy.signal_family}")
    print(f"alpaca creds   : {'present' if settings.has_alpaca_credentials() else 'missing (keyless fallback)'}")
    print(f"rh crypto creds: {'present' if settings.has_rh_credentials() else 'missing'}")

    conn = open_db(settings.db_path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    print(f"db             : {settings.db_path}")
    print(f"db tables      : {', '.join(tables)}")
    conn.close()

    if not settings.has_rh_credentials():
        print("\nAdd RH_API_KEY and RH_PRIVATE_KEY_B64 to .env, then run "
              "`python -m botcore.cli account`.")
        return 1

    try:
        with _client() as c:
            acct = c.get_account()
        print(f"\nAPI reachable  : yes (account status: {acct.get('status', '?')})")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\nAPI check FAILED: {exc}")
        return 2


def cmd_account(_: argparse.Namespace) -> int:
    with _client() as c:
        acct = c.get_account()
        _pp(acct)
        print(f"\nbuying_power (parsed): {c.get_buying_power()}")
    return 0


def cmd_holdings(_: argparse.Namespace) -> int:
    with _client() as c:
        _pp(c.get_holdings())
    return 0


def _syms(args: argparse.Namespace) -> List[str]:
    return [s.upper() for s in args.symbols] if args.symbols else list(get_config().universe)


def cmd_pairs(args: argparse.Namespace) -> int:
    with _client() as c:
        _pp(c.get_trading_pairs(_syms(args)))
    return 0


def cmd_price(args: argparse.Namespace) -> int:
    with _client() as c:
        syms = _syms(args)
        raw = c.get_best_bid_ask(syms)
        _pp(raw)
        parsed = c.list_universe_prices(syms)
        print("\nparsed:")
        for sym, px in parsed.items():
            spread = (px["ask"] - px["bid"]) / px["mid"] * 100 if px["mid"] else 0
            print(f"  {sym:10s} bid={px['bid']:.6g}  ask={px['ask']:.6g}  spread={spread:.3f}%")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="botcore.cli")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("account").set_defaults(func=cmd_account)
    sub.add_parser("holdings").set_defaults(func=cmd_holdings)

    pp = sub.add_parser("pairs")
    pp.add_argument("symbols", nargs="*")
    pp.set_defaults(func=cmd_pairs)

    pr = sub.add_parser("price")
    pr.add_argument("symbols", nargs="*")
    pr.set_defaults(func=cmd_price)

    bt = sub.add_parser("backtest", help="run a backtest (see botcore.backtest.run)")
    bt.add_argument("rest", nargs=argparse.REMAINDER)
    bt.set_defaults(func=lambda a: _delegate("botcore.backtest.run", a.rest))

    bm = sub.add_parser("backtest-multi", help="multi-agent backtest (coordinator + each agent)")
    bm.add_argument("rest", nargs=argparse.REMAINDER)
    bm.set_defaults(func=lambda a: _delegate("botcore.backtest.multi", a.rest))

    sw = sub.add_parser("sweep", help="risk-per-trade sweep (see botcore.backtest.sweep)")
    sw.add_argument("rest", nargs=argparse.REMAINDER)
    sw.set_defaults(func=lambda a: _delegate("botcore.backtest.sweep", a.rest))

    sv = sub.add_parser("serve", help="run the engine + dashboard (see botcore.serve)")
    sv.add_argument("rest", nargs=argparse.REMAINDER)
    sv.set_defaults(func=lambda a: _delegate("botcore.serve", a.rest))

    tk = sub.add_parser("tick", help="run one engine tick and print the result")
    tk.set_defaults(func=lambda a: _delegate("botcore.serve", ["--once"]))

    sub.add_parser("agents", help="show the multi-agent roster + shadow P&L").set_defaults(func=cmd_agents)
    return p


def cmd_agents(_: argparse.Namespace) -> int:
    from botcore.agents.registry import agent_roster

    s = get_settings()
    conn = open_db(s.db_path)
    roster = agent_roster(conn, s.bot_mode, get_config(), s.db_path)
    print(f"{'agent':16s} {'kind':10s} {'status':8s} {'wt':>4s} {'shadow%':>8s} "
          f"{'trades':>6s} {'win%':>5s} {'attr$':>9s} {'to-kill%':>8s}  last signal")
    for a in roster:
        st = "DEAD" if a["dead"] else ("active" if a["enabled"] else "off")
        ls = a["last_signal"]
        lsig = f"{ls['symbol']} {ls['direction']:+d} {ls['conviction']:.2f}" if ls else "-"
        print(f"{a['id']:16s} {a['kind']:10s} {st:8s} {a['weight']:4.1f} "
              f"{a['shadow_return_pct']:8.2f} {a['shadow_trades']:6d} "
              f"{a['win_rate']*100:5.0f} {a['attributed_pnl']:9.2f} "
              f"{a['distance_to_kill_pct']:8.1f}  {lsig}")
    return 0


def _delegate(module: str, argv):
    import importlib

    return importlib.import_module(module).main(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
