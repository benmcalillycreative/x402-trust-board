#!/bin/bash
# One full cycle: crawl the catalog, probe everything, score, write the report.
# The schedule calls this; you can also run it by hand.
cd "$(dirname "$0")" || exit 1
[ -f ./settings.env ] && set -a && . ./settings.env && set +a
mkdir -p "${X402T_DATA_DIR:-$HOME/x402-trust-data}/logs"
# Never let two cycles overlap (a slow cycle could still be running when the next starts).
LOCK="${X402T_DATA_DIR:-$HOME/x402-trust-data}/.running"
if ! mkdir "$LOCK" 2>/dev/null; then
  # Clear a lock left behind by a crash (older than 12 hours).
  if [ -n "$(find "$LOCK" -maxdepth 0 -mmin +720 2>/dev/null)" ]; then
    rmdir "$LOCK" && mkdir "$LOCK" || exit 0
  else
    exit 0
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
LOG="${X402T_DATA_DIR:-$HOME/x402-trust-data}/logs/$(date +%Y-%m-%d).log"
# Pick up new code first. If there is some, start over with the new run.sh.
if [ -z "${X402T_JUST_UPDATED:-}" ] && [ -x ./update.sh ]; then
  echo "=== $(date) update check ===" >> "$LOG"
  ./update.sh >> "$LOG" 2>&1
  if [ $? -eq 10 ]; then
    rmdir "$LOCK" 2>/dev/null
    trap - EXIT
    X402T_JUST_UPDATED=1 exec /bin/bash ./run.sh
  fi
fi
{
  echo "=== $(date) ==="
  /usr/bin/env python3 -m x402trust run && ./publish.sh
} >> "$LOG" 2>&1
