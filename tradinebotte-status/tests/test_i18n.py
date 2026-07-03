"""Unit tests for the i18n layer of generate_status.

The page ships every language inline (one .langbox each) and toggles client-side. The
critical invariants: the per-language dictionaries are complete (identical key sets and
matching placeholders, or one language silently falls back), the render emits a box per
language, no i18n key leaks unrendered into the HTML, and the global language is restored
after rendering.
"""

import json
import os
import re
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import generate_status as g  # noqa: E402

_I18N_DIR = os.path.join(os.path.dirname(__file__), "..", "i18n")


def _load(lang):
    with open(os.path.join(_I18N_DIR, f"{lang}.json"), encoding="utf-8") as f:
        return json.load(f)


def _placeholders(s):
    return set(re.findall(r"\{(\w+)\}", s))


class TestDictionaryParity(unittest.TestCase):
    """Every language must define the same keys with the same placeholders as English."""

    def setUp(self):
        self.en = _load("en")
        self.others = {l: v for l, v in g._I18N.items() if l != "en"}
        self.assertIn("fr", self.others, "fr.json must exist")

    def test_key_sets_identical(self):
        for lang, d in self.others.items():
            self.assertEqual(set(d), set(self.en),
                             f"{lang}.json key set differs from en.json: "
                             f"missing={set(self.en) - set(d)} extra={set(d) - set(self.en)}")

    def test_placeholders_match(self):
        for lang, d in self.others.items():
            for key, en_val in self.en.items():
                self.assertEqual(_placeholders(d.get(key, "")), _placeholders(en_val),
                                 f"{lang}.json['{key}'] placeholders differ from en")

    def test_no_empty_values(self):
        for lang, d in {**{"en": self.en}, **self.others}.items():
            for key, val in d.items():
                self.assertTrue(val.strip(), f"{lang}.json['{key}'] is empty")


class TestTranslateHelper(unittest.TestCase):

    def tearDown(self):
        g._CUR_LANG = "en"

    def test_lookup_and_format(self):
        g._CUR_LANG = "en"
        self.assertEqual(g.t("win_daily"), "Day")
        g._CUR_LANG = "fr"
        self.assertEqual(g.t("win_daily"), "Jour")
        self.assertIn("3", g.t("status_issues", n=3))

    def test_missing_key_falls_back_to_key(self):
        g._CUR_LANG = "en"
        self.assertEqual(g.t("no_such_key_xyz"), "no_such_key_xyz")

    def test_missing_in_lang_falls_back_to_english(self):
        # A key absent from the current language uses the English string, never a crash.
        g._I18N.setdefault("en", {})["_probe"] = "english"
        g._CUR_LANG = "fr"
        try:
            self.assertEqual(g.t("_probe"), "english")
        finally:
            g._I18N["en"].pop("_probe", None)


def _mini_render():
    hb = [{"account": "c2", "bot_name": "live_bot", "_label": "acct-2", "flag": "ALIVE",
           "age_s": 30, "bounds_ok": "ok", "version": "v1",
           "payload": {"pnl_total": 10.0, "daily_pnl": 1.0, "capital": 100}}]
    accounts = [{"version": "v1", "services": [], "live": None, "grids": [], "accum": None}]
    pw = {"c2|live_bot": {"daily": 1.0, "weekly": 5.0, "monthly": 10.0, "alltime": 10.0}}
    return g._render_html(heartbeats=hb, accounts=accounts,
                          generated_at=datetime.now(tz=timezone.utc), collection_s=1.0,
                          pnl_windows=pw)


class TestBilingualRender(unittest.TestCase):

    def test_both_language_boxes_and_selector(self):
        html = _mini_render()
        self.assertIn("class='langbox i18-en'", html)
        self.assertIn("class='langbox i18-fr'", html)
        self.assertIn("setLang(", html)
        self.assertIn("data-title-fr", html)

    def test_headings_present_in_both_languages(self):
        html = _mini_render()
        self.assertIn("Bots — by family", html)     # en
        self.assertIn("Bots — par famille", html)    # fr
        self.assertIn("All systems nominal", html)   # en status
        self.assertIn("Tous les systèmes nominaux", html)  # fr status

    def test_no_i18n_key_leaks_into_html(self):
        html = _mini_render()
        # If t() ever fell back to the raw key, it would appear verbatim in the output.
        for key in ("sb_bots_alive", "win_daily", "h_bots_family", "lbl_capital",
                    "badge_alive", "k_pnl", "footer_generated"):
            self.assertNotIn(key, html, f"i18n key '{key}' leaked into the page")

    def test_current_language_restored_after_render(self):
        before = g._CUR_LANG
        _mini_render()
        self.assertEqual(g._CUR_LANG, before)


if __name__ == "__main__":
    unittest.main()
