#!/usr/bin/env bash
# Wrapper: MEXC accumulation_bot (sim) on account index 1 (btc_accumulation_mexc).
# Price/OBI come from the shared MEXC-derived scalping stream (btc_scalping_mexc,
# registered on demand); macro gates stay on the shared Binance streams.
# Override: ACCUM_USER_IDX=<n> bash scripts/deploy_accumulation_claude2.sh
ACCUM_USER_IDX=1 \
BOT_STRATEGY=strategies/accumulation/btc_accumulation_mexc.json \
    exec bash "$(dirname "$0")/deploy_accumulation.sh" "$@"
