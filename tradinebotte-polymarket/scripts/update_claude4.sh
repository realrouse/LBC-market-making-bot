#!/usr/bin/env bash
# update_claude4.sh — Push a code update to the scalping account and restart live_bot.
#
# Targets TEST_USERS[3] (scalping/orderbook account — also runs Polymarket live_bot).
#
# Thin wrapper around update_standalone.sh targeting index 3 in TEST_USERS.
# All flags (--skip-restart, --verify-only) are forwarded as-is.
#
# Usage:
#   bash scripts/update_claude4.sh
#   bash scripts/update_claude4.sh --skip-restart
#   bash scripts/update_claude4.sh --verify-only

TEST_STANDALONE_USER_IDX=3 TRADINEBOTTE_DATA_SOURCE=feed TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557 \
  exec bash "$(dirname "$0")/update_standalone.sh" "$@"
