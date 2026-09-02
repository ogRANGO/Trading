#!/usr/bin/env bash
# Vercel "Ignored Build Step": build only the main branch; skip data-branch pushes.
[ "$VERCEL_GIT_COMMIT_REF" = "main" ] && exit 1 || exit 0
