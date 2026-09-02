#!/usr/bin/env bash
# Vercel "Ignored Build Step": skip the build when a commit only touched status.json
# (the every-2-min status pushes). Any real change to the site still deploys.
git diff --quiet HEAD^ HEAD -- . ":(exclude)status.json" && exit 0 || exit 1
