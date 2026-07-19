"""Unit tests for inventory_labels — account-label + live-bot derivation from inventory.

The live_bots test is the load-bearing one: it decides whether a real-money bot is badged
LIVE or silently shown as SIM. Since 2026-07-17 exactly one bot (LBCUSDT accumulation) is
live, and the real-inventory test pins that set — so an accidental second live bot fails
the suite rather than quietly trading real funds.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import inventory_labels as il  # noqa: E402


class TestAccountLabels(unittest.TestCase):

    def test_derives_bracket_from_bots_ordered_and_deduped(self):
        rows = [
            {"account_idx": 0, "bot_name": "indicators"},      # "" → omitted
            {"account_idx": 0, "bot_name": "feed"},            # poly (acct-1 is infra: the feed carries the poly tag)
            {"account_idx": 0, "bot_name": "cex_feed"},        # cex
            {"account_idx": 0, "bot_name": "status_collector"},  # status
            {"account_idx": 1, "bot_name": "live_bot"},        # poly
            {"account_idx": 1, "bot_name": "accumulation_bot"},  # accum
        ]
        labels = il.account_labels(rows)
        self.assertEqual(labels[0], "acct-1 [poly+cex+status]")   # _TAG_ORDER, indicators omitted
        self.assertEqual(labels[1], "acct-2 [poly+accum]")

    def test_real_inventory_labels(self):
        # Snapshot of the live inventory.toml labels. bot_names are now generated bot_ids
        # (mexc-grid-lbcusdt-…), so the tag comes from the keyword scan, not an exact _TAG
        # match. The ephemeral clean-install test account (no inventory rows → empty) is
        # excluded from the page by active_account_idxs, leaving a numbering gap; the index
        # after it is a real-money account.
        labels = il.account_labels(il.load_rows())
        self.assertEqual(labels, [
            "acct-1 [poly+cex+status]",
            "acct-2 [poly+accum]",
            "acct-3 [poly+accum+grid]",
            "acct-4 [poly+accum]",
            "acct-5 [swing]",
            "acct-6 [grid]",
            "acct-7",
            "acct-8 [accum+grid]",
        ])

    def test_account_with_no_tagged_bots_has_no_bracket(self):
        labels = il.account_labels([{"account_idx": 0, "bot_name": "indicators"}])
        self.assertEqual(labels, ["acct-1"])

    def test_unknown_bot_tag_falls_back_to_plain(self):
        # A brand-new bot_name with no _TAG entry contributes nothing (no crash).
        labels = il.account_labels([{"account_idx": 0, "bot_name": "future_bot"}])
        self.assertEqual(labels, ["acct-1"])

    def test_empty_rows_returns_empty(self):
        self.assertEqual(il.account_labels([]), [])


class TestLiveBots(unittest.TestCase):

    def test_all_sim_is_empty(self):
        rows = [{"account_idx": 1, "bot_name": "live_bot", "is_live": False},
                {"account_idx": 2, "bot_name": "grid_bot"}]          # is_live absent
        self.assertEqual(il.live_bots(rows), set())

    def test_live_bot_is_keyed_acct_short(self):
        rows = [{"account_idx": 1, "bot_name": "live_bot", "is_live": True},
                {"account_idx": 3, "bot_name": "accumulation_bot", "is_live": True},
                {"account_idx": 4, "bot_name": "swing_bot", "is_live": False}]
        self.assertEqual(
            il.live_bots(rows),
            {("acct-2", "live_bot"), ("acct-4", "accumulation_bot")},
        )

    def test_real_inventory_live_set_is_exactly_the_known_real_money_bot(self):
        """Pins the real-money set to the ONE bot we intend to be live (the LBCUSDT
        accumulation bot, armed 2026-07-17). This replaces the old all-sim assertion:
        the point was never "nothing is live", it was "nothing is live BY ACCIDENT".
        A second bot appearing here means real funds are at stake somewhere unreviewed."""
        self.assertEqual(il.live_bots(il.load_rows()),
                         {("acct-8", "mexc-accumulation-lbcusdt-955a99")})


class TestFailSoft(unittest.TestCase):

    def test_missing_file_returns_empty(self):
        self.assertEqual(il.load_rows("/nonexistent/inventory.toml"), [])

    def test_malformed_file_returns_empty(self):
        p = f"/tmp/_bad_inv_{os.getpid()}.toml"
        with open(p, "w", encoding="utf-8") as f:
            f.write("this is [ not valid toml =")
        try:
            self.assertEqual(il.load_rows(p), [])
        finally:
            os.remove(p)


if __name__ == "__main__":
    unittest.main()
