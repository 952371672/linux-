#!/usr/bin/env bash
set -Eeuo pipefail
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
URL="${CNB_INSTALL_URL:-https://cnb.cool/952371672/cmcc-linux-docker/-/git/raw/main/install.sh}"
curl --http1.1 -fsSL --retry 5 --retry-all-errors --retry-delay 2 --connect-timeout 30 --max-time 300 "$URL" -o "$TMP/install.sh"
chmod 700 "$TMP/install.sh"
exec bash "$TMP/install.sh"
