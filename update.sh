#!/usr/bin/env bash
# Update Diskrisk from git without touching /etc/diskrisk/config.env
#
#   cd /opt/diskrisk && sudo ./update.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo $0" >&2
  exit 1
fi

if [[ ! -d .git ]]; then
  echo "This directory is not a git checkout (${REPO_ROOT})." >&2
  echo "Clone the repo to /opt/diskrisk (recommended), or pull upstream and re-run ./install.sh --copy." >&2
  exit 1
fi

echo "Updating $(pwd) …"
# Prefer fast-forward; fall back to pull with rebase message if needed
if ! git pull --ff-only; then
  echo "git pull --ff-only failed. Fix the checkout (stash/commit local changes), then retry." >&2
  exit 1
fi

# Refresh symlinks/unit; never overwrite config; restart service
./install.sh --in-place --restart

echo "Update complete. Config unchanged: /etc/diskrisk/config.env"
