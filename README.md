# bot monitor

Public status page for the two paper-trading bots. The bots run on the Mac and
can't be reached from the internet, so the Mac **pushes** a `status.json` to this
repo (`ogRANGO/Trading`, `main`) every ~2 minutes, and the page reads it via
jsDelivr. PIN-gated (client-side — keeps randoms out, not real security).

```
Mac timer ─> curl :8787 / :8788 ─> status.json ─> git push (Trading@main)
                                                       │
Vercel ── index.html ── fetch(cdn.jsdelivr.net/gh/ogRANGO/Trading@main/status.json)
```

Vercel's *Ignored Build Step* skips the build for status.json-only commits, so the
frequent pushes don't redeploy the site.

---

## One-time setup

### 1. Push this repo to `ogRANGO/Trading`

```bash
cd ~/rh-bot-monitor
git remote add origin https://github.com/ogRANGO/Trading.git
git push -u origin main
#   Username: ogRANGO
#   Password: <paste the github_pat_… token>   (macOS Keychain saves it)
```

### 2. Deploy on Vercel

1. vercel.com → **Add New → Project → Import** `Trading`
2. Framework Preset: **Other** · Build Command: *(empty)* · Output Directory: *(empty)*
3. **Settings → Git → Ignored Build Step** →
   `bash vercel-ignore.sh`
4. Deploy → you get `https://<name>.vercel.app`. Open it, enter PIN **1943**.

### 3. Start the publisher on the Mac

```bash
cd ~/rh-crypto-bot
UID_=$(id -u)
sed -e "s#__REPO__#$PWD#g" -e "s#__SITE_DIR__#$HOME/rh-bot-monitor#g" \
    deploy/com.rhcryptobot.publish.plist.template \
    > ~/Library/LaunchAgents/com.rhcryptobot.publish.plist
launchctl bootstrap gui/$UID_ ~/Library/LaunchAgents/com.rhcryptobot.publish.plist
launchctl kickstart -k gui/$UID_/com.rhcryptobot.publish
tail -f ~/rh-crypto-bot/logs/publish.out     # should log "pushed + purged" every 2 min
```

That's it. The page now updates itself; nothing to touch.

### Stop the publisher

```bash
launchctl bootout gui/$(id -u)/com.rhcryptobot.publish
rm ~/Library/LaunchAgents/com.rhcryptobot.publish.plist
```

---

## Files

| file | what |
| --- | --- |
| `index.html`      | the page — PIN gate, two bot cards, polls every 20s |
| `config.js`       | one line: the jsDelivr URL of the status feed |
| `vercel-ignore.sh`| makes Vercel skip status-only commits |
| `vercel.json`     | static hosting config |
| `status.json`     | the live feed, rewritten by the Mac every 2 min |

Publisher lives in the bot repo: `scripts/publish_status.sh` +
`deploy/com.rhcryptobot.publish.plist.template`.
