# rh-crypto-bot

Autonomous Robinhood crypto trading bot: a deterministic trend-following strategy
with an LLM "risk governor" that can only *reduce* risk, hard safety limits, a
paper-trading engine, a backtester, and a local web dashboard.

> **Not investment advice.** A trend-following crypto bot at small size is very
> likely to lose money to fees, spread, and whipsaw. The point of this project is
> the measurement pipeline (backtest + paper trading) and the safety rails. Only
> risk money you can afford to lose. You alone enable live trading.

Full design: [`../.claude/plans/i-want-to-create-cached-seahorse.md`](../.claude/plans/i-want-to-create-cached-seahorse.md)

## Status

| Phase | Scope | State |
| --- | --- | --- |
| 0 | Scaffold + read-only Robinhood crypto API + SQLite + CLI | **done** |
| 1 | Data layer, indicators, 2 signal families, portfolio manager, exit plans, sim broker, risk engine, backtester + risk sweep | **done** |
| 2 | Realtime quote feeds, Alpaca paper broker, execution router + startup reconcile, always-on engine loop, web dashboard v1 (KPIs / positions / ledger / quotes, PAUSE·RESUME·FLATTEN&HALT) | **done** |
| 3 | LLM risk-governor advisor | next |
| 4 | Robinhood MCP live backend, FLATTEN & HALT, $ caps | todo |
| 5 | Unattended hardening — launchd agent (reboot + crash safe), stalled-tick watchdog, ntfy.sh alerts + daily summary, log rotation, SimBroker SQLite persistence, DB retention, runbook | **done** |
| 6 | Stocks on Alpaca paper (`tech_equity` default), **permanent kill-on-loss** (equity ≤ 95% of the anchored deposit → flatten + DEAD + self-disable), graceful no-key degradation | **done** |
| 7 | Multi-agent "trading floor": 4 technical agents + news (RSS) + flow (funding) blended by a coordinator, each with its own **shadow P&L**; an agent that loses past its floor is permanently disabled (`data/agents/<id>.DEAD`) and dropped — the bot keeps trading. `engine_mode: multi` | **done** |

Backtest findings (crypto majors, daily, 2023-05 → 2026-08, keyless Coinbase data):
trend family ≈ +33% total / Sharpe 0.73 / -19% max DD at 1%/trade risk; the risk
sweep shows return and drawdown both scaling with risk fraction, Sharpe peaking
near 2-3%/trade. Mean-reversion barely trades on daily bars (needs intraday).
**Equity backtests need an Alpaca key** — the keyless Yahoo feed rate-limits hard.

## Setup

Requires Python 3.9+ (developed on the system Python 3.9.6; no 3.11 features used).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

### Robinhood API key

1. `.venv/bin/python scripts/generate_keypair.py`
2. Sign in to robinhood.com (web) → Account → Crypto → **Crypto API** → Add key.
   Paste the **public** key. Start with read-only scopes (account + market data).
3. Put the API key string Robinhood gives you into `.env` as `RH_API_KEY`, and the
   printed private key as `RH_PRIVATE_KEY_B64`.

`.env` is gitignored. The private key never leaves your machine.

### Alpaca key — required for stocks

