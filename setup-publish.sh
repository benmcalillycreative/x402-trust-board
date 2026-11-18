#!/bin/bash
# One-time setup for publishing the board to GitHub Pages.
# Usage: ./setup-publish.sh your-github-name/your-repo-name
set -e
cd "$(dirname "$0")"
REPO="$1"
if [ -z "$REPO" ] || [[ "$REPO" != */* ]]; then
  echo "Usage: ./setup-publish.sh your-github-name/your-repo-name"
  exit 1
fi
KEY="$HOME/.ssh/x402trust_deploy"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
if [ ! -f "$KEY" ]; then
  ssh-keygen -q -t ed25519 -N "" -C "x402trust deploy key ($(hostname -s))" -f "$KEY"
fi

# Remember the repo in settings.env (replace any earlier value).
touch settings.env
grep -v '^X402T_PUBLISH_REPO=' settings.env > settings.env.tmp || true
mv settings.env.tmp settings.env
echo "X402T_PUBLISH_REPO=$REPO" >> settings.env

cat <<EOF

Almost done. On GitHub, open:
  https://github.com/$REPO/settings/keys/new

  Title:  Settle Watch (mini)
  Key:    paste the line below
  Tick:   Allow write access
  Then:   Add key

$(cat "$KEY.pub")

Then run:   ./publish.sh
And turn on Pages at https://github.com/$REPO/settings/pages
  Source: Deploy from a branch  ·  Branch: gh-pages  ·  Folder: / (root)  ·  Save
EOF
