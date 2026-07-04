#!/usr/bin/env python3
"""generate_status.py — Generate a full HTML status page for all tradinebotte accounts.

Collects data via one sequential SSH per account (6 total):
  - systemd service states + version stamp (all accounts)
  - live.db trade stats (accounts 1, 2, 3, 4)
  - live_accum.db accumulation stats (accounts 3, 4)
  - shared state DB: heartbeats + inventory + deploys (account 1 — the status collector)

No dependency on heartbeat_query.py — reads the shared state DB directly.

Usage:
  python3 tradinebotte-status/generate_status.py > status.html
  python3 tradinebotte-status/generate_status.py --out /var/www/html/status.html
  python3 tradinebotte-status/generate_status.py --conf ~/.tradinebotte-test.conf

Config: ~/.tradinebotte-test.conf  (same file as bot_status.sh)
  Required keys: TEST_SERVER, TEST_PORT, TEST_USERS (array), TEST_PASSWORDS (array)
  Optional key:  TEST_REMOTE_INSTALL_DIR (default ~/tradinebotte)
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html import escape


def _fetch_btc_24h(symbol: str = "BTCUSDT") -> dict:
    """Fetch 24h ticker stats from Binance public REST API. Returns {} on any error."""
    url = f"https://api.binance.com/api/v3/ticker/24hr?symbol={symbol}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tradinebotte-status/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}

# ─── Remote data-collection snippet (runs via SSH on each account) ────────────

_REMOTE_COLLECT = r"""
import sqlite3, json, os, subprocess, glob

data = {"version": "?", "services": [], "live": None, "grids": [],
        "accum": None, "heartbeats": None, "pnl_windows": {}}

try:
    data["version"] = open(os.path.expanduser("~/tradinebotte/version.stamp")).read().strip()
except Exception:
    pass

try:
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    r = subprocess.run(
        ["systemctl", "--user", "list-units", "tradinebotte-*", "--no-legend", "--plain"],
        capture_output=True, text=True, env=env, timeout=5,
    )
    for line in r.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) >= 3:
            unit = parts[0].replace("tradinebotte-", "").replace(".service", "")
            if unit.startswith("account-"):
                unit = "account_bot"
            data["services"].append({
                "unit":   unit,
                "active": parts[2] == "active",
                "sub":    parts[3] if len(parts) > 3 else "",
            })
except Exception:
    pass


def _qdb(path, queries):
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return None
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    result = {}
    for key, sql in queries.items():
        try:
            result[key] = [dict(row) for row in db.execute(sql).fetchall()]
        except Exception:
            result[key] = []
    db.close()
    return result


# live.db: Polymarket bot. PnL/capital now come from the heartbeat payload (the single
# source of truth shared with the pills + fleet headline); live.db is queried only for
# the win-rate counts and the recent/open trade tables that payloads don't carry.
_LIVE_QUERIES = {
    "totals": (
        "SELECT count(*) t,"
        " sum(CASE WHEN outcome='WIN'  THEN 1 ELSE 0 END) w,"
        " sum(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) l,"
        " sum(CASE WHEN resolved=0    THEN 1 ELSE 0 END) o"
        " FROM trades"
    ),
    "recent": (
        "SELECT id, entry_ts_ms/1000 entry_ts, direction,"
        " outcome result, entry_price, pnl_net pnl, capital_after capital,"
        " substr(question,1,42) q"
        " FROM trades WHERE resolved=1 ORDER BY id DESC LIMIT 8"
    ),
    "open": (
        "SELECT id, entry_ts_ms/1000 entry_ts, direction, entry_price,"
        " substr(question,1,42) q FROM trades WHERE resolved=0"
    ),
}
data["live"] = _qdb("~/tradinebotte/live.db", _LIVE_QUERIES)

# Grid bots keep aggregate state (bounds / cycles / level fills / halted) in grid_state
# + grid_levels rather than a per-trade log, so the Polymarket trade queries above find
# nothing for them. Collect that aggregate from every candidate db: the standard path
# (an account running only a grid) and any alternate data dir
# (TRADINEBOTTE_DIR=~/tradinebotte-grid). The heartbeat carries only PnL — bounds, level
# fills, and the halted flag are otherwise absent from the page.
_GRID_QUERIES = {
    "state": (
        "SELECT symbol, grid_lower, grid_upper, grid_step, order_size_usdt,"
        " total_cycles, total_profit_usd, halted FROM grid_state LIMIT 1"
    ),
    "levels": "SELECT status, count(*) n FROM grid_levels GROUP BY status",
}
data["grids"] = []
for _p in [os.path.expanduser("~/tradinebotte/live.db")] + sorted(
        glob.glob(os.path.expanduser("~/tradinebotte-*/live.db"))):
    _g = _qdb(_p, _GRID_QUERIES)
    if _g and _g.get("state"):
        data["grids"].append({"dir": os.path.basename(os.path.dirname(_p)), **_g})

# live_accum.db: accumulation bot — table accum_trades
data["accum"] = _qdb("~/tradinebotte/live_accum.db", {
    "totals": "SELECT count(*) t FROM accum_trades",
    "portfolio": (
        "SELECT holdings_after holdings_btc, free_usdt_after free_usdt,"
        " avg_entry_after avg_entry FROM accum_trades ORDER BY id DESC LIMIT 1"
    ),
})

# Heartbeats now live in the shared state DB (read on the collector account, account-1).
data["heartbeats"] = _qdb("/data1/tradinebotte-shared/database/tradinebotte.db", {
    "rows": (
        "SELECT account, bot_name, max(ts) as last_ts,"
        " status, bounds_ok, version, payload"
        " FROM heartbeats GROUP BY account, bot_name ORDER BY account, bot_name"
    ),
})

# Desired-state inventory + latest deploy per bot (same shared DB, account-1 only).
data["inventory"] = _qdb("/data1/tradinebotte-shared/database/tradinebotte.db", {
    "rows": (
        "SELECT account, bot_name, kind, bot_type, is_live"
        " FROM inventory WHERE enabled=1 ORDER BY account, bot_name"
    ),
})
data["deploys"] = _qdb("/data1/tradinebotte-shared/database/tradinebotte.db", {
    "rows": (
        "SELECT account, bot_name, max(ts) as last_ts, git_hash, result"
        " FROM deploys GROUP BY account, bot_name"
    ),
})


# ── Windowed PnL (daily / weekly / monthly / alltime) ────────────────────────
# From heartbeat *history* in the shared state DB — the only source that (a) covers
# every family uniformly (grid_bot keeps no per-trade log) and (b) survives a per-bot
# live.db reset (the history lives in the shared DB, not the wiped live.db). pnl_total
# is cumulative "since reset"; a window value is pnl_now - pnl_at(window_start). A ZMQ
# reset zeroes pnl_total, so when a reset falls inside the window we clamp the baseline
# to just after it (→ post-reset PnL, consistent with alltime = "since reset") and flag
# it. daily comes straight from the bot-authoritative daily_pnl (resets UTC midnight).
def _compute_pnl_windows(shared_path):
    import time as _t
    shared_path = os.path.expanduser(shared_path)
    if not os.path.exists(shared_path):
        return {}
    now = int(_t.time())
    wdb = sqlite3.connect(shared_path)
    wdb.row_factory = sqlite3.Row
    latest = {}
    for r in wdb.execute("SELECT account, bot_name, payload, max(ts) FROM heartbeats"
                         " GROUP BY account, bot_name"):
        try:
            p = json.loads(r["payload"]) if r["payload"] else {}
        except Exception:
            p = {}
        latest[(r["account"], r["bot_name"])] = p
    out = {}
    windows = {"weekly": 7 * 86400, "monthly": 30 * 86400}
    horizon = now - 30 * 86400
    for (acct, bot), p in latest.items():
        pt = p.get("pnl_total")
        rec = {"daily": p.get("daily_pnl"), "alltime": pt,
               "weekly": None, "monthly": None,
               "weekly_reset": False, "monthly_reset": False}
        if isinstance(pt, (int, float)):
            series = [(row["ts"], row["p"]) for row in wdb.execute(
                "SELECT ts, json_extract(payload,'$.pnl_total') p FROM heartbeats"
                " WHERE account=? AND bot_name=? AND ts>=?"
                " AND json_extract(payload,'$.pnl_total') IS NOT NULL ORDER BY ts",
                (acct, bot, horizon))]
            for wname, secs in windows.items():
                wstart = now - secs
                first_val = reset_val = None
                reset = False
                prev = None
                for ts, val in series:
                    # A reset wipes pnl_total to exactly 0 — that lands-on-zero signature
                    # distinguishes it from an ordinary drawdown (arbitrary value).
                    if prev is not None and abs(val) < 0.01 and abs(prev) > 1.0 and ts >= wstart:
                        reset_val = val
                        reset = True
                    if ts >= wstart and first_val is None:
                        first_val = val
                    prev = val
                base = reset_val if reset else first_val
                if base is not None:
                    rec[wname] = pt - base
                    rec[wname + "_reset"] = reset
        out[f"{acct}|{bot}"] = rec
    wdb.close()
    return out


data["pnl_windows"] = _compute_pnl_windows(
    "/data1/tradinebotte-shared/database/tradinebotte.db")

