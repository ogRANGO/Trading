#!/usr/bin/env bash
# Vercel "Ignored Build Step" (exit 0 = skip build, exit 1 = build).
#
# This monorepo also holds the bot (rh-crypto-bot/) and the every-2-min
# status.json pushes. Only redeploy the site when a file the site actually
# serves changed.
set -u

# Fast path: the Mac's status pushes all start with "status ".
case "${VERCEL_GIT_COMMIT_MESSAGE:-}" in
  "status "*) echo "status-only push, skipping build"; exit 0 ;;
esac

# Otherwise build only if a site file changed in this push.
SITE_FILES='index.html config.js vercel.json vercel-ignore.sh'
if git diff --quiet HEAD^ HEAD -- $SITE_FILES 2>/dev/null; then
  echo "no site files changed, skipping build"
  exit 0
fi
echo "site files changed, building"
exit 1
