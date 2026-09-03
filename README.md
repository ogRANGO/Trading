# Trading

Monorepo for the paper-trading bots and their public status page.

```
trading/
├── rh-crypto-bot/     the bot — strategy, backtester, engine, dashboard, launchd harness
│                      (see rh-crypto-bot/README.md for the full design + runbook)
├── index.html         the public status page (Vercel)
├── config.js          one line: the jsDelivr URL of the status feed
├── vercel.json        static-hosting config
├── vercel-ignore.sh   tells Vercel to skip rebuilds for bot / status-only commits
└── status.json        the live feed, rewritten by the Mac every ~2 min
```

## The bot

Everything about the trading bot lives in [`rh-crypto-bot/`](rh-crypto-bot/) —
start with [`rh-crypto-bot/README.md`](rh-crypto-bot/README.md). Quick start:

```bash
cd rh-crypto-bot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # then fill in keys
.venv/bin/python -m botcore.cli check
.venv/bin/python -m pytest
```

Unattended (macOS launchd): `rh-crypto-bot/scripts/launchd.sh install`.

## The status page

The bots run on the Mac and aren't reachable from the internet, so the Mac
**pushes** `status.json` to this repo every ~2 min and Vercel serves `index.html`,
which reads the feed via jsDelivr. PIN-gated client-side (keeps randoms out, not
real security).

```
Mac timer ─> curl :8787 / :8788 ─> status.json ─> git push (Trading@main)
                                                       │
Vercel ── index.html ── fetch(cdn.jsdelivr.net/gh/ogRANGO/Trading@main/status.json)
```

The publisher is `rh-crypto-bot/scripts/publish_status.sh` +
`rh-crypto-bot/deploy/com.rhcryptobot.publish.plist.template`, installed by
`launchd.sh install`. It writes `status.json` at the repo root and pushes a
`status …`-only commit; `vercel-ignore.sh` keeps those (and bot-only commits)
from redeploying the site.

### One-time: Vercel

1. vercel.com → **Add New → Project → Import** `Trading`
2. Framework Preset **Other** · Build Command *(empty)* · Output Directory *(empty)*
3. **Settings → Git → Ignored Build Step** → `bash vercel-ignore.sh`
4. Deploy → open the `*.vercel.app` URL, enter the PIN.

### Stop the publisher

```bash
launchctl bootout gui/$(id -u)/com.rhcryptobot.publish
rm ~/Library/LaunchAgents/com.rhcryptobot.publish.plist
```