print(json.dumps(data))
"""

# ─── SSH helper ──────────────────────────────────────────────────────────────

def _ssh(user: str, password: str, server: str, port: int, cmd: str) -> tuple[str, int]:
    env = {**os.environ, "SSHPASS": password}
    result = subprocess.run(
        [
            "/usr/bin/sshpass", "-e",
            "ssh",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=15",
            "-o", "BatchMode=no",
            "-o", "PreferredAuthentications=password",
            "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3",
            "-p", str(port),
            f"{user}@{server}",
            cmd,
        ],
        capture_output=True, text=True, env=env, check=False,
    )
    return result.stdout, result.returncode


def _collect_account(user: str, password: str, server: str, port: int) -> dict:
    cmd = f"python3 - <<'__PYEOF__'\n{_REMOTE_COLLECT}\n__PYEOF__"
    stdout, _ = _ssh(user, password, server, port, cmd)
    if not stdout.strip():
        return {"version": "?", "services": [], "live": None, "accum": None,
                "heartbeats": None, "pnl_windows": {}, "error": "unreachable"}
    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError:
        return {"version": "?", "services": [], "live": None, "accum": None,
                "heartbeats": None, "pnl_windows": {}, "error": "parse_error"}


# ─── Heartbeat classification ────────────────────────────────────────────────

# Heartbeats are sent every 120 s (tradinetools.heartbeat_loop default), so a bot
# missing ~2 heartbeats is STALE and ~5 is DEAD — a real death shows in minutes.
_STALE_AFTER = int(os.environ.get("HEARTBEAT_STALE_S", 240))
_DEAD_AFTER  = int(os.environ.get("HEARTBEAT_DEAD_S",  600))

# Data-recording freshness. A bot can heartbeat fine (ALIVE) while its time-series
# table stops growing — the 2026-06-16 CEX snapshots bug hid for 10 days exactly this
# way. Bots report last_write_ts (epoch of last PERSISTED data row); if it falls behind
# this many seconds the page flags ⚠data, independently of the heartbeat flag. Generous
# default (snapshots are written ~1/s, so 10 min behind is unambiguous, not flapping).
_DATA_STALE_AFTER = int(os.environ.get("DATA_STALE_S", 600))


def _data_flag(payload: dict, now: int) -> str:
    """"STALE" if the bot's data table stopped growing, else "".

    last_write_ts ABSENT → infra service / snapshots disabled → never alarms.
    last_write_ts == 0.0 (present) → bot claims to record but has written NOTHING →
    must alarm: that's the post-restart reproduction of the 2026-06-16 bug (the bot
    boots in a mode that never writes). So test `is None`, not falsiness."""
    wts = payload.get("last_write_ts")
    if wts is None:
        return ""
    return "STALE" if now - int(wts) > _DATA_STALE_AFTER else ""


def _classify_heartbeats(raw_rows: list, user_to_label: dict) -> list:
    now = int(time.time())
    result = []
    for row in raw_rows:
        age_s = now - int(row["last_ts"])
        if age_s <= _STALE_AFTER:
            flag = "ALIVE"
        elif age_s <= _DEAD_AFTER:
            flag = "STALE"
        else:
            flag = "DEAD"
        bounds_val = row.get("bounds_ok")
        bounds = "-" if bounds_val is None else ("ok" if bounds_val else "FAIL")
        acct = row.get("account", "")
        raw_payload = row.get("payload")
        payload = {}
        if raw_payload:
            try:
                payload = json.loads(raw_payload) if isinstance(raw_payload, str) else {}
            except Exception:
                pass
        result.append({
            "account":    acct,
            "bot_name":   row.get("bot_name", ""),
            "last_ts":    int(row["last_ts"]),
            "age_s":      age_s,
            "flag":       flag,
            "bot_status": str(row.get("status") or "-"),
            "bounds_ok":  bounds,
            "version":    (row.get("version") or "unknown")[:10],
            "_label":     user_to_label.get(acct, acct),
            "payload":    payload,
        })
    return result


# ─── Configuration loader ────────────────────────────────────────────────────

def _load_conf(path: str) -> dict:
    def _bash_eval(expr: str) -> str:
        r = subprocess.run(
            ["bash", "-c", f"source {path} 2>/dev/null; {expr}"],
            capture_output=True, text=True, check=False,
        )
        return r.stdout.strip()

    server      = _bash_eval("echo \"$TEST_SERVER\"")
    port        = int(_bash_eval("echo \"${TEST_PORT:-22}\"") or "22")
    install_dir = _bash_eval("echo \"${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}\"")
    users_str   = _bash_eval("echo \"${TEST_USERS[*]}\"")
    pwds_str    = _bash_eval("echo \"${TEST_PASSWORDS[*]}\"")

    return {
        "server":      server,
        "port":        port,
        "install_dir": install_dir or "~/tradinebotte",
        "users":       users_str.split() if users_str else [],
        "passwords":   pwds_str.split()  if pwds_str  else [],
    }


# ─── HTML rendering ──────────────────────────────────────────────────────────

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'SF Mono',Menlo,monospace;background:#0d1117;color:#c9d1d9;
     padding:20px;font-size:13px;line-height:1.5}
a{color:#58a6ff;text-decoration:none}
h1{color:#58a6ff;font-size:1.3em;padding-bottom:10px;border-bottom:1px solid #30363d;
   display:flex;align-items:center;gap:10px;flex-wrap:wrap}
h2{color:#8b949e;font-size:.85em;text-transform:uppercase;letter-spacing:1.5px;
   margin:28px 0 10px;padding-bottom:5px;border-bottom:1px solid #21262d}

/* ── Status dot ───────────────────────────────────────── */
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;flex-shrink:0}
.dot.ok{background:#3fb950;box-shadow:0 0 6px #3fb95088;animation:pulse 2s infinite}
.dot.warn{background:#d29922}
.dot.bad{background:#f85149}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

/* ── Tooltip system ───────────────────────────────────── */
.tt{position:relative;cursor:default}
.tt .tip{
  visibility:hidden;opacity:0;pointer-events:none;
  position:absolute;left:0;top:calc(100% + 5px);z-index:300;
  background:#1c2029;border:1px solid #30363d;border-radius:7px;
  padding:13px 17px;min-width:280px;max-width:500px;
  font-size:1.1em;color:#c9d1d9;line-height:1.85;white-space:normal;
  box-shadow:0 8px 28px rgba(0,0,0,.65);
  transition:opacity .13s ease,visibility .13s ease}
.tt:hover .tip{visibility:visible;opacity:1}
.tt .tip.tip-up{top:auto;bottom:calc(100% + 5px)}
.tip-label{color:#8b949e;font-size:1em;margin-bottom:5px;display:block}
.tip-row{color:#c9d1d9;margin:2px 0}
.tip-dim{color:#8b949e;margin-top:6px;font-size:.95em}
/* Small screens: enlarge hover tooltips so the detail is readable on phones */
@media (max-width:640px){
  .tt .tip{font-size:1.3em;line-height:1.8;min-width:200px;max-width:92vw;padding:15px 18px}
  .tip-label{font-size:1.05em}
  .tip-dim{font-size:1em}
}

/* ── Degraded-collection banners ──────────────────────── */
.banner{border-radius:7px;padding:11px 15px;margin:12px 0;font-size:.95em;
        border:1px solid;line-height:1.45}
.banner.bad{background:#2d1418;border-color:#f8514955;color:#ff7b72}
.banner.warn{background:#2b2412;border-color:#d2992255;color:#e3b341}
.banner code{background:#00000033;padding:1px 5px;border-radius:4px;font-size:.92em}

/* ── Summary bar ──────────────────────────────────────── */
.summary-bar{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}
.sb-item{background:#161b22;border:1px solid #30363d;border-radius:6px;
          padding:10px 16px;display:flex;flex-direction:column}
.sb-item .lbl{font-size:.65em;color:#8b949e;text-transform:uppercase;letter-spacing:.9px}
.sb-item .val{font-size:1.3em;font-weight:700;margin-top:3px}

/* ── Heartbeat pills ──────────────────────────────────── */
.hb-pills{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.bot-pill{display:inline-flex;align-items:center;gap:5px;
          background:#161b22;border:1px solid #30363d;border-radius:20px;
          padding:4px 11px 4px 7px;
          transition:border-color .15s,background .15s}
.bot-pill:hover{border-color:#58a6ff55;background:#1c2029}
.pill-name{font-size:.83em;color:#c9d1d9;font-weight:600}
.pill-acct{font-size:.72em;color:#484f58}
.pill-metric{font-size:.83em;margin-left:2px}

/* ── Account grid ─────────────────────────────────────── */
.accounts{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
           gap:14px;margin-top:8px}
.account{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}

/* account header — service dots shown inline, full list on hover */
.account-header{display:flex;align-items:center;gap:8px;margin-bottom:10px}
.account-header .name{font-weight:700;color:#c9d1d9;flex:1;min-width:0;
                       overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.svc-dots{display:flex;gap:4px;align-items:center;flex-shrink:0}
.svc-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.svc-dot.ok{background:#3fb950}.svc-dot.bad{background:#f85149}

/* ── Big metrics ──────────────────────────────────────── */
.big-metrics{display:flex;gap:10px;margin:8px 0 6px}
.metric-big{flex:1;background:#0d1117;border-radius:6px;padding:10px 13px;min-width:0}
.metric-big .lbl{font-size:.63em;color:#8b949e;text-transform:uppercase;letter-spacing:.9px}
.metric-big .val{font-size:1.8em;font-weight:700;margin-top:2px;
                  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* Dim metric values whose source heartbeat is stale/dead (last-known, not current) */
.metric-big .val.stale-val{opacity:.45}
.metric-sub{font-size:.72em;color:#8b949e;margin-top:2px}

/* ── Compact bot rows in account cards ────────────────── */
.bot-rows{margin-top:8px;border-top:1px solid #21262d;padding-top:6px}
.bot-row{display:flex;align-items:center;gap:5px;padding:3px 2px;
          border-bottom:1px solid #1c2029;font-size:.8em}
.bot-row:last-child{border-bottom:none}
.bot-row:hover{background:#1c2029;border-radius:4px}
.bot-name{color:#c9d1d9;min-width:108px;flex-shrink:0}
.bot-metric{color:#8b949e;flex:1;text-align:right;font-size:.88em;
             white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ── Trade tables ─────────────────────────────────────── */
.tr-table{width:100%;border-collapse:collapse;font-size:.77em;margin-top:6px}
.tr-table th{background:#0d1117;color:#8b949e;padding:4px 8px;text-align:left;
             border-bottom:1px solid #30363d;white-space:nowrap}
.tr-table td{padding:3px 8px;border-bottom:1px solid #161b22}
.tr-table tr:hover td{background:#1c2029}
details summary{cursor:pointer;color:#8b949e;font-size:.78em;
                 padding:4px 0;user-select:none}
details summary:hover{color:#c9d1d9}

/* ── Badges ───────────────────────────────────────────── */
.badge{display:inline-block;font-size:.67em;padding:1px 6px;border-radius:8px;
       font-weight:600;vertical-align:middle;white-space:nowrap}
.badge.alive{background:#0d2212;color:#3fb950;border:1px solid #2a4a30}
.badge.stale{background:#2d1f00;color:#d29922;border:1px solid #4a3800}
.badge.dead{background:#1e0a0a;color:#f85149;border:1px solid #5a2020}
.badge.open-b{background:#0d1a2a;color:#58a6ff;border:1px solid #1f3a5f}
.badge.sim{background:#1a1f3a;color:#79b8ff;border:1px solid #264f78}
.badge.live-mode{background:#0d2212;color:#3fb950;border:1px solid #2a4a30}

/* ── Colours ──────────────────────────────────────────── */
.alive{color:#3fb950}.stale{color:#d29922}.dead{color:#f85149}
.win{color:#3fb950}.loss{color:#f85149}.open-t{color:#d29922}
.pnl-pos{color:#3fb950}.pnl-neg{color:#f85149}

/* ── BTC price in header ──────────────────────────────── */
.btc-price{font-size:.78em;color:#8b949e;display:flex;
            align-items:center;gap:6px;white-space:nowrap}
.btc-price .price{color:#e6a817;font-weight:700;font-size:1.05em}
.btc-price .chg.up{color:#3fb950}.btc-price .chg.dn{color:#f85149}

/* ── Open trade badges ────────────────────────────────── */
.open-trades{margin:4px 0;font-size:.8em}

/* ── Header right-hand group (BTC · timestamp · language switch, top-right) ── */
.h1-right{margin-left:auto;display:flex;align-items:center;gap:10px;flex-wrap:wrap}

/* ── Language switch (one .langbox per language, toggled by a body class) ── */
.langbox{display:none}
.lang-sel{display:inline-flex;gap:4px;align-self:center}
.lbtn{background:#161b22;border:1px solid #30363d;color:#8b949e;border-radius:5px;
      padding:3px 9px;font:inherit;font-size:.6em;cursor:pointer;transition:all .12s}
.lbtn:hover{border-color:#58a6ff55;color:#c9d1d9}

/* ── Window toggle (daily / weekly / monthly / alltime) ─ */
.win-toggle{display:flex;align-items:center;gap:6px;margin:14px 0 4px;flex-wrap:wrap}
.win-toggle .wt-lbl{color:#8b949e;font-size:.78em;text-transform:uppercase;letter-spacing:1px}
.wbtn{background:#161b22;border:1px solid #30363d;color:#8b949e;border-radius:6px;
      padding:5px 13px;font:inherit;font-size:.82em;cursor:pointer;transition:all .12s}
.wbtn:hover{border-color:#58a6ff55;color:#c9d1d9}
body.win-daily .wbtn-daily,body.win-weekly .wbtn-weekly,
body.win-monthly .wbtn-monthly,body.win-alltime .wbtn-alltime{
  background:#1f6feb;border-color:#1f6feb;color:#fff;font-weight:600}
/* Windowed PnL values: every value is rendered as 4 spans; only the active shows. */
.pw{display:none}
body.win-daily .pw-daily,body.win-weekly .pw-weekly,
body.win-monthly .pw-monthly,body.win-alltime .pw-alltime{display:inline}
.rst{color:#d29922;cursor:help;font-size:.9em}

/* ── Bot family sections (primary "by bot" view) ──────── */
.families{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;
          margin-top:8px;align-items:start}
@media (max-width:700px){.families{grid-template-columns:1fr}}
.fam{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:12px 14px}
.fam-head{display:flex;align-items:center;gap:10px;margin-bottom:6px;
          border-bottom:1px solid #21262d;padding-bottom:6px;flex-wrap:wrap}
.fam-name{font-weight:700;color:#c9d1d9;font-size:.95em}
.fam-count{font-size:.78em;font-weight:600}
.fam-pnl{margin-left:auto;font-size:1.05em;font-weight:700}
.fam-row{display:flex;align-items:center;gap:7px;padding:4px 2px;
         border-bottom:1px solid #1c2029;font-size:.83em}
.fam-row:last-child{border-bottom:none}
.fam-row:hover{background:#1c2029;border-radius:4px}
.fam-acct{color:#8b949e;min-width:118px;flex-shrink:0}
.fam-val{font-weight:700;min-width:82px}
.fam-val.stale-val{opacity:.45}
.fam-cap{color:#8b949e;font-size:.9em;margin-left:8px}
.fam-detail{color:#8b949e;font-size:.92em;flex:1;min-width:0;overflow:hidden;
            text-overflow:ellipsis;white-space:nowrap}

/* ── Footer ───────────────────────────────────────────── */
.footer{margin-top:28px;font-size:.72em;color:#484f58;border-top:1px solid #21262d;
         padding-top:10px}
.no-data{color:#484f58;font-style:italic;font-size:.82em;padding:4px 0}
"""