The default config trades US stocks (`active_universe: tech_equity`,
`BROKER=alpaca`). Get free **paper** keys at
[alpaca.markets](https://alpaca.markets) → Paper Trading → API Keys, and put
`ALPACA_KEY_ID` / `ALPACA_SECRET_KEY` in `.env` (keep `ALPACA_PAPER=true`).

Without the keys the engine will **not** trade — it logs `BROKER=alpaca needs
ALPACA_KEY_ID …`, sends one alert, and runs the dashboard only (no crash-loop).
To run keyless on crypto instead, set `BROKER=sim` and either
`active_universe: crypto_major` in `config.yaml` or `ACTIVE_UNIVERSE=crypto_major`
in `.env`.

## Use

```bash
.venv/bin/python -m botcore.cli check                              # config / env / DB / API
.venv/bin/python -m botcore.cli price                              # Robinhood best bid/ask (needs key)

# Backtesting (crypto works keyless; equities need ALPACA_KEY_ID/SECRET)
.venv/bin/python -m botcore.cli backtest --universe crypto_major --signals both --days 1200
.venv/bin/python -m botcore.cli sweep    --universe crypto_major --signals trend
.venv/bin/python scripts/backfill_history.py --universe crypto_major --days 1400

# Paper trading + dashboard (BROKER=sim is keyless; BROKER=alpaca needs a key)
.venv/bin/python -m botcore.cli tick                 # run one engine tick, print JSON
.venv/bin/python -m botcore.serve                    # engine loop + dashboard on :8787
.venv/bin/python -m botcore.serve --no-engine        # dashboard only (inspect a DB)
```

Open `http://127.0.0.1:8787/` for the dashboard (append `?token=…` if you set
`DASHBOARD_TOKEN`). Charts are written to `data/*.png`. There is **no live
order-placement code** yet — `BROKER=sim` simulates fills on live quotes and
`BROKER=alpaca` with `ALPACA_PAPER=true` trades Alpaca's paper account.

### Leaving paper trading running (macOS)

Quick / foreground (survives closing the terminal, **not** a reboot):

```bash
./scripts/paper.sh start      # runs botcore.serve under `caffeinate` (no sleep)
./scripts/paper.sh status     # running? last tick? equity? recent events
./scripts/paper.sh logs       # tail the log
./scripts/paper.sh stop       # graceful SIGTERM
```

On a daily timeframe the trend strategy may sit flat for days before a fresh
EMA/MACD alignment opens a position; that is expected and matches the backtest.
Let it run for weeks, then compare realized P&L / win rate / trade count against
the Phase 1 backtest.

## Runbook — unattended (Phase 5)

Everything here is free: launchd (built into macOS), ntfy.sh (no account),
Tailscale free Personal plan, keyless Coinbase data, local SQLite. **No LLM /
Anthropic calls.** Only cost: the Mac's electricity, and it must stay on **AC
power** (`caffeinate -is` only blocks sleep on AC; optionally
`sudo pmset -c sleep 0`).

### Install / start

```bash
./scripts/launchd.sh install     # render plists -> ~/Library/LaunchAgents, load, start
./scripts/launchd.sh status      # launchd state + last tick / equity / events
./scripts/launchd.sh logs        # tail the rotating logs/paper.log
./scripts/launchd.sh restart     # kickstart the paper agent
./scripts/launchd.sh uninstall   # unload + remove (keeps data/ and logs/)
```

Two agents get installed: `com.rhcryptobot.paper` (the engine + dashboard,
`RunAtLoad` + crash-restart via `KeepAlive`) and `com.rhcryptobot.watchdog` (an
external heartbeat check every 5 min). Both refuse to run unless `BOT_MODE=paper`.
`launchd` captures only early stdout/stderr to `logs/launchd.*.{out,err}`; the
app's own log is the rotating `logs/paper.log` (5 MB × 5).

### Controls

- Dashboard: **Pause / Resume / Flatten & Halt** and **Clear Halt** (clear-halt
  also re-baselines the drawdown peak — a genuine ongoing drawdown is forgiven).
- Or `curl -H "X-Dashboard-Token: <token>" -XPOST http://127.0.0.1:8787/api/pause`.
- Or the file kill switch: `touch data/HALT` to stop, `rm data/HALT` to resume.
- Watchdog: a wedged tick loop → the engine writes `data/HALT`, alerts, and
  `os._exit`s so launchd relaunches it **halted**. Read `logs/paper.log`, then
  Clear Halt from the dashboard (works from your phone).

### Kill-on-loss (permanent)

On the **first** engine boot the deposit is anchored (`bot_flags.initial_equity`,
= the broker's equity then). If mark-to-market equity falls to/through
`(1 + kill_floor_pct) × deposit` — **95% of the deposit by default** — for
`kill_floor_confirm_ticks` (3) consecutive ticks, the engine:

1. flattens every position to cash,
2. writes `data/DEAD` (a JSON certificate) + `data/HALT`,
3. sends an urgent `BOT KILLED — DEAD` alert,
4. `launchctl bootout`s **both** launchd agents,
5. exits 0 — so `KeepAlive` and reboot's `RunAtLoad` leave it stopped.

The dashboard shows a red DEAD banner; `botcore.serve` refuses to start the engine
while `data/DEAD` exists (dashboard-only). Tune the band in `config.yaml` →
`risk.kill_floor_pct` (`0.0` = kill exactly at the deposit; `-0.10` = 10% budget)
and `risk.kill_below_deposit: false` to disable entirely.

**Reviving** (deliberate — review first):

```bash
./scripts/launchd.sh revive            # prints the DEAD certificate + "review logs"
./scripts/launchd.sh revive --confirm  # clears DEAD/HALT, re-anchors the deposit
```

`revive --confirm` deletes `bot_flags.initial_equity` so the floor re-anchors to
your *current* equity on the next boot (you can't un-lose the money — a revive
without re-anchoring would just re-kill on the next tick).

## Multi-agent trading floor (Phase 7)

`config.yaml` ships `engine.engine_mode: multi`. Instead of one signal family the
engine runs a **coordinator** over several agents, each keeping its own **shadow
P&L** (a notional $1000 solo book driven by that agent alone):

| agent | what it reads | data |
| --- | --- | --- |
| `trend` / `mean_reversion` | the existing signal families | price |
| `momentum` | Donchian breakout + ROC | price |
| `vol_regime` | **veto only** — shouts "-1" in a vol spike or down regime | price |
| `news` | Yahoo Finance RSS headlines → lexicon (or LLM) sentiment | free RSS |
| `flow` | Binance perp funding rate + open interest (crypto only, **off by default**) | free public API |

The coordinator enters a name only when `min_agents_agree` agents vote long **and**
the weighted net conviction clears `min_net_conviction` **and** nothing vetoes it.

**Per-agent kill.** When an agent's shadow equity falls to/through
`stake_usd·(1 + kill_floor_pct)` (85% of the stake) after ≥ `min_trades` shadow
trades, it is **permanently disabled** — `data/agents/<id>.DEAD` is written and the
coordinator drops it. The bot keeps trading with the survivors. Revive is
deliberate:

```bash
./scripts/launchd.sh revive-agent momentum --confirm   # then: ./scripts/launchd.sh restart
```

```bash
.venv/bin/python -m botcore.cli agents              # roster + shadow P&L + last signal
```
```bash
.venv/bin/python -m botcore.cli backtest-multi --universe crypto_major --days 1200
```

The backtest prints the blended curve **and each agent standalone**, and writes an
overlay PNG to `data/` — so you can see which agents earn before any real money.
News RSS is thin and laggy and the funding edge is crowded; those two agents start
at small weights and are expected to be the first killed. The LLM news scorer
(`agents.news.engine: llm`) needs `pip install anthropic` + `ANTHROPIC_API_KEY`
and costs credits — the free `lexicon` scorer is the default.

To go back to the single-family engine: `engine_mode: single` in `config.yaml`
(or `ENGINE_MODE=single` in `.env`).

### Notifications (ntfy.sh — free, no account)

```bash
# pick an unguessable topic, put it in .env, subscribe in the ntfy phone app
echo "NTFY_TOPIC=rhbot-$(openssl rand -hex 8)" >> .env
curl -d "test" ntfy.sh/$(grep NTFY_TOPIC .env | cut -d= -f2)
./scripts/launchd.sh restart
```

`NOTIFY_EVENTS` (default `halt,watchdog,error,reconcile,daily`) selects what
pushes; add `fills` / `engine` for more. A daily P&L digest goes out at
`daily_summary_utc_hour`. Optional email via `SMTP_URL` + `ALERT_EMAIL_TO`.

### Phone access — Tailscale (free Personal plan)

1. `brew install tailscale && sudo tailscale up` on the Mac; install Tailscale on
   the phone (same account).
2. In `.env`: set a real long `DASHBOARD_TOKEN`, and `DASHBOARD_HOST=0.0.0.0`
   (or the Mac's `100.x.y.z` tailnet IP). `./scripts/launchd.sh restart`.
3. From the phone: `http://100.x.y.z:8787/?token=<token>`.

Never port-forward 8787 to the public internet.

### Rotate keys / raise the cap / full stop

- **Rotate keys:** `./scripts/launchd.sh uninstall` → new keys in the provider
  dashboard → edit `.env` → `./scripts/launchd.sh install` → revoke old keys
  (config is cached per-process, so a restart is required).
- **Go live** is Phase 4, operator-only: the launchd agents stay `BOT_MODE=paper`
  and refuse otherwise.
- **Full stop:** Flatten from the dashboard if positions are open, then
  `./scripts/launchd.sh uninstall`.
- **Reset the paper account:** stop, `rm data/bot.db*`, restart (or just delete
  the `sim_broker_state` / `sim_positions` rows).

### Crash recovery

Every restart runs `startup_reconcile` before the first tick and the SimBroker
rehydrates cash + open positions from SQLite (`sim_broker_state` / `sim_positions`),
so a crash mid-trial does **not** lose the paper book (Alpaca is the source of
truth for `BROKER=alpaca`). Old `events` / `quotes` are pruned daily
(`events_retention_days` / `quotes_retention_days`) and the WAL is checkpointed.
A crash-restart does **not** trip the kill (that only fires on a real equity
breach); a restart while `data/DEAD` exists stays dashboard-only until you
`revive`.

## Tests

```bash
.venv/bin/python -m pytest
```

## Layout

```
botcore/
  config.py            # .env (Settings) + config.yaml (BotConfig) loaders
  cli.py               # CLI: check / price / backtest / sweep / serve / tick
  serve.py             # `python -m botcore.serve` — engine + dashboard process
  rh/                  # Ed25519 signing, token-bucket limiter, read-only REST client
  data/
    base.py history.py # timeframe/asset helpers + historical-bar loader w/ cache
    quotes.py          # realtime quote feeds (Coinbase keyless / Alpaca)
  strategy/            # indicators, 2 signal families, portfolio manager, exit plans
  risk/                # limits engine, kill switch, market-hours + PDT guards
  brokers/
    base.py            # BrokerClient interface + shared dataclasses
    sim.py             # slippage/commission fill simulator (backtest + paper)
    alpaca.py          # Alpaca paper/live REST broker
  execution/
    router.py          # pick broker + quote feed from mode/config
    reconcile.py       # startup: square the positions table with the broker
  engine/
    loop.py            # TradingEngine: one tick = quotes→mark→exits→signals→entries
    watchdog.py        # pure stalled-tick verdict (thread in loop.py acts on it)
  logging_setup.py     # rotating file log + quiet httpx/apscheduler
  notify/
    push.py summary.py # ntfy.sh + SMTP alerts; daily P&L digest
  backtest/            # replay engine, metrics, plots, run + sweep CLIs
  dashboard/
    app.py static/     # FastAPI read API + controls + single-page UI
  store/
    db.py migrations.sql  # SQLite connect + migrations
    state.py           # repo fns + retention + SimBroker persistence
scripts/
  generate_keypair.py  # Ed25519 keypair for the Robinhood API
  backfill_history.py  # pre-populate the candle cache
  paper.sh             # foreground supervisor (dev)
  launchd.sh watchdog.sh  # unattended install + external heartbeat check
deploy/                # launchd plist templates + run.sh guard wrapper
config.yaml            # universes, strategy params, exit profiles, risk limits, engine knobs
.env.example           # secrets + mode + money caps + notify/log settings (copy to .env)
```

## Safety model (enforced from Phase 1 on)

- `BOT_MODE=paper` by default; `live` is opt-in and `LIVE_MAX_USD` starts at `0`.
- The Robinhood API has no money-movement endpoints — the bot **cannot** deposit
  or withdraw. "Withdraw" in the dashboard = a link to the Robinhood app plus a
  **FLATTEN & HALT** button that sells bot positions to USD and stops.
- Hard limits (per-trade cap, daily loss limit, max drawdown, consecutive-loss
  breaker, anomaly breaker, order-rate cap) + a `HALT` kill switch.
- The LLM advisor can only narrow the tradeable set / cut size / go risk-off. It
  never opens a position or increases size; invalid output ⇒ conservative default.
