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
    "live_bot": "poly", "feed": "poly", "feed5m": "poly",
    "accumulation_bot": "accum", "grid_bot": "grid", "swing_bot": "swing",
    "cex_feed": "cex", "status_collector": "status",
    "indicators": "",
}
_TAG_ORDER = ["poly", "accum", "grid", "swing", "ob", "cex", "status"]

# Keyword → tag fallback for the generated bot_id scheme, where bot_name is a unique id
# (e.g. "mexc-grid-lbcusdt-a00f5f") that no longer matches _TAG exactly. Scanned against
# "<bot_type> <bot_name>"; order matters ("cex-feed" before "feed"; "accum" early).
# Order matters: a Polymarket bot's type is e.g. "polymarket-grid", which also contains
# "grid" — so the poly/threshold keys MUST be tested before grid/accum/swing, and "cex-feed"
# before "feed", else the wrong tag wins.
_TAG_KEYWORDS = [("polymarket", "poly"), ("threshold", "poly"),
                 ("accum", "accum"), ("grid", "grid"), ("swing", "swing"),
                 ("cex-feed", "cex"), ("cexfeed", "cex"),
                 ("status", "status"), ("indicators", ""), ("feed", "poly")]


def _tag_for(row: dict) -> str:
    """Category tag for a bot: its legacy bot_name (exact match), else a keyword scan of
    bot_type/bot_name for the generated bot_id scheme ('cex-grid-…' → 'grid')."""
    name = row.get("bot_name", "")
    if name in _TAG:
        return _TAG[name]
    hay = f"{row.get('bot_type', '')} {name}".lower()
    for kw, tag in _TAG_KEYWORDS:
        if kw in hay:
            return tag
    return ""


def display_name(row: dict) -> str:
    """Human-readable name for the status page: the explicit display_name, else the legacy
    bot_name (for un-migrated bots the id IS readable, e.g. 'grid_bot')."""
    return str(row.get("display_name") or row.get("bot_name", ""))


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
        tag = _tag_for(r)
        if tag and tag not in tags_per[idx]:
            tags_per[idx].append(tag)
    labels = []
    for i in range(n):
        ordered = [t for t in _TAG_ORDER if t in tags_per[i]]
        ordered += [t for t in tags_per[i] if t not in _TAG_ORDER]   # unknown tags last
        suffix = f" [{'+'.join(ordered)}]" if ordered else ""
        labels.append(f"acct-{i + 1}{suffix}")
    return labels


def active_account_idxs(rows: list[dict]) -> list[int]:
    """Sorted account_idx values that host at least one inventory bot — i.e. the accounts
    the status page should render. A TEST_USERS entry with NO inventory row (the ephemeral
    clean-install test account) is excluded, so it stops showing as an empty card.
    Labels stay keyed acct-(idx+1) — an excluded index simply leaves a gap in the numbering
    (e.g. no acct-7), which is correct: that slot is not a production bot host."""
    return sorted({int(r.get("account_idx", 0)) for r in rows})


def live_bots(rows: list[dict]) -> set[tuple[str, str]]:
    """{(acct_short, bot_name)} for bots with is_live=true — the real-money set driving the
    SIM/LIVE badge. Keyed acct_short="acct-N" to match generate_status._mode_badge lookup.
    Non-empty since 2026-07-17: the LBCUSDT accumulation bot trades real money. Auto-tracking
    is_live (rather than a manual _LIVE_BOTS list) is what kept this correct on the day it
    happened — a mislabel here shows a real-money bot as SIM."""
    return {(f"acct-{int(r.get('account_idx', 0)) + 1}", r.get("bot_name", ""))
            for r in rows if r.get("is_live") is True}
