#!/usr/bin/env python3
"""check_statuspage.py — verify the *published* status page over HTTP (curl).

generate_status.py writes the page and a systemd timer republishes it ~every minute; this
checker fetches the live URL with curl and asserts the artifact users actually see is
healthy. It is deliberately black-box (HTTP, not the DB): it catches the failure modes the
generator's own tests cannot — a dead timer (stale page), a web server not serving the
file, a truncated/half-written page, unrendered template/traceback leftovers, a regressed
window toggle, or a page stuck on a degraded-collection banner.

The verdicts come from a PURE function (`evaluate`) unit-tested against saved HTML; only
`main()` touches the network (curl) and prints.

Usage:
  python3 tradinebotte-status/check_statuspage.py                # URL from ~/.tradinebotte-test.conf
  python3 tradinebotte-status/check_statuspage.py --url https://host/tradinebottestatus.html
  python3 tradinebotte-status/check_statuspage.py --verbose      # show PASS lines too
  python3 tradinebotte-status/check_statuspage.py --warn-only    # always exit 0
  python3 tradinebotte-status/check_statuspage.py --json         # machine-readable

Config: the URL defaults to https://$TEST_SERVER/tradinebottestatus.html, reading
TEST_SERVER from ~/.tradinebotte-test.conf (the same file generate_status.py uses) — the
host is never hardcoded. Override with --url or $TRADINEBOTTE_STATUS_URL.

Exit codes: 0 = all PASS/WARN, 1 = at least one FAIL, 2 = fetch/config error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

# A page older than this is stale — the generator/timer has stopped (FAIL). The timer runs
# ~1/min and collection takes ~40s, so a healthy page is well under a minute old; WARN a
# little before FAIL so a single skipped tick is visible without alarming.
STALE_S      = int(os.environ.get("STATUSPAGE_STALE_S", 600))   # 10 min → FAIL
NEAR_STALE_S = int(os.environ.get("STATUSPAGE_WARN_S",  240))   # 4 min  → WARN
# Below this the response is an error page / truncated write, not our status page.
MIN_SIZE     = int(os.environ.get("STATUSPAGE_MIN_BYTES", 3000))
FETCH_TIMEOUT = int(os.environ.get("STATUSPAGE_FETCH_TIMEOUT", 20))


class Status(Enum):
    PASS = "✓"
    WARN = "⚠"
    FAIL = "✗"


@dataclass
class Result:
    name: str
    status: Status
    detail: str


def _r(name: str, status: Status, detail: str) -> Result:
    return Result(name, status, detail)


# ── Freshness ────────────────────────────────────────────────────────────────

# The footer/H1 carry: Generated 2026-07-02 15:55 UTC
_GEN_RE = re.compile(r"Generated\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s+UTC")


def _parse_generated(body: str) -> int | None:
    """Epoch (UTC) of the page's 'Generated … UTC' stamp, or None if absent/unparsable."""
    m = _GEN_RE.search(body)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp())


def _fmt_age(secs: int) -> str:
    if secs < 0:
        return f"{secs}s (clock skew?)"
    if secs < 120:
        return f"{secs}s"
    if secs < 7200:
        return f"{secs // 60}min"
    return f"{secs // 3600}h"


# ── Pure evaluation ──────────────────────────────────────────────────────────

# Core structural markers that MUST be present in a well-rendered page.
_CORE_MARKERS = {
    "title":            "<title>tradinebotte",
    "summary bar":      "class='summary-bar'",
    "bot-family view":  "Bots — par famille",
    "accounts section": 'class="accounts"',
}
# Markers of the windowed-PnL feature (daily/weekly/monthly/alltime toggle).
_WINDOW_MARKERS = {
    "toggle bar":  "win-toggle",
    "weekly spans": "pw-weekly",
    "monthly spans": "pw-monthly",
    "toggle script": "setWin(",
    "Jour button":  ">Jour<",
    "Semaine button": ">Semaine<",
    "Mois button":  ">Mois<",
    "reset button": ">Depuis reset<",
}
# Strings that must NEVER appear — unrendered templates / crash artifacts / repr leaks.
_ARTIFACTS = [
    "Traceback (most recent call last)",
    "__REPO_DIR__",
    "__VENV__",
    "KeyError",
    "object at 0x",
]

# Anchored on the status dot so it matches the H1 headline, not the <title>.
_HEADLINE_RE = re.compile(r'class="dot \w+"></span>\s*tradinebotte\s+—\s+([^<\n]+)')

