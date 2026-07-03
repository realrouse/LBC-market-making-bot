#!/usr/bin/env bash
# deploy_all.sh — thin shim over the inventory-driven orchestrator (scripts/deploy.py).
#
# The account→deploy-script mapping is no longer hardcoded here: it is derived from
# inventory.toml (the single source of truth). This wrapper is kept as the stable
# entrypoint (e.g. the "bot upgrades" trigger) and forwards every argument as-is.
#
# Usage (unchanged):
#   bash scripts/deploy_all.sh                 # full deploy (account-1 rsync-only)
#   bash scripts/deploy_all.sh --restart-infra # also restart account-1 feeds/services
#   bash scripts/deploy_all.sh --skip-restart  # rsync only, no bot restarts
#   bash scripts/deploy_all.sh --verify-only   # status check, no changes
#   bash scripts/deploy_all.sh --list          # print the derived plan and exit
#   bash scripts/deploy_all.sh --dry-run       # print each step, no execution
#
# The logic (ordering, account-1 special-casing, summary) lives in scripts/deploy.py.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# tomllib requires Python 3.11+; prefer the project .venv, fall back to python3.
if [[ -x "$REPO/.venv/bin/python3" ]]; then
    PYTHON="$REPO/.venv/bin/python3"
else
    PYTHON="python3"
fi

exec "$PYTHON" "$REPO/scripts/deploy.py" "$@"
