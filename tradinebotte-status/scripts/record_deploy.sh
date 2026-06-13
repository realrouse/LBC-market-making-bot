#!/usr/bin/env bash
# record_deploy.sh — sourced helper: append a row to the shared deploy journal.
#
# Each backend deploy script sources this and calls, on success (right where it has
# already written version.stamp):
#
#     source "<repo>/tradinebotte-status/scripts/record_deploy.sh"
#     tbnt_record_deploy <account> <bot_name> [result] [mode]
#
# Go/no-go rule: every successful deploy path must call this exactly once, so the
# `deploys` table never drifts from reality.  deploy_all.sh does NOT record — it
# delegates to the backends, so recording here avoids double-counting.
#
# The write is non-fatal: a journal failure prints a warning but never fails a deploy.

# tbnt_record_deploy <account> <bot_name> [result=OK] [mode=full]
# Reads $GIT_HASH from the caller's scope when set, else derives it from the repo.
tbnt_record_deploy() {
    # Escape hatch: a wrapper that reuses another backend as a pure rsync engine sets
    # TBNT_SKIP_JOURNAL=1 so that inner call does not log a row under the wrong bot
    # (e.g. update_claude1.sh drives update_standalone.sh for the account-1 infra).
    [[ -n "${TBNT_SKIP_JOURNAL:-}" ]] && return 0
    local account="$1" bot="$2" result="${3:-OK}" mode="${4:-full}"
    local helper_dir repo git_hash
    helper_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    repo="$(cd "$helper_dir/../.." && pwd)"     # tradinebotte-status/scripts → repo root
    git_hash="${GIT_HASH:-$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo unknown)}"

    if [[ -z "$account" || -z "$bot" ]]; then
        echo "  ! tbnt_record_deploy: missing account/bot — skipping journal" >&2
        return 0
    fi
    python3 "$repo/tradinebotte-status/record_deploy.py" \
        --account "$account" --bot "$bot" --git-hash "$git_hash" \
        --script "$(basename "${0:-unknown}")" --mode "$mode" --result "$result" \
        >/dev/null 2>&1 \
        && echo "  ✓ deploy journal: $account/$bot $git_hash ($result)" \
        || echo "  ! deploy journal write failed (non-fatal): $account/$bot" >&2
    return 0
}