# ─── i18n ────────────────────────────────────────────────────────────────────
# External per-language dictionaries in i18n/<lang>.json. Add a file to add a language;
# the selector top-right lists whatever is present. The body is rendered once per language
# (with _CUR_LANG switched) and the versions are toggled client-side via a body class, so
# t() stays a plain lookup and every string is naturally contiguous (no per-token spans).
_I18N_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n")


def _load_i18n() -> dict:
    langs: dict[str, dict] = {}
    try:
        for fn in sorted(os.listdir(_I18N_DIR)):
            if fn.endswith(".json"):
                with open(os.path.join(_I18N_DIR, fn), encoding="utf-8") as f:
                    langs[fn[:-5]] = json.load(f)
    except FileNotFoundError:
        pass
    return langs


_I18N = _load_i18n()
_DEFAULT_LANG = os.environ.get("TRADINEBOTTE_STATUS_LANG", "en")
_CUR_LANG = _DEFAULT_LANG if _DEFAULT_LANG in _I18N else ("en" if "en" in _I18N else
            (next(iter(_I18N), "en")))


def _langs_ordered() -> list[str]:
    """Available languages, default first."""
    if not _I18N:
        return ["en"]
    rest = sorted(l for l in _I18N if l != _CUR_LANG)
    return ([_CUR_LANG] if _CUR_LANG in _I18N else []) + rest


def t(key: str, **kw) -> str:
    """Translate `key` for the current language (_CUR_LANG). Returns plain text; falls back
    to English then to the key itself so a missing string never crashes the page."""
    d = _I18N.get(_CUR_LANG) or {}
    s = d.get(key)
    if s is None:
        s = (_I18N.get("en") or {}).get(key, key)
    if not kw:
        return s
    # A stray/misnamed brace in a translation must degrade, never blank the whole page.
    try:
        return s.format(**kw)
    except (KeyError, IndexError, ValueError):
        return s


# Account labels + the real-money (_LIVE_BOTS) set are DERIVED from inventory.toml (the
# single source of truth), not hand-maintained here. Fail-soft: if the inventory can't be
# read/parsed, degrade to plain acct-N labels + no live bots so the 60s status page never
# crashes over a label. The account COUNT/ORDER stays anchored to the collected users (the
# min() clamp in main()); inventory only enriches label text + the is_live flag.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import inventory_labels as _inv  # noqa: E402
    _INV_ROWS = _inv.load_rows()
except Exception:
    _INV_ROWS = []

