#!/usr/bin/env bash
# Wrapper: accumulation_bot on the scalping account (index 3, btc_accumulation_deepdip strategy).
# Override: ACCUM_USER_IDX=<n> bash scripts/deploy_accumulation_claude4.sh
ACCUM_USER_IDX=3 \
BOT_STRATEGY=strategies/accumulation/btc_accumulation_deepdip.json \
    exec bash "$(dirname "$0")/deploy_accumulation.sh" "$@"
