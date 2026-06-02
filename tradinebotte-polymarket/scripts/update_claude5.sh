#!/usr/bin/env bash
# update_claude5.sh — Push a code update to the BTC 5M Polymarket account and restart live_bot.
#
# Targets TEST_USERS[4] (swing trading tester, TEST_SWING_USER_IDX=4).
#
# Thin wrapper around update_standalone.sh targeting index 4 in TEST_USERS.
# All flags (--skip-restart, --verify-only) are forwarded as-is.
#
# Usage:
#   bash scripts/update_claude5.sh
#   bash scripts/update_claude5.sh --skip-restart
#   bash scripts/update_claude5.sh --verify-only

TEST_STANDALONE_USER_IDX=4 exec bash "$(dirname "$0")/update_standalone.sh" "$@"
