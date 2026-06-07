#!/usr/bin/env bash
# Wrapper: accumulation_bot on the standalone account (index 2, btc_accumulation strategy).
# Override: ACCUM_USER_IDX=<n> bash scripts/deploy_accumulation_claude3.sh
ACCUM_USER_IDX=2 \
BOT_STRATEGY=strategies/accumulation/btc_accumulation.json \
    exec bash "$(dirname "$0")/deploy_accumulation.sh" "$@"
