#!/usr/bin/env bash
# Vercel "Ignored Build Step". The Mac's every-2-min status pushes use commit
# messages that start with "status " -> skip those. Everything else builds.
case "${VERCEL_GIT_COMMIT_MESSAGE:-}" in
  "status "*) echo "status-only push, skipping build"; exit 0 ;;
  *) echo "real change, building"; exit 1 ;;
esac
