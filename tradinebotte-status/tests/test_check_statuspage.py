"""Unit tests for check_statuspage.evaluate — the pure HTTP-quality grader.

All cases run against synthetic HTML (no network): a representative healthy page plus
targeted mutations for each failure mode the checker is meant to catch.
"""

import os
import sys
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import check_statuspage as c  # noqa: E402


NOW = 1_770_000_000  # fixed reference epoch


def _stamp(age_s: int) -> str:
    """A 'Generated … UTC' body stamp `age_s` seconds before NOW."""
    dt = datetime.fromtimestamp(NOW - age_s, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M")


def _good_body(age_s: int = 30) -> str:
    return f"""<!DOCTYPE html><html><head>
<title>tradinebotte — status</title></head>
<body class="win-daily">
<h1><span class="dot ok"></span>
  tradinebotte — All systems nominal
  <span>{_stamp(age_s)} UTC</span>
</h1>
<div class='summary-bar'>…</div>
<div class="win-toggle">
  <button onclick="setWin('daily')">Jour</button>
  <button onclick="setWin('weekly')">Semaine</button>
  <button onclick="setWin('monthly')">Mois</button>
  <button onclick="setWin('alltime')">Depuis reset</button>
</div>
<h2>Bots — par famille</h2>
<div class='families'><span class='pw pw-weekly'><span class='pnl-pos'>+$45.21</span></span><span class='pw pw-monthly'><span class='pnl-pos'>+$226.57</span></span></div>
<h2>Comptes</h2><div class="accounts"></div>
<div class="footer">Generated {_stamp(age_s)} UTC</div>
<script>function setWin(w){{}}</script>
</body></html>"""


def _meta(**over) -> dict:
    m = {"error": None, "http_code": 200, "content_type": "text/html",
         "size": 55000, "elapsed": 0.02}
    m.update(over)
    return m


def _by_name(results):
    return {r.name: r for r in results}


class TestEvaluate(unittest.TestCase):

    def test_healthy_page_all_pass(self):
        results = c.evaluate(_good_body(30), _meta(), NOW)
        self.assertTrue(all(r.status is c.Status.PASS for r in results),
                        msg=[(r.name, r.status.name, r.detail) for r in results
                             if r.status is not c.Status.PASS])

    def test_headline_is_h1_not_title(self):
        # Regression: must capture the H1 status text, not the <title>'s "status".
        res = _by_name(c.evaluate(_good_body(), _meta(), NOW))
        self.assertEqual(res["fleet headline"].detail, "All systems nominal")

    def test_curl_error_short_circuits(self):
        results = c.evaluate("", _meta(error="Could not resolve host"), NOW)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].name, "reachable")
        self.assertIs(results[0].status, c.Status.FAIL)

    def test_non_200_short_circuits(self):
        results = c.evaluate("whatever", _meta(http_code=404), NOW)
        self.assertEqual(len(results), 1)
        self.assertIs(results[0].status, c.Status.FAIL)
        self.assertIn("404", results[0].detail)

    def test_stale_page_fails_freshness(self):
        res = _by_name(c.evaluate(_good_body(age_s=3600), _meta(), NOW))
        self.assertIs(res["freshness"].status, c.Status.FAIL)

    def test_near_stale_warns(self):
        res = _by_name(c.evaluate(_good_body(age_s=300), _meta(), NOW))  # 5min, >WARN <FAIL
        self.assertIs(res["freshness"].status, c.Status.WARN)

    def test_missing_generated_stamp_fails(self):
        body = _good_body().replace("Generated", "Made at").replace("UTC", "")
        res = _by_name(c.evaluate(body, _meta(), NOW))
        self.assertIs(res["freshness"].status, c.Status.FAIL)

    def test_missing_core_section_fails(self):
        body = _good_body().replace("Bots — par famille", "Bots by account")
        res = _by_name(c.evaluate(body, _meta(), NOW))
        self.assertIs(res["core sections"].status, c.Status.FAIL)
        self.assertIn("bot-family view", res["core sections"].detail)

    def test_missing_window_toggle_warns(self):
        body = _good_body().replace("pw-weekly", "pw-xxx")
        res = _by_name(c.evaluate(body, _meta(), NOW))
        self.assertIs(res["window toggle"].status, c.Status.WARN)

    def test_empty_window_data_warns_when_collector_up(self):
        # Markers present but every value is the "—" placeholder → silent feature blank.
        body = (_good_body()
                .replace("<span class='pnl-pos'>+$45.21</span>", "<span class='no-data'>—</span>")
                .replace("<span class='pnl-pos'>+$226.57</span>", "<span class='no-data'>—</span>"))
        res = _by_name(c.evaluate(body, _meta(), NOW))
        self.assertIs(res["window data"].status, c.Status.WARN)
        self.assertIs(res["window toggle"].status, c.Status.PASS)  # markers still there

    def test_empty_window_data_ok_when_collector_down(self):
        # Same emptiness, but the collector-down banner explains it → not the feature's fault.
        body = (_good_body()
                .replace("<span class='pnl-pos'>+$45.21</span>", "<span class='no-data'>—</span>")
                .replace("<span class='pnl-pos'>+$226.57</span>", "<span class='no-data'>—</span>")
                .replace("<div class='summary-bar'>",
                         "<div class='banner bad'>collector down</div><div class='summary-bar'>"))
        res = _by_name(c.evaluate(body, _meta(), NOW))
        self.assertIs(res["window data"].status, c.Status.PASS)

    def test_populated_window_data_passes(self):
        res = _by_name(c.evaluate(_good_body(), _meta(), NOW))
        self.assertIs(res["window data"].status, c.Status.PASS)

    def test_traceback_artifact_fails(self):
        body = _good_body() + "\nTraceback (most recent call last):\n"
        res = _by_name(c.evaluate(body, _meta(), NOW))
        self.assertIs(res["no artifacts"].status, c.Status.FAIL)

    def test_truncated_html_fails(self):
        body = _good_body().replace("</html>", "")
        res = _by_name(c.evaluate(body, _meta(), NOW))
        self.assertIs(res["html complete"].status, c.Status.FAIL)

    def test_small_body_fails_size(self):
        res = _by_name(c.evaluate(_good_body(), _meta(size=500), NOW))
        self.assertIs(res["body size"].status, c.Status.FAIL)

    def test_banner_bad_fails_collection(self):
        body = _good_body().replace("<div class='summary-bar'>",
                                    "<div class='banner bad'>collector down</div><div class='summary-bar'>")
        res = _by_name(c.evaluate(body, _meta(), NOW))
        self.assertIs(res["collection"].status, c.Status.FAIL)

    def test_banner_warn_warns_collection(self):
        body = _good_body().replace("<div class='summary-bar'>",
                                    "<div class='banner warn'>1 acct down</div><div class='summary-bar'>")
        res = _by_name(c.evaluate(body, _meta(), NOW))
        self.assertIs(res["collection"].status, c.Status.WARN)

    def test_content_type_non_html_warns(self):
        res = _by_name(c.evaluate(_good_body(), _meta(content_type="text/plain"), NOW))
        self.assertIs(res["content-type"].status, c.Status.WARN)


class TestParseGenerated(unittest.TestCase):

    def test_valid(self):
        ts = c._parse_generated("… Generated 2026-07-02 15:55 UTC …")
        self.assertEqual(datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                         "2026-07-02 15:55")

    def test_absent(self):
        self.assertIsNone(c._parse_generated("no stamp here"))

    def test_malformed(self):
        self.assertIsNone(c._parse_generated("Generated 2026-13-99 99:99 UTC"))


if __name__ == "__main__":
    unittest.main()