if _INV_ROWS:
    _ACCOUNT_LABELS = _inv.account_labels(_INV_ROWS)
    # Bots running with REAL money (is_live=true) — all others default to SIM. Keyed
    # (acct_short, bot_name) to match _mode_badge. Empty today (all sim); auto-tracks
    # inventory the day a bot goes live.
    _LIVE_BOTS: set[tuple[str, str]] = _inv.live_bots(_INV_ROWS)
else:
    _ACCOUNT_LABELS = [f"acct-{i + 1}" for i in range(12)]   # fail-soft plain labels
    _LIVE_BOTS = set()


def _fmt_pnl(v) -> str:
    if v is None:
        return "<span class='no-data'>—</span>"
    cls = "pnl-pos" if v >= 0 else "pnl-neg"
    sign = "+" if v >= 0 else ""
    return f"<span class='{cls}'>{sign}${v:.2f}</span>"


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def _wr(w: int, l: int) -> str:
    total = w + l
    if total == 0:
        return "—"
    pct = 100.0 * w / total
    cls = "win" if pct >= 90 else ("open-t" if pct >= 70 else "loss")
    return f"<span class='{cls}'>{pct:.1f}%</span>"


def _render_payload_summary(bot_name: str, payload: dict, now: int) -> str:
    if not payload:
        return "—"

    def _ago(ts):
        if not ts:
            return t("ago_never")
        age = now - int(ts)
        if age < 120:
            return t("ago_s", n=age)
        if age < 7200:
            return t("ago_min", n=age // 60)
        if age < 172800:
            return t("ago_h", n=age // 3600)
        return t("ago_d", n=age // 86400)

    parts = []
    if bot_name in ("live_bot", "grid_bot", "swing_bot", "account_bot"):
        # Cumulative realized PnL is the headline; daily shown alongside. Fall back to
        # daily_pnl for heartbeats from bots predating pnl_total. account_bot shares this
        # payload shape (pnl_total/trades_total/capital) — same labels across trading bots;
        # it reports a feed message ts rather than a book ts.
        pt = payload.get("pnl_total")
        if pt is not None:
            parts.append(f"{t('k_pnl')}${pt:+.2f}")
        dp = payload.get("daily_pnl")
        if dp is not None:
            # When pnl_total is present it is the headline (pnl=); daily becomes day=.
            # For old heartbeats without pnl_total, daily is shown as pnl=.
            parts.append(f"{t('k_day')}${dp:+.2f}" if pt is not None else f"{t('k_pnl')}${dp:+.2f}")
        tt = payload.get("trades_total")
        if tt is not None:
            parts.append(f"{t('k_trades')}{tt}")
        cap = payload.get("capital")
        if cap is not None:
            parts.append(f"{t('k_cap')}${cap:.0f}")
        ot = payload.get("open_trades")
        if ot is not None:
            parts.append(f"{t('k_open')}{ot}")
        ts = payload.get("last_feed_msg_ts") if bot_name == "account_bot" else payload.get("last_book_ts")
        if ts:
            parts.append(f"{t('k_feed') if bot_name == 'account_bot' else t('k_book')}{_ago(ts)}")
    elif bot_name == "accumulation_bot":
        h = payload.get("holdings_btc")
        if h is not None:
            parts.append(f"{t('k_btc')}{h:.4f}")
        u = payload.get("free_usdt")
        if u is not None:
            parts.append(f"{t('k_usdt')}${u:.0f}")
        e = payload.get("avg_entry")
        if e is not None:
            parts.append(f"{t('k_entry')}{e:.0f}")
        r = payload.get("total_realized")
        if r is not None:
            parts.append(f"{t('k_pnl')}${r:+.2f}")
    elif bot_name == "orderbook_bot":
        tp = payload.get("total_pnl")
        if tp is not None:
            parts.append(f"{t('k_pnl')}${tp:+.2f}")
        op = payload.get("open_positions")
        if op is not None:
            parts.append(f"{t('k_pos')}{op}")
        lp = payload.get("last_price")
        if lp:
            parts.append(f"{t('k_px')}${lp:,.0f}")
    elif bot_name == "feed":
        ws = payload.get("ws_connected")
        if ws is not None:
            parts.append(t("k_ws_on") if ws else t("k_ws_off"))
        mt = payload.get("msgs_total")
        if mt is not None:
            parts.append(f"{t('k_msgs')}{mt}")
        ts = payload.get("last_book_ts")
        if ts:
            parts.append(f"{t('k_book')}{_ago(ts)}")
    elif bot_name == "indicators":
        ts = payload.get("last_pub_ts")
        if ts:
            parts.append(f"{t('k_pub')}{_ago(ts)}")

    # Data-recording freshness — uniform across every family that persists a
    # time-series table (snapshots / accum_snapshots / …). ⚠ when the table has
    # stopped growing while the bot still heartbeats (the 2026-06-16 failure mode).
    wts = payload.get("last_write_ts")
    if wts is not None:
        mark = " ⚠" if now - int(wts) > _DATA_STALE_AFTER else ""
        parts.append(f"{t('k_data')}{_ago(wts)}{mark}")

    return " · ".join(parts) if parts else "—"


def _mode_badge(acct_short: str, bot_name: str) -> str:
    if (acct_short, bot_name) in _LIVE_BOTS:
        return f"<span class='badge live-mode'>{t('badge_live')}</span>"
    return f"<span class='badge sim'>{t('badge_sim')}</span>"


_FLAG_KEYS = {"ALIVE": "badge_alive", "STALE": "badge_stale", "DEAD": "badge_dead",
              "MISSING": "flag_missing"}


def _flag_label(flag: str) -> str:
    """Translated status-badge text; the CSS class still uses the English flag.lower()."""
    return t(_FLAG_KEYS[flag]) if flag in _FLAG_KEYS else flag


def _key_metric(bot_name: str, payload: dict) -> str:
    """Return the single most important display value for a bot heartbeat pill."""
    if bot_name in ("live_bot", "grid_bot", "swing_bot", "account_bot"):
        pnl = payload.get("daily_pnl")
        if pnl is not None:
            sign = "+" if pnl >= 0 else "-"
            return f"{sign}${abs(pnl):.2f}"
    elif bot_name == "accumulation_bot":
        btc = payload.get("holdings_btc")
        if btc is not None:
            return f"{btc:.4f} BTC"
    elif bot_name == "orderbook_bot":
        # No daily window in the payload; cumulative realized PnL is the headline.
        pnl = payload.get("total_pnl")
        if pnl is not None:
            sign = "+" if pnl >= 0 else "-"
            return f"{sign}${abs(pnl):.2f}"
    return ""


# ─── Bot-family view (primary "by bot, not by account" layout) ───────────────

# Families that carry a comparable cumulative PnL (pnl_total) — these get windowed
# PnL. accumulation_bot heartbeats holdings/realized (its pnl_total is a flat 0), so it
# is a detail family: its rows show the payload summary, not a misleading windowed $0.
_PNL_FAMILIES = {"live_bot", "grid_bot", "swing_bot", "orderbook_bot", "account_bot"}
_FAMILY_ORDER = ["live_bot", "grid_bot", "swing_bot", "orderbook_bot", "account_bot",
                 "accumulation_bot", "feed", "feed5m", "cex_feed", "indicators"]
def _family_title(fam: str) -> str:
    """"<bot_name> · <translated descriptor>". The bot name is a literal identifier; only
    the descriptor is translated (i18n key fam_<bot_name>)."""
    desc = t(f"fam_{fam}")
    return f"{fam} · {desc}" if desc != f"fam_{fam}" else fam
def _reset_mark() -> str:
    return f"<span class='rst' title='{escape(t('reset_title'))}'>⚠</span>"


_WINS = ("daily", "weekly", "monthly", "alltime")


def _fmt_pnl_val(v) -> str:
    if not isinstance(v, (int, float)):
        return "<span class='no-data'>—</span>"
    cls = "pnl-pos" if v >= 0 else "pnl-neg"
    return f"<span class='{cls}'>{'+' if v >= 0 else '-'}${abs(v):.2f}</span>"


def _win_spans(rec: dict) -> str:
    """The four window spans for one bot's PnL record; CSS shows only the active one."""
    return "".join(
        f"<span class='pw pw-{w}'>{_fmt_pnl_val(rec.get(w))}"
        f"{_reset_mark() if rec.get(w + '_reset') else ''}</span>"
        for w in _WINS
    )


def _sum_windows(recs: list) -> dict:
    """Aggregate several bots' window records into one {window: (sum|None, reset)}."""
    agg = {}
    for w in _WINS:
        vals = [r.get(w) for r in recs if isinstance(r.get(w), (int, float))]
        reset = any(r.get(w + "_reset") for r in recs)
        agg[w] = (sum(vals), reset) if vals else (None, False)
    return agg


def _win_spans_agg(agg: dict) -> str:
    return "".join(
        f"<span class='pw pw-{w}'>{_fmt_pnl_val(agg[w][0])}"
        f"{_reset_mark() if agg[w][1] else ''}</span>"
        for w in _WINS
    )


def _render_bot_families(heartbeats: list, pnl_windows: dict, now: int) -> str:
    """Group every bot by family (not by account); windowed PnL toggled via the buttons.

    Each family is a section; its instances (one per account) are the rows. PnL families
    show windowed PnL + capital; detail families (feeds, accumulation) show the payload
    summary. The account is demoted to a per-row label.
    """
    if not heartbeats:
        return f"<p class='no-data'>{escape(t('no_hb_data'))}</p>"
    pw = pnl_windows or {}
    by_family: dict[str, list] = {}
    for r in heartbeats:
        by_family.setdefault(r["bot_name"], []).append(r)
    ordered = [f for f in _FAMILY_ORDER if f in by_family]
    ordered += sorted(f for f in by_family if f not in _FAMILY_ORDER)

    sections = ""
    for fam in ordered:
        rows = sorted(by_family[fam], key=lambda r: (r.get("_label") or r["account"]))
        is_pnl = fam in _PNL_FAMILIES
        recs = [pw.get(f"{r['account']}|{fam}", {}) for r in rows]
        alive = sum(1 for r in rows if r["flag"] == "ALIVE")
        alive_cls = "alive" if alive == len(rows) else ("dead" if alive == 0 else "stale")
        title = escape(_family_title(fam))
        head_pnl = (f"<span class='fam-pnl'>{_win_spans_agg(_sum_windows(recs))}</span>"
                    if is_pnl else "")

        irows = ""
        for r, rec in zip(rows, recs):
            flag = r["flag"]
            acct_short = r.get("_label") or r["account"]
            payload = r.get("payload", {}) or {}
            mode = _mode_badge(acct_short, fam)
            data_badge = (f"<span class='badge stale' style='font-size:.6em' "
                          f"title='{escape(t('badge_data_title'))}'>{t('badge_data')}</span>"
                          if _data_flag(payload, now) else "")
            detail = escape(_render_payload_summary(fam, payload, now))
            if is_pnl:
                vcls = " stale-val" if flag != "ALIVE" else ""
                cap = payload.get("capital")
                cap_str = (f"<span class='fam-cap'>cap ${cap:,.0f}</span>"
                           if isinstance(cap, (int, float)) else "")
                val_cell = f"<span class='fam-val{vcls}'>{_win_spans(rec)}</span>{cap_str}"
            else:
                val_cell = f"<span class='fam-detail'>{detail}</span>"
            age_min = r["age_s"] // 60
            age_str = f"{age_min}min" if age_min < 120 else f"{age_min // 60}h{age_min % 60:02d}m"
            irows += (
                f"<div class='fam-row tt'>"
                f"<span class='badge {flag.lower()}' style='font-size:.6em'>{_flag_label(flag)}</span>"
                f"{data_badge}{mode}"
                f"<span class='fam-acct'>{escape(acct_short)}</span>"
                f"{val_cell}"
                f"<div class='tip tip-up'>"
                f"<div class='tip-row'>{detail}</div>"
                f"<div class='tip-dim'>{escape(t('age', a=age_str))} · v={escape(r['version'])}</div>"
                f"</div></div>"
            )
        sections += (
            f"<div class='fam'>"
            f"<div class='fam-head'>"
            f"<span class='fam-name'>{title}</span>"
            f"<span class='fam-count {alive_cls}'>{alive}/{len(rows)}</span>"
            f"{head_pnl}"
            f"</div>{irows}</div>"
        )
    return f"<div class='families'>{sections}</div>"


def _render_trade_table(rows: list, db_type: str = "live") -> str:
    if not rows:
        return f"<p class='no-data'>{escape(t('no_trades'))}</p>"
    head_extra = f"<th>{t('th_market')}</th>" if db_type == "live" else ""
    html = (
        "<table class='tr-table'><thead><tr>"
        f"<th>{t('th_num')}</th><th>{t('th_time')}</th><th>{t('th_dir')}</th><th>{t('th_result')}</th>"
        f"<th>{t('th_entry')}</th><th>{t('th_pnl')}</th><th>{t('th_capital')}</th>{head_extra}"
        "</tr></thead><tbody>"
    )
    for r in rows:
        result = r.get("result", "")
        cls = "win" if result == "WIN" else ("loss" if result == "LOSS" else "open-t")
        result_disp = (t("res_win") if result == "WIN"
                       else t("res_loss") if result == "LOSS" else result)
        pnl = r.get("pnl")
        cap = r.get("capital")
        ep  = r.get("entry_price")
        ep_str  = f"{ep:.4f}" if ep is not None else "—"
        cap_str = f"${cap:.2f}" if cap is not None else "—"
        html += (
            f"<tr>"
            f"<td class='{cls}'>#{r.get('id','?')}</td>"
            f"<td>{_fmt_ts(r.get('entry_ts'))}</td>"
            f"<td>{escape(r.get('direction',''))}</td>"
            f"<td class='{cls}'>{escape(result_disp)}</td>"
            f"<td>{ep_str}</td>"
            f"<td>{_fmt_pnl(pnl)}</td>"
            f"<td>{cap_str}</td>"
        )
        if db_type == "live":
            q = r.get("q", "")
            html += f"<td title='{escape(q)}'>{escape(q[:30])}…</td>"
        html += "</tr>"
    html += "</tbody></table>"
    return html


def _render_bot_section(hb_rows: list, now: int) -> str:
    """Compact per-bot rows — status+mode+name+key metric always visible; details on hover."""
    if not hb_rows:
        return ""
    rows_html = ""
    for r in hb_rows:
        flag = r["flag"].lower()
        acct_short = (r.get("_label") or "").split()[0]
        bot = r["bot_name"]
        age_min = r["age_s"] // 60
        age_str = f"{age_min}min" if age_min < 120 else f"{age_min//60}h{age_min%60:02d}m"
        detail = _render_payload_summary(bot, r.get("payload", {}), now)
        mode_cell = _mode_badge(acct_short, bot)
        km = _key_metric(bot, r.get("payload", {}))
        km_cls = " pnl-pos" if km.startswith("+") else (" pnl-neg" if km.startswith("-") else "")
        km_html = f"<span class='bot-metric{km_cls}'>{escape(km)}</span>" if km else ""
        tip_content = (
            f"<div class='tip-row'>{escape(detail)}</div>"
            f"<div class='tip-dim'>{escape(t('age', a=age_str))} · v={escape(r['version'])}</div>"
        )
        rows_html += (
            f"<div class='bot-row tt'>"
            f"<span class='badge {flag}' style='font-size:.62em'>{_flag_label(r['flag'])}</span>"
            f"{mode_cell}"
            f"<span class='bot-name'>{escape(bot)}</span>"
            f"{km_html}"
            f"<div class='tip tip-up'>{tip_content}</div>"
            f"</div>"
        )
    return f"<div class='bot-rows'>{rows_html}</div>"


def _render_account_card(label: str, data: dict, hb_rows: list | None = None) -> str:
    version = escape(data.get("version", "?"))
    error   = data.get("error", "")

    # Service dots: compact colored circles in header; full names on hover
    services = data.get("services", [])
    if services:
        dots = "".join(
            f"<span class='svc-dot {'ok' if s['active'] else 'bad'}'></span>"
            for s in services
        )
        svc_tip_rows = "".join(
            f"<div class='tip-row'>"
            f"<span class='{'alive' if s['active'] else 'dead'}'>"
            f"{'✓' if s['active'] else '✗'}</span> {escape(s['unit'])}"
            f"</div>"
            for s in services
        )
        svc_dots_html = (
            f"<div class='tt svc-dots'>"
            f"{dots}"
            f"<div class='tip tip-up'>"
            f"<span class='tip-label'>{escape(t('services'))}</span>{svc_tip_rows}"
            f"</div>"
            f"</div>"
        )
    else:
        svc_dots_html = ""

    header = (
        f"<div class='account-header'>"
        f"<div class='tt' style='flex:1;min-width:0'>"
        f"<span class='name'>{escape(label)}</span>"
        f"<div class='tip tip-up'><span class='tip-dim'>v={version}</span></div>"
        f"</div>"
        f"{svc_dots_html}"
        f"</div>"
    )

    if error:
        _msg = (t("card_unreachable") if error == "unreachable"
                else t("card_collect_failed", err=escape(str(error))))
        return f"<div class='account'>{header}<p class='no-data'>⚠ {_msg}</p></div>"

    # Determine whether this account runs Polymarket bots (live_bot / account_bot).
    _POLY_BOT_NAMES = {"live_bot", "account_bot"}
    has_poly = any(r["bot_name"] in _POLY_BOT_NAMES for r in (hb_rows or []))

    live = data.get("live")
    stats_html = ""
    trade_html = ""
    open_html  = ""

    # Big metrics (Capital, Today PnL) come from the primary bot's heartbeat payload —
    # the single source of truth shared with the pills and the fleet headline. live.db
    # is used only for the Polymarket win-rate and the trade tables further below.
    primary = None
    for _name in ("live_bot", "account_bot", "grid_bot", "swing_bot"):
        primary = next((r for r in (hb_rows or []) if r["bot_name"] == _name), None)
        if primary:
            break
    if primary:
        p          = primary.get("payload", {}) or {}
        acct_s     = (primary.get("_label") or "").split()[0]
        flag       = primary.get("flag", "")
        cap        = p.get("capital")
        today_pnl  = p.get("daily_pnl")
        life_pnl   = p.get("pnl_total")
        trades_tot = p.get("trades_total")
        open_n     = p.get("open_trades")
        mode_badge = _mode_badge(acct_s, primary["bot_name"])
        cap_str    = f"${cap:,.2f}" if isinstance(cap, (int, float)) else "—"
        pnl_cls    = "pnl-pos" if (today_pnl or 0) >= 0 else "pnl-neg"
        pnl_str    = (f"{'+' if today_pnl >= 0 else '-'}${abs(today_pnl):.2f}"
                      if isinstance(today_pnl, (int, float)) else "—")
        # Win-rate sub-line + tip from live.db (Polymarket only; payload has no W/L split).
        # Win rate is over RESOLVED trades only, so the count shown next to it is the
        # resolved count (w+l) — not trades_total, which also includes open positions.
        cap_sub = mode_badge
        wr_tip = ""
        if has_poly and live:
            totals = (live.get("totals") or [{}])[0]
            w = totals.get("w", 0) or 0
            l = totals.get("l", 0) or 0
            resolved = w + l
            total_all = trades_tot if trades_tot is not None else (totals.get("t", 0) or 0)
            cap_sub = f"{_wr(w, l)} · {resolved}T"
            open_extra = t("tip_open_suffix", n=open_n) if open_n else ""
            wr_tip = (f"<div class='tip-row'>{t('tip_win_rate', wr=_wr(w, l), w=w, l=l)}</div>"
                      f"<div class='tip-row'>{t('tip_resolved_total', r=resolved, t=total_all, open=open_extra)}</div>")
        # Age-gate: when the source heartbeat is STALE/DEAD, dim the values and show a
        # badge so last-known payload numbers aren't read as current.
        is_stale   = bool(flag) and flag != "ALIVE"
        vcls       = " stale-val" if is_stale else ""
        flag_badge = (f" <span class='badge {flag.lower()}'>{_flag_label(flag)}</span>"
                      if is_stale else "")
        stale_note = (
            f"<div class='tip-row'><span class='{flag.lower()}'>"
            f"{escape(t('hb_lastknown', flag=_flag_label(flag)))}</span></div>"
            if is_stale else ""
        )
        open_tip = (f"<div class='tip-row'><span style='color:#8b949e;min-width:64px;"
                    f"display:inline-block'>{escape(t('lbl_open'))}</span> {open_n}</div>"
                    if open_n is not None else "")
        stats_html = (
            f"<div class='big-metrics'>"
            f"<div class='metric-big tt'>"
            f"<div class='lbl'>{escape(t('lbl_capital'))}</div>"
            f"<div class='val{vcls}'>{cap_str}</div>"
            f"<div class='metric-sub'>{cap_sub}{flag_badge}</div>"
            f"<div class='tip tip-up'>"
            f"<span class='tip-label'>{escape(t('tip_from_heartbeat', bot=primary['bot_name']))}</span>"
            f"{wr_tip}"
            f"<div class='tip-row'>{escape(t('lbl_since_reset'))} {_fmt_pnl(life_pnl)}</div>"
            f"{stale_note}"
            f"</div></div>"
            f"<div class='metric-big tt'>"
            f"<div class='lbl'>{escape(t('lbl_today_pnl'))}</div>"
            f"<div class='val {pnl_cls}{vcls}'>{pnl_str}</div>"
            f"<div class='metric-sub'>{mode_badge}{flag_badge}</div>"
            f"<div class='tip tip-up'>"
            f"<span class='tip-label'>{escape(t('tip_pnl_from_hb'))}</span>"
            f"<div class='tip-row'><span style='color:#8b949e;min-width:64px;display:inline-block'>{escape(t('lbl_today_utc'))}</span>"
            f" {_fmt_pnl(today_pnl)}</div>"
            f"<div class='tip-row'><span style='color:#8b949e;min-width:64px;display:inline-block'>{escape(t('lbl_since_reset'))}</span>"
            f" {_fmt_pnl(life_pnl)}</div>"
            f"{open_tip}{stale_note}"
            f"</div></div>"
            f"</div>"
        )

    # Polymarket trade tables (open + recent) from live.db — payloads don't carry them.
    if live and has_poly:
        open_trades = live.get("open", [])
        if open_trades:
            def _ep(r):
                ep = r.get("entry_price")
                return f"{ep:.4f}" if ep is not None else "?"
            badges = "".join(
                f"<span class='badge open-b' style='margin-right:4px'>"
                f"#{r['id']} {r.get('direction','')} {_ep(r)}</span>"
                for r in open_trades
            )
            open_html = f"<div class='open-trades'><span style='color:#d29922'>{escape(t('open_label'))}</span> {badges}</div>"
        recent = live.get("recent", [])
        if recent:
            trade_html = (
                f"<details><summary>{escape(t('last_n_trades', n=len(recent)))}</summary>"
                f"{_render_trade_table(recent, 'live')}"
                f"</details>"
            )

    # Grid bots track aggregate state (bounds / cycles / level fills / halted), not a
    # per-trade log — surface it from grid_state + grid_levels. Covers both the standard
    # path (an account running only a grid) and an alternate data dir (~/tradinebotte-grid).
    grid_html = ""
    for gr in (data.get("grids") or []):
        st = (gr.get("state") or [{}])[0]
        if not st:
            continue
        lvls = {row.get("status"): row.get("n", 0) for row in (gr.get("levels") or [])}
        total_lvls = sum(lvls.values())
        holding    = lvls.get("sell_placed", 0)        # bought, waiting to sell
        lo, hi     = st.get("grid_lower"), st.get("grid_upper")
        bounds = (f"${lo/1000:.1f}k–${hi/1000:.1f}k"
                  if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) else "—")
        cycles  = st.get("total_cycles")
        profit  = st.get("total_profit_usd")
        sym     = escape(str(st.get("symbol", "")))
        halted_badge = (f"<span class='badge dead' style='margin-left:5px'>{t('grid_halted')}</span>"
                        if st.get("halted") else "")
        profit_cls = "pnl-pos" if (profit or 0) >= 0 else "pnl-neg"
        grid_html += (
            f"<div style='margin-top:4px;font-size:.8em'>"
            f"<span style='color:#8b949e;text-transform:uppercase;letter-spacing:.8px;"
            f"font-size:.85em'>{escape(t('grid_label'))}</span>"
            f" {sym} · <span style='color:#58a6ff'>{bounds}</span>"
            f" · {escape(t('grid_holding', h=holding, t=total_lvls))}"
            f" · {escape(t('grid_cycles', n=cycles if cycles is not None else '?'))}"
            f" · <span class='{profit_cls}'>{_fmt_pnl(profit)}</span>"
            f"{halted_badge}"
            f"</div>"
        )

    cex_html = ""
    accum = data.get("accum")
    if accum:
        accum_port = (accum.get("portfolio") or [{}])[0]
        accum_tot  = (accum.get("totals") or [{}])[0]
        btc  = accum_port.get("holdings_btc")
        usdt = accum_port.get("free_usdt")
        avg  = accum_port.get("avg_entry")
        btc_str  = f"{btc:.6f} BTC" if btc is not None else "—"
        usdt_str = f" · {t('accum_free', u=f'{usdt:.0f}')}" if usdt is not None else ""
        avg_str  = f" · {t('accum_avg', a=f'{avg:.0f}')}" if avg is not None else ""
        cex_html += (
            f"<div style='margin-top:4px;font-size:.8em'>"
            f"<span style='color:#8b949e;text-transform:uppercase;letter-spacing:.8px;"
            f"font-size:.85em'>accum_bot</span>"
            f" — <span style='color:#58a6ff'>{btc_str}</span>{usdt_str}{avg_str}"
            f" · {accum_tot.get('t', 0)}T"
            f"</div>"
        )

    now = int(time.time())
    bot_section = _render_bot_section(hb_rows or [], now)

    return (
        f"<div class='account'>"
        f"{header}{stats_html}{open_html}{trade_html}{grid_html}{cex_html}{bot_section}"
        f"</div>"
    )


def _fmt_age(secs: int) -> str:
    if secs < 0:
        return "—"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# Status colours (matches the page's dark palette).
_C_OK, _C_WARN, _C_BAD, _C_MUTE = "#3fb950", "#d29922", "#f85149", "#8b949e"


def _render_expected_actual(inventory: list, deploys: list, heartbeats: list,
                            user_to_label: dict) -> str:
    """Additive section: every EXPECTED bot (from inventory) vs what actually reports.

    Surfaces bots that are expected but silent (no heartbeat at all → MISSING, today
    invisible), declared-vs-reported mode mismatches, and the latest deploy per bot.
    """
    if not inventory:
        return ""
    hb_by  = {(r["account"], r["bot_name"]): r for r in heartbeats}
    dep_by = {(d["account"], d["bot_name"]): d for d in deploys}
    now = int(time.time())
    n_missing = n_mismatch = 0
    body = []
    for inv in sorted(inventory, key=lambda r: (r.get("account", ""), r.get("bot_name", ""))):
        acct, bot = inv.get("account", ""), inv.get("bot_name", "")
        label = user_to_label.get(acct, acct)
        hb = hb_by.get((acct, bot))

        kind = inv.get("kind", "bot") or "bot"
        if hb is None:
            # A bot that never reported is a real problem (today it would be invisible);
            # a service that never reports is fine — its liveness is the systemctl section
            # below (the collector cannot heartbeat itself).
            if kind == "service":
                flag, fcol, flag_disp = "n/a", _C_MUTE, t("flag_na")
            else:
                flag, fcol, flag_disp = "MISSING", _C_BAD, t("flag_missing")
                n_missing += 1
        else:
            flag = hb["flag"]
            fcol = {"ALIVE": _C_OK, "STALE": _C_WARN}.get(flag, _C_BAD)
            flag_disp = _flag_label(flag)

        is_live  = inv.get("is_live")
        declared = "—" if is_live is None else ("live" if is_live else "sim")
        reported = (hb or {}).get("payload", {}).get("mode") if hb else None
        if reported and is_live is not None and reported != declared:
            mode_cell = f"<span style='color:{_C_BAD}'>{escape(declared)}≠{escape(reported)}</span>"
            n_mismatch += 1
        else:
            mode_cell = escape(declared)

        dep = dep_by.get((acct, bot))
        if dep:
            dep_cell = (f"{escape((dep.get('git_hash') or '?')[:7])} "
                        f"{escape(str(dep.get('result') or ''))} "
                        f"<span style='color:{_C_MUTE}'>({_fmt_age(now - int(dep['last_ts']))})</span>")
        else:
            dep_cell = f"<span style='color:{_C_MUTE}'>—</span>"

        body.append(
            f"<tr><td>{escape(label)}</td><td>{escape(bot)}</td>"
            f"<td style='color:{_C_MUTE}'>{escape(inv.get('kind','') or '')}</td>"
            f"<td>{mode_cell}</td>"
            f"<td style='color:{fcol};font-weight:600'>{escape(flag_disp)}</td>"
            f"<td>{dep_cell}</td></tr>"
        )

    note = []
    if n_missing:
        note.append(f"<span style='color:{_C_BAD}'>{escape(t('ea_silent', n=n_missing))}</span>")
    if n_mismatch:
        note.append(f"<span style='color:{_C_BAD}'>{escape(t('ea_mismatch', n=n_mismatch))}</span>")
    sub = ("  ·  ".join(note) if note
           else f"<span style='color:{_C_OK}'>{escape(t('ea_all_present'))}</span>")
    return (
        f"<h2>{escape(t('h_expected_actual'))} <span style='font-size:.5em;color:{_C_MUTE};font-weight:400'>"
        f"{escape(t('ea_declared', n=len(inventory)))} · {sub}</span></h2>"
        "<table class='hb-table'><thead><tr>"
        f"<th>{t('th_acct')}</th><th>{t('th_bot')}</th><th>{t('th_kind')}</th>"
        f"<th>{t('th_mode')}</th><th>{t('th_heartbeat')}</th><th>{t('th_last_deploy')}</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def _render_banners(accounts: list, heartbeats: list) -> str:
    """Make a degraded collection cycle look like a failure, never like silence.

    The collector account (index 0) is the single source of all heartbeat, inventory
    and deploy data. If its SSH/collect failed, the entire fleet view is blank — which
    must read as 'fleet status unknown', not 'all quiet'. Other accounts failing only
    affects their own card, but is still called out so a partial page is visibly partial.
    """
    if not accounts:
        return ""
    banners = []
    collector = accounts[0]
    coll_err = collector.get("error")
    if coll_err:
        banners.append(
            f"<div class='banner bad'>{t('banner_collector_down', err=escape(str(coll_err)))}</div>"
        )
    elif not heartbeats:
        banners.append(f"<div class='banner warn'>{t('banner_no_heartbeats')}</div>")

    # Per-account collect failures (the collector is handled above). Their own cards
    # show '⚠ unreachable'; surface the set so the partial page is obviously partial.
    failed = [
        (label.split()[0] if label else "?")
        for label, acc in zip(_ACCOUNT_LABELS, accounts)
        if acc is not collector and acc.get("error")
    ]
    if failed:
        banners.append(
            f"<div class='banner warn'>"
            f"{t('banner_partial', n=len(failed), accts=escape(', '.join(failed)))}</div>"
        )
    return "".join(banners)


def _render_html(
    heartbeats: list,
    accounts: list,
    generated_at: datetime,
    collection_s: float,
    inventory: list | None = None,
    deploys: list | None = None,
    user_to_label: dict | None = None,
    pnl_windows: dict | None = None,
) -> str:
    pnl_windows = pnl_windows or {}
    global _CUR_LANG
    _saved_lang = _CUR_LANG

    # ── Language-independent data prep (computed once) ───────────────────────
    # The collector account (index 0) is the sole source of fleet heartbeat data — if it
    # failed, an empty `heartbeats` means "unknown", not "nothing wrong".
    collector_down = bool(accounts and accounts[0].get("error"))
    hb_issues  = sum(1 for r in heartbeats if r["flag"] != "ALIVE")
    svc_issues = sum(1 for acc in accounts for svc in acc.get("services", []) if not svc["active"])
    unreachable = sum(1 for a in accounts if a.get("error"))
    total_issues = hb_issues + svc_issues + unreachable
    dot_cls = "bad" if collector_down else ("ok" if total_issues == 0
                                            else ("warn" if hb_issues == 0 else "bad"))
    ts_str = generated_at.strftime("%Y-%m-%d %H:%M UTC")

    total_bots = len(heartbeats)
    alive_bots = sum(1 for r in heartbeats if r["flag"] == "ALIVE")
    # With no heartbeats (collector down) never render a falsely-green "0/0".
    if total_bots == 0:
        alive_cls, alive_display = "dead", "—"
    else:
        alive_cls = "alive" if alive_bots == total_bots else "stale"
        alive_display = f"{alive_bots}/{total_bots}"
    hb_cls  = "alive" if hb_issues == 0 else "dead"
    svc_cls = "alive" if svc_issues == 0 else "dead"
    unr_cls = "alive" if unreachable == 0 else "dead"

    # Fleet PnL — Today + Lifetime from the payloads; weekly/monthly from pnl_windows.
    today_pnl_total = life_pnl_total = 0.0
    for _hb in heartbeats:
        _pl = _hb.get("payload") or {}
        if isinstance(_pl.get("daily_pnl"), (int, float)):
            today_pnl_total += _pl["daily_pnl"]
        if isinstance(_pl.get("pnl_total"), (int, float)):
            life_pnl_total += _pl["pnl_total"]

    # BTC 24h (Binance) — labels are neutral (BTC/H/L), so this is language-independent.
    btc_24h   = _fetch_btc_24h()
    btc_price = float(btc_24h.get("lastPrice") or 0)
    btc_chg   = float(btc_24h.get("priceChangePercent") or 0)
    btc_high  = float(btc_24h.get("highPrice") or 0)
    btc_low   = float(btc_24h.get("lowPrice") or 0)
    if btc_price:
        chg_cls  = "up" if btc_chg >= 0 else "dn"
        chg_sign = "+" if btc_chg >= 0 else ""
        chg_html = (f"<span class='chg {chg_cls}'>{chg_sign}{btc_chg:.2f}%</span>"
                    if btc_24h else "")
        range_html = (f"<span style='color:#484f58;font-size:.88em'>H&thinsp;${btc_high:,.0f}"
                      f" &nbsp; L&thinsp;${btc_low:,.0f}</span>" if btc_high else "")
        btc_price_html = (f"<div class='btc-price'><span>BTC</span>"
                          f"<span class='price'>${btc_price:,.0f}</span>{chg_html}{range_html}</div>")
    else:
        btc_price_html = ""

    now = int(time.time())

    def _acct_hb(label: str) -> list:
        short = label.split()[0]
        return [r for r in heartbeats if (r.get("_label") or "").split()[0] == short]

    langs = _langs_ordered()

    # ── Render the whole body once per language (t() reads _CUR_LANG) ────────
    # Each language version is wrapped in a .langbox toggled client-side by a body class,
    # so every string is contiguous and even native title= attributes are per-language.
    langboxes = []
    titles = {}
    lang_sel = ("<span class='lang-sel'>" + "".join(
        f"<button class='lbtn lbtn-{L}' onclick=\"setLang('{L}')\">{escape(L.upper())}</button>"
        for L in langs) + "</span>")
    for lang in langs:
        _CUR_LANG = lang
        titles[lang] = t("title")

        status_text = (t("status_collector_down") if collector_down
                       else t("status_nominal") if total_issues == 0
                       else t("status_issues", n=total_issues))

        if pnl_windows:
            fleet_pnl_html = _win_spans_agg(_sum_windows(list(pnl_windows.values())))
        else:
            _s = "+" if today_pnl_total >= 0 else ""
            _c = "pnl-pos" if today_pnl_total >= 0 else "pnl-neg"
            fleet_pnl_html = f"<span class='{_c}'>{_s}${today_pnl_total:.2f}</span>"

        summary_bar = (
            f"<div class='summary-bar'>"
            f"<div class='sb-item'><div class='lbl'>{escape(t('sb_bots_alive'))}</div>"
            f"<div class='val {alive_cls}'>{alive_display}</div></div>"
            f"<div class='sb-item tt'>"
            f"<div class='lbl'>{escape(t('sb_pnl_window'))}</div>"
            f"<div class='val'>{fleet_pnl_html}</div>"
            f"<div class='tip'>"
            f"<span class='tip-label'>{escape(t('sb_tip_title'))}</span>"
            f"<div class='tip-row'><span style='color:#8b949e;min-width:74px;display:inline-block'>{escape(t('lbl_today_utc'))}</span>"
            f" {_fmt_pnl(today_pnl_total)}</div>"
            f"<div class='tip-row'><span style='color:#8b949e;min-width:74px;display:inline-block'>{escape(t('lbl_since_reset'))}</span>"
            f" {_fmt_pnl(life_pnl_total)}</div>"
            f"</div></div>"
            f"<div class='sb-item'><div class='lbl'>{escape(t('sb_hb_issues'))}</div>"
            f"<div class='val {hb_cls}'>{hb_issues}</div></div>"
            f"<div class='sb-item'><div class='lbl'>{escape(t('sb_svc_issues'))}</div>"
            f"<div class='val {svc_cls}'>{svc_issues}</div></div>"
            f"<div class='sb-item'><div class='lbl'>{escape(t('sb_unreachable'))}</div>"
            f"<div class='val {unr_cls}'>{unreachable}</div></div>"
            f"</div>"
        )

        win_toggle = (
            f"<div class='win-toggle'>"
            f"<span class='wt-lbl tt'>{escape(t('win_lbl'))}"
            f"<div class='tip tip-up'>"
            f"<span class='tip-label'>{escape(t('win_tip_title'))}</span>"
            f"<div class='tip-row'>{escape(t('win_tip_daily'))}</div>"
            f"<div class='tip-row'>{escape(t('win_tip_weekmonth'))}</div>"
            f"<div class='tip-row'>{escape(t('win_tip_sincereset'))}</div>"
            f"<div class='tip-dim'>{escape(t('win_tip_note'))}</div>"
            f"</div></span>"
            f"<button class='wbtn wbtn-daily' onclick=\"setWin('daily')\">{escape(t('win_daily'))}</button>"
            f"<button class='wbtn wbtn-weekly' onclick=\"setWin('weekly')\">{escape(t('win_weekly'))}</button>"
            f"<button class='wbtn wbtn-monthly' onclick=\"setWin('monthly')\">{escape(t('win_monthly'))}</button>"
            f"<button class='wbtn wbtn-alltime' onclick=\"setWin('alltime')\">{escape(t('win_alltime'))}</button>"
            f"</div>"
        )

        families_html = _render_bot_families(heartbeats, pnl_windows, now)
        cards_html = "".join(_render_account_card(label, data, _acct_hb(label))
                             for label, data in zip(_ACCOUNT_LABELS, accounts))
        banners_html = _render_banners(accounts, heartbeats)
        expected_html = _render_expected_actual(
            inventory or [], deploys or [], heartbeats, user_to_label or {})

        # The refresh countdown number is a live span JS updates; inject it as the {n} of
        # the (already-translated) "refresh in {n}s" phrase, so word order stays correct.
        _rf_span = "<span class='rf-ct'>60</span>"
        footer = (
            f"<div class='footer'>{escape(t('footer_generated', ts=ts_str))} · "
            f"{escape(t('footer_collection', s=f'{collection_s:.1f}'))} · "
            f"{t('footer_refresh', n=_rf_span)}</div>"
        )

        body_inner = (
            f"<h1><span class='dot {dot_cls}'></span>"
            f" {escape('tradinebotte')} — {escape(status_text)}"
            f"<span class='h1-right'>{btc_price_html}"
            f"<span style='font-size:.6em;color:#8b949e;font-weight:400'>{ts_str}</span>"
            f"{lang_sel}</span></h1>"
            f"{banners_html}{win_toggle}{summary_bar}"
            f"<h2>{escape(t('h_bots_family'))}</h2>{families_html}"
            f"{expected_html}"
            f"<h2>{escape(t('h_accounts'))}</h2><div class='accounts'>{cards_html}</div>"
            f"{footer}"
        )
        langboxes.append(f"<div class='langbox i18-{lang}'>{body_inner}</div>")

    _CUR_LANG = _saved_lang
    default_lang = langs[0]
    title_attrs = " ".join(f'data-title-{L}="{escape(titles[L])}"' for L in langs)
    # Per-language display rules + active-selector highlight (static CSS only knows en/fr).
    lang_css = "".join(
        f"body.lang-{L} .i18-{L}{{display:block}}"
        f"body.lang-{L} .lbtn-{L}{{background:#1f6feb;border-color:#1f6feb;color:#fff;font-weight:600}}"
        for L in langs)

    return f"""<!DOCTYPE html>
<html lang="{default_lang}">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="60">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(titles[default_lang])}</title>
<style>{_CSS}{lang_css}</style>
</head>
<body class="win-daily lang-{default_lang}" {title_attrs}>
{''.join(langboxes)}
<script>
function setWin(w){{
  var b=document.body,c=b.className.replace(/\\bwin-\\w+\\b/g,'').trim();
  b.className=(c?c+' ':'')+'win-'+w;
  try{{localStorage.setItem('tbwin',w);}}catch(e){{}}
}}
function setLang(l){{
  var b=document.body,c=b.className.replace(/\\blang-\\w+\\b/g,'').trim();
  b.className=(c?c+' ':'')+'lang-'+l;
  try{{localStorage.setItem('tblang',l);}}catch(e){{}}
  var tt=b.getAttribute('data-title-'+l);if(tt)document.title=tt;
}}
(function(){{
  var w='daily';try{{w=localStorage.getItem('tbwin')||'daily';}}catch(e){{}}
  setWin(w);
  var l='{default_lang}';try{{l=localStorage.getItem('tblang')||'{default_lang}';}}catch(e){{}}
  if(document.querySelector('.i18-'+l)) setLang(l); else setLang('{default_lang}');
  var t=60;
  setInterval(function(){{t--;if(t<=0)t=60;
    var els=document.getElementsByClassName('rf-ct');
    for(var i=0;i<els.length;i++) els[i].textContent=t;}},1000);
}})();
</script>
</body>
</html>"""


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Generate tradinebotte HTML status page")
    parser.add_argument("--conf", default=os.path.expanduser("~/.tradinebotte-test.conf"))
    _default_out = os.path.expanduser(
        os.environ.get("TRADINEBOTTE_STATUS_OUT", "~/public_html/tradinebottestatus.html")
    )
    parser.add_argument(
        "--out",
        default=_default_out,
        help="Output file (default: ~/public_html/tradinebottestatus.html or $TRADINEBOTTE_STATUS_OUT)",
    )
    parser.add_argument(
        "--lang",
        default=None,
        choices=sorted(_I18N) or None,
        help=("Default UI language (the language shown before the visitor picks another; "
              "the page always ships every language and a switcher). "
              "Default: $TRADINEBOTTE_STATUS_LANG or en."),
    )
    args = parser.parse_args()

    if args.lang:
        global _CUR_LANG
        _CUR_LANG = args.lang

    if not os.path.exists(args.conf):
        print(f"Config not found: {args.conf}", file=sys.stderr)
        sys.exit(1)

    conf      = _load_conf(args.conf)
    server    = conf["server"]
    port      = conf["port"]
    users     = conf["users"]
    passwords = conf["passwords"]

    n_accounts = min(len(users), len(passwords), len(_ACCOUNT_LABELS))
    if n_accounts == 0:
        print("No accounts found in config", file=sys.stderr)
        sys.exit(1)

    t0 = time.monotonic()

    subprocess.run(
        ["bash", "-c",
         f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
         f"ssh-keygen -F '[{server}]:{port}' &>/dev/null || "
         f"ssh-keyscan -p {port} -H {server} >> ~/.ssh/known_hosts 2>/dev/null"],
        check=False,
    )

    # One SSH per account, sequentially
    accounts_data = []
    for idx in range(n_accounts):
        print(f"Collecting account {idx+1}/{n_accounts}…", file=sys.stderr)
        data = _collect_account(users[idx], passwords[idx], server, port)
        accounts_data.append(data)

    elapsed = time.monotonic() - t0
    print(f"Collected in {elapsed:.1f}s", file=sys.stderr)

    # Heartbeat rows come from account-1 (the status collector)
    hb_data  = accounts_data[0].get("heartbeats") or {}
    raw_rows = hb_data.get("rows", []) if isinstance(hb_data, dict) else []

    # Map OS usernames to "acct-N" labels (avoids exposing real usernames in HTML)
    user_to_label = {u: lbl.split()[0] for u, lbl in zip(users, _ACCOUNT_LABELS)}
    heartbeats = _classify_heartbeats(raw_rows, user_to_label)

    # Inventory + latest deploys also come from account-1 (the shared state DB)
    def _rows(key: str) -> list:
        blob = accounts_data[0].get(key) or {}
        return blob.get("rows", []) if isinstance(blob, dict) else []
    inventory_rows = _rows("inventory")
    deploy_rows    = _rows("deploys")

    # Windowed PnL is computed on the collector account (it owns the shared state DB).
    pnl_windows = accounts_data[0].get("pnl_windows") or {}

    html = _render_html(
        heartbeats=heartbeats,
        accounts=accounts_data,
        generated_at=datetime.now(tz=timezone.utc),
        collection_s=elapsed,
        inventory=inventory_rows,
        deploys=deploy_rows,
        user_to_label=user_to_label,
        pnl_windows=pnl_windows,
    )

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Written to {args.out}", file=sys.stderr)
    else:
        print(html)


if __name__ == "__main__":
    main()
