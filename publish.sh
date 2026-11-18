#!/bin/bash
# Publish the latest board to GitHub Pages (the gh-pages branch of X402T_PUBLISH_REPO).
# Every publish replaces the branch with a single fresh commit, so the repo never bloats.
# run.sh calls this after each cycle once X402T_PUBLISH_REPO is set in settings.env.
set -e
cd "$(dirname "$0")"
[ -f ./settings.env ] && set -a && . ./settings.env && set +a

REPO="${X402T_PUBLISH_REPO:-}"
if [ -z "$REPO" ]; then
  echo "Publishing is off (X402T_PUBLISH_REPO is not set in settings.env)."
  exit 0
fi
KEY="${X402T_PUBLISH_KEY:-$HOME/.ssh/x402trust_deploy}"
REPORTS="${X402T_DATA_DIR:-$HOME/x402-trust-data}/reports"
[ -f "$REPORTS/leaderboard.html" ] || { echo "No report yet; run a cycle first."; exit 1; }
[ -f "$KEY" ] || { echo "Deploy key $KEY not found; run ./setup-publish.sh first."; exit 1; }

SITE="$(mktemp -d)"
trap 'rm -rf "$SITE"' EXIT
cp "$REPORTS/leaderboard.html" "$SITE/index.html"
cp "$REPORTS/scores.json" "$SITE/scores.json"
[ -f "$REPORTS/onchain.json" ] && cp "$REPORTS/onchain.json" "$SITE/onchain.json"
[ -f "$REPORTS/payouts.json" ] && cp "$REPORTS/payouts.json" "$SITE/payouts.json"
[ -f "$REPORTS/sellers.html" ] && cp "$REPORTS/sellers.html" "$SITE/sellers.html"
[ -f "$REPORTS/pro.html" ] && cp "$REPORTS/pro.html" "$SITE/pro.html"
[ -f "$REPORTS/method.html" ] && cp "$REPORTS/method.html" "$SITE/method.html"
[ -f "$REPORTS/listing.html" ] && cp "$REPORTS/listing.html" "$SITE/listing.html"
[ -d "$REPORTS/seller" ] && cp -R "$REPORTS/seller" "$SITE/seller"
[ -d "$REPORTS/badge" ] && cp -R "$REPORTS/badge" "$SITE/badge"
[ -d "$REPORTS/state" ] && cp -R "$REPORTS/state" "$SITE/state"
touch "$SITE/.nojekyll"
# Static pages (like verify.html), with the contact address filled in.
for f in site/*.html; do
  [ -f "$f" ] || continue
  if [ -n "${X402T_CONTACT:-}" ]; then
    sed "s|__CONTACT__|$X402T_CONTACT|g" "$f" > "$SITE/$(basename "$f")"
  else
    sed 's|<span class="contact-line">.*</span>||' "$f" > "$SITE/$(basename "$f")"
  fi
done
[ -n "${X402T_PUBLISH_DOMAIN:-}" ] && echo "$X402T_PUBLISH_DOMAIN" > "$SITE/CNAME"

cd "$SITE"
git init -q
git checkout -q -b gh-pages
git add -A
git -c user.name="x402 Trust Board" -c user.email="noreply@users.noreply.github.com" \
    commit -q -m "Board update $(date -u +%Y-%m-%dT%H:%MZ)"
GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
    git push -q -f "${X402T_PUBLISH_REMOTE:-git@github.com:$REPO.git}" gh-pages
echo "Published to https://$(echo "$REPO" | cut -d/ -f1 | tr '[:upper:]' '[:lower:]').github.io/$(echo "$REPO" | cut -d/ -f2)/"
