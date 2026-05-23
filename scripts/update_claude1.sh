#!/usr/bin/env bash
# update_claude1.sh — Push a code update to the BTC 15M Polymarket account and restart live_bot.
#
# Targets TEST_USERS[0] (15M Polymarket collector, tag=102467).
# Retrieve live.db BEFORE any update if you plan to wipe the install.
#
# Thin wrapper around update_standalone.sh targeting index 0 in TEST_USERS.
# All flags (--skip-restart, --verify-only) are forwarded as-is.
#
# Usage:
#   bash scripts/update_claude1.sh
#   bash scripts/update_claude1.sh --skip-restart
#   bash scripts/update_claude1.sh --verify-only

TEST_STANDALONE_USER_IDX=0 exec bash "$(dirname "$0")/update_standalone.sh" "$@"
