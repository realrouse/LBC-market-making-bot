#!/usr/bin/env bash
# update_claude2.sh — Push a code update to the BTC 5M Polymarket account and restart live_bot.
#
# Targets TEST_USERS[1] (5M piste3, tag=102892, obi_reject_thresh=-0.65).
#
# Thin wrapper around update_standalone.sh targeting index 1 in TEST_USERS.
# All flags (--skip-restart, --verify-only) are forwarded as-is.
#
# Usage:
#   bash scripts/update_claude2.sh
#   bash scripts/update_claude2.sh --skip-restart
#   bash scripts/update_claude2.sh --verify-only

TEST_STANDALONE_USER_IDX=1 TRADINEBOTTE_DATA_SOURCE=feed TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557 \
  exec bash "$(dirname "$0")/update_standalone.sh" "$@"