# A windowed-PnL span carrying an actual '$' value (vs the "—" no-data placeholder). The
# lazy (?!</span>) stops at the span's own close, so this matches only a $ inside the span.
_WIN_VALUE_RE = re.compile(r"pw-(?:weekly|monthly)'>(?:(?!</span>).)*?\$")


def evaluate(body: str, meta: dict, now: int,
             *, stale_s: int = STALE_S, near_stale_s: int = NEAR_STALE_S,
             min_size: int = MIN_SIZE) -> list[Result]:
    """Grade a fetched page. PURE — no network. `meta` carries the HTTP fetch metadata.

    meta keys: http_code (int), content_type (str), size (int), elapsed (float),
               error (str|None — set when curl itself failed).
    """
    results: list[Result] = []

    # 1. Reachability. A transport error or non-200 makes every downstream check moot.
    err = meta.get("error")
    if err:
        results.append(_r("reachable", Status.FAIL, f"fetch failed: {err}"))
        return results
    code = meta.get("http_code", 0)
    if code != 200:
        results.append(_r("reachable", Status.FAIL, f"HTTP {code}"))
        return results
    results.append(_r("reachable", Status.PASS,
                      f"HTTP 200 · {meta.get('elapsed', 0):.2f}s"))

    # 2. Content-type.
    ctype = (meta.get("content_type") or "").lower()
    results.append(_r("content-type", Status.PASS if "text/html" in ctype else Status.WARN,
                      ctype or "unknown"))

    # 3. Size / completeness — small bodies are error pages; a missing </html> is a
    #    truncated/half-written publish (the generator writes then a reader raced it).
    size = meta.get("size") or len(body.encode("utf-8"))
    if size < min_size:
        results.append(_r("body size", Status.FAIL, f"{size} B < {min_size} B (error page?)"))
    else:
        results.append(_r("body size", Status.PASS, f"{size} B"))
    complete = "</html>" in body and "<body" in body
    results.append(_r("html complete", Status.PASS if complete else Status.FAIL,
                      "closed <html>" if complete else "missing </html> — truncated?"))

    # 4. Freshness — the single most important quality signal: is the generator still
    #    republishing? A stale page looks fine but is lying about the fleet.
    gen = _parse_generated(body)
    if gen is None:
        results.append(_r("freshness", Status.FAIL, "no 'Generated … UTC' stamp found"))
    else:
        age = now - gen
        if age > stale_s:
            st = Status.FAIL
        elif age > near_stale_s or age < -near_stale_s:
            st = Status.WARN
        else:
            st = Status.PASS
        results.append(_r("freshness", st, f"generated {_fmt_age(age)} ago"))

    # 5. Core structure.
    missing = [name for name, marker in _CORE_MARKERS.items() if marker not in body]
    results.append(_r("core sections", Status.PASS if not missing else Status.FAIL,
                      "all present" if not missing else f"missing: {', '.join(missing)}"))

    # 6. Windowed-PnL feature (daily/weekly/monthly/alltime).
    win_missing = [name for name, marker in _WINDOW_MARKERS.items() if marker not in body]
    results.append(_r("window toggle", Status.PASS if not win_missing else Status.WARN,
                      "daily/weekly/monthly/alltime present" if not win_missing
                      else f"missing: {', '.join(win_missing)}"))

    # 6b. Windowed PnL has actual DATA — the markers above can all be present while every
    #     value renders "—" because the remote _compute_pnl_windows returned {} (the
    #     collector could not read the shared state DB). That silently blanks the flagship
    #     feature. A legitimately blind page (collector down) is empty for the same reason
    #     but is already a FAIL below, so only warn when the collector is up.
    has_value = bool(_WIN_VALUE_RE.search(body))
    collector_down = "class='banner bad'" in body
    if has_value:
        results.append(_r("window data", Status.PASS, "windowed PnL populated"))
    elif collector_down:
        results.append(_r("window data", Status.PASS, "empty (collector down — expected)"))
    else:
        results.append(_r("window data", Status.WARN,
                          "windowed PnL spans all empty — remote computation returned nothing?"))

    # 7. No unrendered template / crash artifacts.
    found = [a for a in _ARTIFACTS if a in body]
    results.append(_r("no artifacts", Status.PASS if not found else Status.FAIL,
                      "clean" if not found else f"found: {', '.join(found)}"))

    # 8. Degraded-collection banners. The page correctly surfaces these; for the PUBLISHED
    #    artifact they mean the content is blind (bad = collector down) or partial (warn).
    if "class='banner bad'" in body:
        results.append(_r("collection", Status.FAIL,
                          "degraded banner (bad) — collector unreachable, fleet blind"))
    elif "class='banner warn'" in body:
        results.append(_r("collection", Status.WARN,
                          "degraded banner (warn) — some accounts unreachable this cycle"))
    else:
        results.append(_r("collection", Status.PASS, "no degradation banner"))

    # 9. Fleet headline — informational (a page correctly reporting fleet issues is still a
    #    healthy PAGE, so this never fails the check; it just surfaces the current line).
    m = _HEADLINE_RE.search(body)
    if m:
        results.append(_r("fleet headline", Status.PASS, m.group(1).strip()))

    return results


