#!/usr/bin/env bash
# update_claude3.sh — Push a code update to the BTC 5M Polymarket account and restart live_bot.
#
# Targets TEST_USERS[2] (5M standalone, full control — no restrictions).
#
# Thin wrapper around update_standalone.sh targeting index 2 in TEST_USERS.
# All flags (--skip-restart, --verify-only) are forwarded as-is.
#
# Usage:
#   bash scripts/update_claude3.sh
#   bash scripts/update_claude3.sh --skip-restart
#   bash scripts/update_claude3.sh --verify-only

TEST_STANDALONE_USER_IDX=2 exec bash "$(dirname "$0")/update_standalone.sh" "$@"
