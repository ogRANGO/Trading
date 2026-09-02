# bot monitor

A tiny public status page for the two paper-trading bots running on the Mac.
The bots can't be reached from the internet, so the Mac **pushes** a
`status.json` to a data repo every ~2 minutes and this page reads it via jsDelivr.

```
Mac cron ──> curl :8787 / :8788 ──> status.json ──> git push (rh-bot-monitor-data)
                                                          │
Vercel (this repo) ── index.html ── fetch(jsDelivr) ──────┘
```

PIN-gated (client-side only — keeps randoms out, not real security).

## Setup

### 1. Two GitHub repos

| repo | contents | who pushes |
| --- | --- | --- |
| `rh-bot-monitor`      | this folder (the page)   | you, rarely |
| `rh-bot-monitor-data` | just `status.json`       | the Mac, every 2 min |

### 2. Point the page at your data repo

Edit `config.js` — replace `__GH_USER__` with your GitHub username.

### 3. Deploy the page on Vercel

1. vercel.com → **Add New → Project → Import** `rh-bot-monitor`
2. Framework preset: **Other**. No build command. Output dir: leave blank (root).
3. Deploy → you get `https://<name>.vercel.app`

Only pushes to `rh-bot-monitor` redeploy. The data repo never touches Vercel.

### 4. Wire up the publisher on the Mac

```bash
cd ~/rh-crypto-bot

# one-time: clone the data repo next to the bot
git clone https://github.com/<you>/rh-bot-monitor-data.git ~/rh-bot-monitor-data

# render + load the launchd timer (runs every 120s)
UID_=$(id -u)
sed -e "s#__REPO__#$PWD#g" -e "s#__DATA_DIR__#$HOME/rh-bot-monitor-data#g" \
    deploy/com.rhcryptobot.publish.plist.template \
    > ~/Library/LaunchAgents/com.rhcryptobot.publish.plist
launchctl bootstrap gui/$UID_ ~/Library/LaunchAgents/com.rhcryptobot.publish.plist
launchctl kickstart -k gui/$UID_/com.rhcryptobot.publish

# check it
tail -f ~/rh-crypto-bot/logs/publish.out
```

`git push` needs credentials — set up a credential helper or a PAT-in-URL remote
for `~/rh-bot-monitor-data` before loading the timer.

### Stop / remove

```bash
launchctl bootout gui/$(id -u)/com.rhcryptobot.publish
rm ~/Library/LaunchAgents/com.rhcryptobot.publish.plist
```

## Files

- `index.html` — the page (PIN gate + two bot cards, polls every 20s)
- `config.js` — the one line you edit (data feed URL)
- `vercel.json` — static hosting config
- `status.json` — a stale sample; the live one lives in the data repo