# ── Fetch (curl) ─────────────────────────────────────────────────────────────

def fetch(url: str, timeout: int = FETCH_TIMEOUT) -> tuple[str, dict]:
    """GET `url` with curl. Returns (body, meta). meta['error'] is set on transport failure.

    curl writes the body to a temp file (-o) and the metadata to stdout (-w) so the two can
    never be confused by newlines in the HTML.
    """
    fd, tmp = tempfile.mkstemp(prefix="statuspage_", suffix=".html")
    os.close(fd)
    try:
        proc = subprocess.run(
            ["curl", "-sS", "--max-time", str(timeout),
             "-o", tmp,
             "-w", "%{http_code}\t%{content_type}\t%{size_download}\t%{time_total}",
             url],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            msg = (proc.stderr or "").strip().splitlines()
            return "", {"error": msg[-1] if msg else f"curl rc={proc.returncode}",
                        "http_code": 0, "content_type": "", "size": 0, "elapsed": 0.0}
        parts = proc.stdout.strip().split("\t")
        code = int(parts[0]) if parts and parts[0].isdigit() else 0
        ctype = parts[1].split(";")[0].strip() if len(parts) > 1 else ""
        size = int(float(parts[2])) if len(parts) > 2 and parts[2] else 0
        elapsed = float(parts[3]) if len(parts) > 3 and parts[3] else 0.0
        with open(tmp, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
        return body, {"error": None, "http_code": code, "content_type": ctype,
                      "size": size, "elapsed": elapsed}
    except FileNotFoundError:
        return "", {"error": "curl not found", "http_code": 0,
                    "content_type": "", "size": 0, "elapsed": 0.0}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ── URL from config (never hardcode the host) ────────────────────────────────

def _default_url(conf_path: str) -> str | None:
    """https://$TEST_SERVER/tradinebottestatus.html, sourced from the shared conf."""
    env_url = os.environ.get("TRADINEBOTTE_STATUS_URL")
    if env_url:
        return env_url
    if not os.path.exists(conf_path):
        return None
    r = subprocess.run(["bash", "-c", f'source "{conf_path}" 2>/dev/null; echo "$TEST_SERVER"'],
                       capture_output=True, text=True, check=False)
    server = r.stdout.strip()
    return f"https://{server}/tradinebottestatus.html" if server else None


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Verify the published tradinebotte status page over HTTP.")
    ap.add_argument("--url", default=None,
                    help="Page URL (default: https://$TEST_SERVER/tradinebottestatus.html from conf)")
    ap.add_argument("--conf", default=os.path.expanduser("~/.tradinebotte-test.conf"))
    ap.add_argument("--timeout", type=int, default=FETCH_TIMEOUT)
    ap.add_argument("--verbose", action="store_true", help="show PASS lines too")
    ap.add_argument("--warn-only", action="store_true", help="always exit 0")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    url = args.url or _default_url(args.conf)
    if not url:
        print("No URL: pass --url or set TRADINEBOTTE_STATUS_URL / TEST_SERVER in the conf",
              file=sys.stderr)
        return 2

    body, meta = fetch(url, args.timeout)
    results = evaluate(body, meta, int(time.time()))

    n_fail = sum(1 for r in results if r.status is Status.FAIL)
    n_warn = sum(1 for r in results if r.status is Status.WARN)

    if args.json:
        print(json.dumps({
            "url": url,
            "ok": n_fail == 0,
            "fail": n_fail, "warn": n_warn,
            "checks": [{"name": r.name, "status": r.status.name, "detail": r.detail}
                       for r in results],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"status page check — {url}")
        for r in results:
            if r.status is Status.PASS and not args.verbose:
                continue
            print(f"  {r.status.value} {r.name:16} {r.detail}")
        summary = (f"{n_fail} FAIL · {n_warn} WARN · "
                   f"{sum(1 for r in results if r.status is Status.PASS)} PASS")
        verdict = "FAIL" if n_fail else ("WARN" if n_warn else "OK")
        print(f"  → {verdict}  ({summary})")

    if args.warn_only:
        return 0
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
