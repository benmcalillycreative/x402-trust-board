#!/bin/bash
# Self-update: fetch the latest code from GitHub (the main branch) and keep it only if
# the tests pass; otherwise roll back to the code that was running. run.sh calls this
# before each cycle. Exit 10 means new code is in place (run.sh then restarts itself).
# Local files that aren't part of the code (settings.env, your data folder) are never touched.
cd "$(dirname "$0")" || exit 1
[ -d .git ] || exit 0
[ "${X402T_AUTO_UPDATE:-1}" = "1" ] || exit 0
git fetch -q origin main 2>/dev/null || { echo "update: couldn't reach GitHub; keeping current code"; exit 0; }
OLD=$(git rev-parse HEAD 2>/dev/null)
NEW=$(git rev-parse origin/main)
[ "$OLD" = "$NEW" ] && exit 0
git reset -q --hard "$NEW"
TESTLOG="${X402T_DATA_DIR:-$HOME/x402-trust-data}/logs/update-tests.log"
if /usr/bin/env python3 -m unittest discover -s tests > "$TESTLOG" 2>&1; then
  echo "update: ${OLD:0:7} -> ${NEW:0:7} ($(git log -1 --format=%s "$NEW")); tests passed"
  exit 10
fi
echo "update: tests failed on ${NEW:0:7}; rolled back to ${OLD:0:7} (see $TESTLOG)"
[ -n "$OLD" ] && git reset -q --hard "$OLD"
exit 0
