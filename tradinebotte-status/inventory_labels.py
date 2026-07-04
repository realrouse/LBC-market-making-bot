"""Derive account display labels + the live-bot set from inventory.toml.

inventory.toml is the single source of truth for the fleet topology; this lets
generate_status.py and bot_status.sh stop hard-coding it (the third/fourth copies after
deploy_all.sh was converged in Phase 1/2). Everything here is fail-soft: on any problem
`load_rows` returns [] and the callers degrade to plain `acct-N` labels + no live bots,
so a missing/malformed inventory never takes down the live status page.
"""

from __future__ import annotations

import os
import tomllib

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INVENTORY = os.path.join(os.path.dirname(_HERE), "inventory.toml")

# bot_name → short category tag for the account-label bracket. "" = omit from the summary
# (indicators is ubiquitous shared infra and would just add noise). Derived, so adding a
# bot to inventory updates the labels automatically — no hand-curated per-account string.
_TAG = {
    "live_bot": "poly", "account_bot": "poly", "feed": "poly", "feed5m": "poly",
    "accumulation_bot": "accum", "grid_bot": "grid", "swing_bot": "swing",
    "orderbook_bot": "ob", "cex_feed": "cex", "status_collector": "status",
    "indicators": "",
}
_TAG_ORDER = ["poly", "accum", "grid", "swing", "ob", "cex", "status"]


def load_rows(path: str = DEFAULT_INVENTORY) -> list[dict]:
    """[[bot]] rows from inventory.toml; [] on ANY error (caller degrades gracefully)."""
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh).get("bot", [])
    except Exception:
        return []


def account_labels(rows: list[dict]) -> list[str]:
    """["acct-1 [poly+cex+status]", …] indexed by account_idx (0-based → acct-1…).

    The bracket is the ordered, de-duplicated set of category tags for the bots on that
    account. Returns [] for empty input so the caller can pick its own fallback.
    """
    if not rows:
        return []
    n = max(int(r.get("account_idx", 0)) for r in rows) + 1
    tags_per: list[list[str]] = [[] for _ in range(n)]
    for r in rows:
        idx = int(r.get("account_idx", 0))
        tag = _TAG.get(r.get("bot_name", ""), "")
        if tag and tag not in tags_per[idx]:
            tags_per[idx].append(tag)
    labels = []
    for i in range(n):
        ordered = [t for t in _TAG_ORDER if t in tags_per[i]]
        ordered += [t for t in tags_per[i] if t not in _TAG_ORDER]   # unknown tags last
        suffix = f" [{'+'.join(ordered)}]" if ordered else ""
        labels.append(f"acct-{i + 1}{suffix}")
    return labels


def live_bots(rows: list[dict]) -> set[tuple[str, str]]:
    """{(acct_short, bot_name)} for bots with is_live=true — the real-money set driving the
    SIM/LIVE badge. Keyed acct_short="acct-N" to match generate_status._mode_badge lookup.
    All bots are sim today, so this is empty — its value is auto-tracking is_live the day a
    bot goes live, removing the manual _LIVE_BOTS sync (a mislabel there = a real-money bot
    shown as SIM)."""
    return {(f"acct-{int(r.get('account_idx', 0)) + 1}", r.get("bot_name", ""))
            for r in rows if r.get("is_live") is True}
