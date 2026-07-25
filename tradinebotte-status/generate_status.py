#!/usr/bin/env python3
"""generate_status.py — Generate a full HTML status page for all tradinebotte accounts.

Reads EVERYTHING from the shared state DB (no SSH). The status collector already receives
one heartbeat per bot per interval and persists the full payload, so every value the page
needs — liveness, capital/PnL, accumulation portfolio, grid PnL/bounds flag, version — is
in `/data1/tradinebotte-shared/database/tradinebotte.db`:
  - heartbeats  : per-bot liveness + payload (the single source of truth)
  - inventory   : desired-state topology + display names
  - deploys     : last deploy per bot (expected-vs-actual)
  - pnl_windows : windowed PnL derived from heartbeat history

No per-account SSH: the previous design read each bot's live.db/live_accum.db/grid_state
over SSH at FIXED paths, which broke the moment the single-tree cutover renamed those
files (live_accum.db → live_accumulation.db, ~/tradinebotte-grid/live.db → live_grid.db)
and made the collection slow + flag phantom "service down" issues. Sourcing from the DB
removes that whole fragile layer. The exact grid bounds/cycles and the Polymarket
per-trade tables are the only detail not in the DB; they are omitted (a follow-up can add
them to the heartbeat payload if wanted).

Usage:
  python3 tradinebotte-status/generate_status.py > status.html
  python3 tradinebotte-status/generate_status.py --out /var/www/html/status.html
  python3 tradinebotte-status/generate_status.py --conf ~/.tradinebotte-test.conf

Config: ~/.tradinebotte-test.conf  (same file as bot_status.sh)
  Used only for TEST_USERS (account order + "acct-N" labels); no passwords are needed.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from html import escape

# Pure DB loaders live in status_data (split out of this module); re-exported here so
# `generate_status.<fn>` and the tests keep resolving them, and render/main call them directly.
# Several are pure re-exports (used by the moved code / tests, not this module) — pylint can't
# tell those from the locally-used ones, so unused-import is disabled for the block.
from status_data import (  # noqa: E402  pylint: disable=unused-import
    _fetch_btc_24h, _qdb, _compute_pnl_windows, _downsample, _load_pnl_series,
    _fleet_series, _load_trades, _SERIES_MAX_POINTS, _TRADES_SQL,
)


# ─── Shared-DB data layer (no SSH — the collector already has everything) ─────

SHARED_DB = "/data1/tradinebotte-shared/database/tradinebotte.db"

_HEARTBEAT_SQL = (
    "SELECT account, bot_name, max(ts) as last_ts, status, bounds_ok, version, payload"
    " FROM heartbeats GROUP BY account, bot_name ORDER BY account, bot_name"
)
# Two forms: the full query selects strategy_type; the base one omits it. generate_status
# reads via _qdb (raw SELECT), NOT open_db, so it never runs the strategy_type ALTER — if the
# code ships before sync_inventory has migrated the prod DB, the full SELECT would error and
# _qdb returns [] for that key, which would blank the whole inventory (→ the real-money bot
# rendered as SIM, the is_live=0-for-months failure). So run both and take whichever returns
# rows; the family derivation falls back to bot_type when strategy_type is absent.
_INVENTORY_COLS_BASE = "account, bot_name, display_name, kind, bot_type, is_live"
_INVENTORY_SQL = (
    f"SELECT {_INVENTORY_COLS_BASE}, strategy_type"
    " FROM inventory WHERE enabled=1 ORDER BY account, bot_name"
)
_INVENTORY_SQL_BASE = (
    f"SELECT {_INVENTORY_COLS_BASE}"
    " FROM inventory WHERE enabled=1 ORDER BY account, bot_name"
)
_DEPLOYS_SQL = (
    "SELECT account, bot_name, max(ts) as last_ts, git_hash, result"
    " FROM deploys GROUP BY account, bot_name"
)
# Durable per-fill log (real-money bots push here). Newest first; the renderer caps how many
# rows it shows. Only live bots ever push, so every row belongs on the real-money page.


def _build_accounts_from_db(users: list, heartbeats: list) -> list:
    """One card-data dict per account (aligned with `users`), derived entirely from the
    shared-DB heartbeats — the replacement for the old per-account SSH `_collect_account`.

    `version` is the account's newest bot payload version. `services` is intentionally
    empty: heartbeats are the single liveness source (the per-bot section + family view
    already show it), so the header no longer paints phantom systemd dots (a stopped unit
    goes STALE/DEAD in its heartbeat). The grid/accumulation detail lines are derived in
    `_render_account_card` straight from each bot's payload, so nothing else is needed here.
    """
    by_acct: dict = {}
    for h in heartbeats:
        by_acct.setdefault(h.get("account"), []).append(h)
    out = []
    for u in users:
        rows = by_acct.get(u, [])
        newest = max(rows, key=lambda r: r.get("last_ts", 0), default=None)
        ver = "?"
        if newest:
            ver = ((newest.get("payload") or {}).get("version")
                   or newest.get("version") or "?")
        out.append({"version": ver, "services": [], "live": None,
                    "grids": [], "accum": None, "error": None})
    return out


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

/* ── Real-money (live) page ───────────────────────────── */
.nav-link{font-size:.62em;font-weight:600;color:#d29922;text-decoration:none;
          border:1px solid #d29922;border-radius:6px;padding:2px 8px;margin-right:10px;
          vertical-align:middle}
.nav-link:hover{background:#d29922;color:#0d1117}
.live-panel{margin:14px 0 26px;padding:14px 16px;border:1px solid #30363d;border-radius:10px;
            background:#0d1117}
.live-panel h3{margin:0 0 10px;font-size:1.02em}
.dim{color:#8b949e;font-size:.82em;font-weight:400}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.82em;color:#8b949e;
      max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:inline-block;
      vertical-align:bottom}
.badge.live-b{background:#238636;color:#fff;border-radius:5px;padding:1px 7px;font-size:.9em}
table.trades{width:100%;border-collapse:collapse;margin-top:10px;font-size:.82em}
table.trades th{text-align:left;color:#8b949e;font-weight:600;border-bottom:1px solid #21262d;
                padding:5px 8px;white-space:nowrap}
table.trades td{padding:5px 8px;border-bottom:1px solid #161b22;white-space:nowrap}
table.trades tr:hover td{background:#161b22}
/* Sub-section caption above each of the two live tables (open orders / executed) */
.live-sub{margin:14px 0 2px;font-size:.82em;font-weight:600;color:#c9d1d9;
          text-transform:uppercase;letter-spacing:.8px}
/* An open-orders table sourced from a STALE/DEAD heartbeat: the snapshot may be out of date */
table.trades.stale-tbl{opacity:.5}

/* ── Evolution charts (PnL / asset value over time) ───── */
.tbchart{margin:10px 0;background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:8px 10px}
.tbchart.mini{margin:6px 0 2px;padding:6px 8px}
.tbc-head{display:flex;align-items:center;gap:10px;font-size:.8em;margin-bottom:4px}
.tbc-title{color:#8b949e;font-weight:600}
.tbc-toggle{display:inline-flex;gap:2px}
.tbc-btn{background:#161b22;border:1px solid #30363d;color:#8b949e;border-radius:5px;
         padding:1px 7px;font-size:.9em;cursor:pointer}
.tbc-btn.active{color:#fff}
.tbc-btn.tbc-pnl.active{background:#1f6feb;border-color:#1f6feb}
.tbc-btn.tbc-asset.active{background:#9e6a1b;border-color:#9e6a1b}
.tbc-readout{margin-left:auto;color:#c9d1d9;font-variant-numeric:tabular-nums}
.tbc-readout .t{color:#6e7681;margin-right:6px}
.tbc-plot{position:relative;width:100%}
.tbchart svg{display:block;width:100%;height:110px}
.tbchart.mini svg{height:60px}
.tbc-grid{stroke:#21262d;stroke-width:1}
.tbc-base{stroke:#484f58;stroke-width:1;stroke-dasharray:3 3}
.tbc-line{fill:none;stroke-width:2;vector-effect:non-scaling-stroke}
.tbc-cross{stroke:#6e7681;stroke-width:1;vector-effect:non-scaling-stroke;visibility:hidden}
.tbc-tip{position:absolute;pointer-events:none;background:#161b22;border:1px solid #30363d;
         border-radius:6px;padding:3px 7px;font-size:.74em;color:#c9d1d9;white-space:nowrap;
         transform:translate(-50%,-115%);visibility:hidden;z-index:5;font-variant-numeric:tabular-nums}
.tbc-tip .v{font-weight:600}
.tbc-empty{color:#484f58;font-size:.78em;font-style:italic;padding:16px 4px}
"""

# Evolution-chart client script. A PLAIN string (not the f-string body) so its many braces stay
# literal — the page interpolates it with {_CHART_JS} and the series JSON separately. Hand-rolled,
# zero external deps: one <svg> line + area per chart, PnL⇄Asset toggle, crosshair+tooltip hover,
# and range clipping synced to the page's daily/weekly/monthly/alltime window (setWin body class).
_CHART_JS = r"""
var TBWIN={daily:86400,weekly:604800,monthly:2592000,alltime:1e12};
function tbCurWin(){var m=document.body.className.match(/win-(\w+)/);return TBWIN[m?m[1]:'alltime']||1e12;}
function tbFmt(metric,v){var s=v<0?'-':(metric==='pnl'?'+':'');return s+'$'+Math.abs(v).toFixed(2);}
function tbDate(sec){var d=new Date(sec*1000);function p(n){return(n<10?'0':'')+n;}
  return p(d.getUTCMonth()+1)+'/'+p(d.getUTCDate())+' '+p(d.getUTCHours())+':'+p(d.getUTCMinutes());}
function tbRender(el){
  var s=window.TBSERIES&&TBSERIES[el.getAttribute('data-key')];
  var metric=el.getAttribute('data-metric')||'pnl';
  var plot=el.querySelector('.tbc-plot'),read=el.querySelector('.tbc-readout');
  var empty=el.getAttribute('data-empty')||'—';
  if(!s||!s.ts||!s.ts.length){plot.innerHTML="<div class='tbc-empty'>"+empty+"</div>";if(read)read.textContent='';return;}
  var now=s.ts[s.ts.length-1],cut=now-tbCurWin(),T=[],V=[],col=s[metric]||[];
  for(var i=0;i<s.ts.length;i++){var v=col[i];if(s.ts[i]>=cut&&v!=null){T.push(s.ts[i]);V.push(v);}}
  if(V.length<2){plot.innerHTML="<div class='tbc-empty'>"+empty+"</div>";if(read)read.textContent='';return;}
  var W=600,H=el.classList.contains('mini')?60:110,PX=3,PY=8;
  var t0=T[0],t1=T[T.length-1],vmin=Math.min.apply(null,V),vmax=Math.max.apply(null,V);
  if(metric==='pnl'){vmin=Math.min(vmin,0);vmax=Math.max(vmax,0);}
  var vspan=(vmax-vmin)||1,tspan=(t1-t0)||1,color=metric==='pnl'?'#58a6ff':'#d29922';
  function X(t){return PX+(t-t0)/tspan*(W-2*PX);}
  function Y(v){return PY+(1-(v-vmin)/vspan)*(H-2*PY);}
  var xs=[],d='';
  for(var i=0;i<T.length;i++){var x=X(T[i]);xs.push(x);d+=(i?'L':'M')+x.toFixed(1)+','+Y(V[i]).toFixed(1)+' ';}
  var baseY=Y(metric==='pnl'?0:vmin);
  var area='M'+xs[0].toFixed(1)+','+baseY.toFixed(1)+' '+d.slice(1)+'L'+xs[xs.length-1].toFixed(1)+','+baseY.toFixed(1)+'Z';
  var grid='';for(var g=1;g<3;g++){var gy=PY+g/3*(H-2*PY);grid+="<line class='tbc-grid' x1='0' y1='"+gy.toFixed(1)+"' x2='"+W+"' y2='"+gy.toFixed(1)+"'/>";}
  var base=metric==='pnl'?"<line class='tbc-base' x1='0' y1='"+baseY.toFixed(1)+"' x2='"+W+"' y2='"+baseY.toFixed(1)+"'/>":'';
  var uid='g'+Math.random().toString(36).slice(2,7);
  var svg="<svg viewBox='0 0 "+W+" "+H+"' preserveAspectRatio='none'>"
    +"<defs><linearGradient id='"+uid+"' x1='0' x2='0' y1='0' y2='1'>"
    +"<stop offset='0' stop-color='"+color+"' stop-opacity='0.28'/><stop offset='1' stop-color='"+color+"' stop-opacity='0'/></linearGradient></defs>"
    +grid+base+"<path d='"+area+"' fill='url(#"+uid+")' stroke='none'/>"
    +"<path class='tbc-line' d='"+d+"' stroke='"+color+"'/>"
    +"<line class='tbc-cross' x1='0' y1='0' x2='0' y2='"+H+"'/></svg>"
    +"<div class='tbc-tip'></div>";
  plot.innerHTML=svg;
  if(read)read.innerHTML="<span class='t'>"+(metric==='pnl'?'PnL':'$')+"</span>"+tbFmt(metric,V[V.length-1]);
  var cross=plot.querySelector('.tbc-cross'),tip=plot.querySelector('.tbc-tip'),svgEl=plot.querySelector('svg');
  function move(ev){
    var r=svgEl.getBoundingClientRect(),cx=ev.touches?ev.touches[0].clientX:ev.clientX;
    var vx=(cx-r.left)/r.width*W,best=0,bd=1e9;
    for(var i=0;i<xs.length;i++){var dd=Math.abs(xs[i]-vx);if(dd<bd){bd=dd;best=i;}}
    cross.setAttribute('x1',xs[best]);cross.setAttribute('x2',xs[best]);cross.style.visibility='visible';
    tip.style.visibility='visible';tip.style.left=(xs[best]/W*100)+'%';tip.style.top=(Y(V[best])/H*100)+'%';
    tip.innerHTML="<span class='v'>"+tbFmt(metric,V[best])+"</span> · "+tbDate(T[best]);
  }
  function leave(){cross.style.visibility='hidden';tip.style.visibility='hidden';}
  svgEl.addEventListener('mousemove',move);svgEl.addEventListener('mouseleave',leave);
  svgEl.addEventListener('touchmove',move);svgEl.addEventListener('touchend',leave);
}
function tbToggle(btn,metric){
  var el=btn.closest('.tbchart');el.setAttribute('data-metric',metric);
  var bs=el.querySelectorAll('.tbc-btn');for(var i=0;i<bs.length;i++)bs[i].classList.remove('active');
  btn.classList.add('active');tbRender(el);
}
function tbRenderAll(){var els=document.querySelectorAll('.tbchart');
  for(var i=0;i<els.length;i++){if(els[i].offsetParent!==null)tbRender(els[i]);}}
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
    _family_of = _inv.family_of      # canonical bot_type/bot_name → family key
except Exception:
    _INV_ROWS = []
    def _family_of(bot_type: str = "", bot_name: str = ""):  # fail-soft: no derivation
        return None

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
    # Full UTC date + time for the trade tables — a bare HH:MM:SS is ambiguous across days
    # (the live per-trade log spans weeks).
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _wr(w: int, l: int) -> str:
    total = w + l
    if total == 0:
        return "—"
    pct = 100.0 * w / total
    cls = "win" if pct >= 90 else ("open-t" if pct >= 70 else "loss")
    return f"<span class='{cls}'>{pct:.1f}%</span>"


def _render_payload_summary(family: str, payload: dict, now: int) -> str:
    """Per-bot heartbeat summary, keyed on the canonical family (strategy or infra category),
    NOT the generated bot_id — the id is unique per bot and matches no branch."""
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
    if family in ("polymarket", "grid", "swing"):
        # PnL strategies: cumulative realized PnL is the headline; daily shown alongside.
        # Fall back to daily_pnl for heartbeats from bots predating pnl_total.
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
        ts = payload.get("last_book_ts")
        if ts:
            parts.append(f"{t('k_book')}{_ago(ts)}")
    elif family == "accumulation":
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
    elif family in ("feed", "feed5m", "cex_feed"):
        ws = payload.get("ws_connected")
        if ws is not None:
            parts.append(t("k_ws_on") if ws else t("k_ws_off"))
        mt = payload.get("msgs_total")
        if mt is not None:
            parts.append(f"{t('k_msgs')}{mt}")
        ts = payload.get("last_book_ts")
        if ts:
            parts.append(f"{t('k_book')}{_ago(ts)}")
    elif family == "indicators":
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


def _key_metric(family: str, payload: dict) -> str:
    """Return the single most important display value for a bot heartbeat pill, keyed on the
    canonical family (strategy/infra category), NOT the generated bot_id."""
    if family in ("polymarket", "grid", "swing"):
        pnl = payload.get("daily_pnl")
        if pnl is not None:
            sign = "+" if pnl >= 0 else "-"
            return f"{sign}${abs(pnl):.2f}"
    elif family == "accumulation":
        btc = payload.get("holdings_btc")
        if btc is not None:
            return f"{btc:.4f} BTC"
    return ""


# ─── Bot-family view (primary "by bot, not by account" layout) ───────────────

# Strategy families that carry a comparable cumulative PnL (pnl_total) — these get windowed
# PnL rolled up across instances. accumulation heartbeats holdings/realized (its pnl_total is
# a flat 0), so it is a detail family: its rows show the payload summary, not a misleading $0.
_PNL_STRATEGIES = {"polymarket", "grid", "swing"}
_FAMILY_ORDER = ["polymarket", "grid", "swing", "accumulation",
                 "feed", "feed5m", "cex_feed", "indicators", "status"]
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
        # Group by the canonical family (attached in main()); derive-if-missing keeps a
        # heartbeat with no inventory row grouping sensibly instead of vanishing.
        fam_key = r.get("_family") or _family_of("", r["bot_name"]) or r["bot_name"]
        by_family.setdefault(fam_key, []).append(r)
    ordered = [f for f in _FAMILY_ORDER if f in by_family]
    ordered += sorted(f for f in by_family if f not in _FAMILY_ORDER)

    sections = ""
    for fam in ordered:
        rows = sorted(by_family[fam], key=lambda r: (r.get("_label") or r["account"]))
        is_pnl = fam in _PNL_STRATEGIES
        # Windowed PnL is keyed account|bot_name (per instance) — NOT the family; _sum_windows
        # then rolls the per-instance recs up into the family head.
        recs = [pw.get(f"{r['account']}|{r['bot_name']}", {}) for r in rows]
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
            mode = _mode_badge(acct_short, r["bot_name"])
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
        fam = r.get("_family") or _family_of("", bot) or bot
        age_min = r["age_s"] // 60
        age_str = f"{age_min}min" if age_min < 120 else f"{age_min//60}h{age_min%60:02d}m"
        detail = _render_payload_summary(fam, r.get("payload", {}), now)
        mode_cell = _mode_badge(acct_short, bot)
        km = _key_metric(fam, r.get("payload", {}))
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
            f"<span class='bot-name'>{escape(r.get('_display') or bot)}</span>"
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

    # Determine whether this account runs Polymarket bots (drives the poly-only win-rate +
    # trade tables below). Keyed on the canonical family, not the generated bot_id.
    has_poly = any(r.get("_family") == "polymarket" for r in (hb_rows or []))

    live = data.get("live")
    stats_html = ""
    trade_html = ""
    open_html  = ""

    # Big metrics (Capital, Today PnL) come from the primary bot's heartbeat payload —
    # the single source of truth shared with the pills and the fleet headline. live.db
    # is used only for the Polymarket win-rate and the trade tables further below.
    primary = None
    for _fam in ("polymarket", "grid", "swing"):
        primary = next((r for r in (hb_rows or []) if r.get("_family") == _fam), None)
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

    # Grid bots: surface the lifetime grid PnL from the heartbeat payload. The exact grid
    # bounds / cycles / level fills used to be read from grid_state over SSH — they are not
    # in the shared DB, so they are omitted here (bounds health still shows per-bot in the
    # section below, and windowed PnL in the family view). Match on the bot_id family tag.
    grid_html = ""
    for r in (hb_rows or []):
        if "grid" not in (r.get("bot_name") or "").lower():
            continue
        p = r.get("payload") or {}
        profit = p.get("pnl_total")
        if profit is None:
            continue
        disp = escape(r.get("_display") or r.get("bot_name") or "grid")
        profit_cls = "pnl-pos" if (profit or 0) >= 0 else "pnl-neg"
        grid_html += (
            f"<div style='margin-top:4px;font-size:.8em'>"
            f"<span style='color:#8b949e;text-transform:uppercase;letter-spacing:.8px;"
            f"font-size:.85em'>{escape(t('grid_label'))}</span>"
            f" <span style='color:#58a6ff'>{disp}</span>"
            f" · <span class='{profit_cls}'>{_fmt_pnl(profit)}</span>"
            f"</div>"
        )

    # Accumulation bots: portfolio (holdings / free / avg entry / trade count) straight
    # from the heartbeat payload — the single source of truth (was read from live_accum.db
    # over SSH, which broke on the single-tree rename to live_accumulation.db).
    cex_html = ""
    for r in (hb_rows or []):
        if "accumulation" not in (r.get("bot_name") or "").lower():
            continue
        p = r.get("payload") or {}
        hold = p.get("holdings_btc")
        usdt = p.get("free_usdt")
        avg  = p.get("avg_entry")
        tot  = p.get("trades_total")
        if hold is None and usdt is None:
            continue
        disp = escape(r.get("_display") or r.get("bot_name") or "accum")
        hold_str = f"{hold:.6f}" if isinstance(hold, (int, float)) else "—"
        usdt_str = f" · {t('accum_free', u=f'{usdt:.0f}')}" if isinstance(usdt, (int, float)) else ""
        avg_str  = f" · {t('accum_avg', a=f'{avg:g}')}" if isinstance(avg, (int, float)) else ""
        tot_str  = f" · {tot}T" if tot is not None else ""
        cex_html += (
            f"<div style='margin-top:4px;font-size:.8em'>"
            f"<span style='color:#8b949e;text-transform:uppercase;letter-spacing:.8px;"
            f"font-size:.85em'>{disp}</span>"
            f" — <span style='color:#58a6ff'>{hold_str}</span>{usdt_str}{avg_str}{tot_str}"
            f"</div>"
        )

    # Per-bot evolution charts (one per trading bot on this account; infra rows have no
    # pnl_total and are skipped so they don't render an empty chart).
    charts_html = ""
    for r in (hb_rows or []):
        if (r.get("payload") or {}).get("pnl_total") is None:
            continue
        charts_html += _chart_html(f"{r.get('account')}|{r.get('bot_name')}",
                                   r.get("_display") or r.get("bot_name") or "bot",
                                   t("chart_no_data"), mini=True)

    now = int(time.time())
    bot_section = _render_bot_section(hb_rows or [], now)

    return (
        f"<div class='account'>"
        f"{header}{stats_html}{open_html}{trade_html}{grid_html}{cex_html}{charts_html}{bot_section}"
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
        bot_disp = inv.get("display_name") or bot   # readable label; bot_name stays the join key
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
            f"<tr><td>{escape(label)}</td><td>{escape(bot_disp)}</td>"
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


_TRADE_ROW_CAP = 100   # most recent fills shown per bot (full history stays in bot_trades)


def _fmt_age(age_s) -> str:
    """Compact age string ("12min", "3h07m", "2d") for a heartbeat's freshness."""
    try:
        a = int(age_s)
    except (TypeError, ValueError):
        return "—"
    m = a // 60
    if m < 120:
        return f"{m}min"
    if m < 2880:
        return f"{m // 60}h{m % 60:02d}m"
    return f"{a // 86400}d"


def _render_open_orders_table(open_orders, hb: dict) -> str:
    """The "orders in flight" table for one live bot — the resting book the bot currently holds
    on the exchange (from the heartbeat payload's `open_orders`), kept SEPARATE from the executed
    fills because its truth semantics differ: fills are immutable history, open orders are a
    snapshot that decays. So the table is stamped "as of <ts> (age)" and dimmed when the source
    heartbeat is not ALIVE (the orders may have filled/canceled since).

    `open_orders is None` → the bot predates this field (or shadow) → "awaiting next heartbeat",
    NOT "zero orders". `[]` → the bot reports it holds no resting order."""
    flag  = hb.get("flag", "")
    stale = bool(flag) and flag != "ALIVE"
    asof  = t("oo_asof", ts=_fmt_ts(hb.get("last_ts")), age=_fmt_age(hb.get("age_s")))
    flag_badge = (f" <span class='badge {flag.lower()}'>{_flag_label(flag)}</span>"
                  if stale else "")
    caption = f"<div class='live-sub'>{escape(t('oo_title'))} <span class='dim'>{escape(asof)}</span>{flag_badge}</div>"

    if open_orders is None:
        return caption + f"<div class='dim'>{escape(t('oo_unknown'))}</div>"
    if not open_orders:
        return caption + f"<div class='dim'>{escape(t('oo_none'))}</div>"

    head = (f"<tr><th>{escape(t('tt_side'))}</th><th>{escape(t('tt_price'))}</th>"
            f"<th>{escape(t('tt_qty'))}</th><th>{escape(t('oo_notional'))}</th>"
            f"<th>{escape(t('oo_placed'))}</th><th>{escape(t('tt_order'))}</th></tr>")
    body = []
    for o in open_orders[:_TRADE_ROW_CAP]:
        side = o.get("side") or ""
        side_cls = "pnl-pos" if side == "sell" else "pnl-neg"
        oid = o.get("order_id") or "—"
        body.append(
            f"<tr><td class='{side_cls}'>{escape(side)}</td>"
            f"<td>{float(o.get('price') or 0):.6f}</td>"
            f"<td>{float(o.get('qty') or 0):.2f}</td>"
            f"<td>${float(o.get('notional') or 0):.4f}</td>"
            f"<td>{_fmt_ts(o.get('placed_ts'))}</td>"
            f"<td class='mono' title='{escape(str(oid))}'>{escape(str(oid))}</td></tr>")
    dim_cls = " stale-tbl" if stale else ""
    return caption + f"<table class='trades{dim_cls}'>{head}{''.join(body)}</table>"


def _render_live_trades(live_heartbeats: list, trades_by_bot: dict) -> str:
    """The real-money page's rich section: per live bot, a stats strip (holdings, avg entry,
    realized PnL, fill/volume/fee tallies from the durable trade log) + TWO separate tables — the
    resting orders currently in flight (from the heartbeat payload) and the executed-fill log
    (order IDs included — the live page shows full detail). Reuses _fmt_pnl / _fmt_ts."""
    panels = []
    for hb in live_heartbeats:
        acct, bot = hb.get("account"), hb.get("bot_name")
        pl = hb.get("payload") or {}
        name = escape(hb.get("_display") or bot or "?")
        trades = trades_by_bot.get((acct, bot), [])

        buys  = [x for x in trades if x.get("side") == "buy"]
        sells = [x for x in trades if x.get("side") == "sell"]
        bought_qty = sum(float(x.get("qty") or 0) for x in buys)
        sold_qty   = sum(float(x.get("qty") or 0) for x in sells)
        fees_total = sum(float(x.get("fee") or 0) for x in trades)
        # None (key absent → old bot / shadow) is deliberately kept distinct from [] (bot
        # reports zero resting orders) — _render_open_orders_table renders them differently.
        open_orders = pl.get("open_orders")
        n_open = len(open_orders) if isinstance(open_orders, list) else 0

        def _stat(lbl, val):
            return (f"<div class='sb-item'><div class='lbl'>{escape(lbl)}</div>"
                    f"<div class='val'>{val}</div></div>")

        stats = (
            "<div class='summary-bar'>"
            + _stat(t("lt_mode"), f"<span class='badge live-b'>{escape(str(pl.get('mode', '?')).upper())}</span>")
            + _stat(t("lt_holdings"), f"{float(pl.get('holdings_btc') or 0):.4f}")
            + _stat(t("lt_avg_entry"), f"{float(pl.get('avg_entry') or 0):.6f}")
            + _stat(t("lt_free"), f"${float(pl.get('free_usdt') or 0):.2f}")
            + _stat(t("lt_realized"), _fmt_pnl(pl.get("pnl_total")))
            + _stat(t("lt_open"), f"{n_open}")
            + _stat(t("lt_fills"), f"{len(trades)} <span class='dim'>({len(buys)}B/{len(sells)}S)</span>")
            + _stat(t("lt_bought"), f"{bought_qty:.2f}")
            + _stat(t("lt_sold"), f"{sold_qty:.2f}")
            + _stat(t("lt_fees"), f"${fees_total:.4f}")
            + "</div>"
        )

        open_table = _render_open_orders_table(open_orders, hb)

        exec_caption = f"<div class='live-sub'>{escape(t('lt_executed_title'))}</div>"
        if trades:
            head = (f"<tr><th>{escape(t('tt_time'))}</th><th>{escape(t('tt_side'))}</th>"
                    f"<th>{escape(t('tt_reason'))}</th><th>{escape(t('tt_price'))}</th>"
                    f"<th>{escape(t('tt_qty'))}</th><th>{escape(t('tt_quote'))}</th>"
                    f"<th>{escape(t('tt_fee'))}</th><th>{escape(t('tt_order'))}</th></tr>")
            body = []
            for x in trades[:_TRADE_ROW_CAP]:
                side = x.get("side") or ""
                side_cls = "pnl-pos" if side == "sell" else "pnl-neg"
                oid = x.get("order_id") or "—"
                body.append(
                    f"<tr><td>{_fmt_ts((x.get('ts_ms') or 0) / 1000)}</td>"
                    f"<td class='{side_cls}'>{escape(side)}</td>"
                    f"<td>{escape(str(x.get('reason') or ''))}</td>"
                    f"<td>{float(x.get('price') or 0):.6f}</td>"
                    f"<td>{float(x.get('qty') or 0):.2f}</td>"
                    f"<td>${float(x.get('quote') or 0):.4f}</td>"
                    f"<td>${float(x.get('fee') or 0):.4f}</td>"
                    f"<td class='mono' title='{escape(str(oid))}'>{escape(str(oid))}</td></tr>")
            cap_note = ("" if len(trades) <= _TRADE_ROW_CAP else
                        f"<div class='dim'>{escape(t('lt_capped', n=_TRADE_ROW_CAP, total=len(trades)))}</div>")
            exec_table = (f"{exec_caption}<table class='trades'>{head}{''.join(body)}</table>{cap_note}")
        else:
            exec_table = f"{exec_caption}<div class='dim'>{escape(t('lt_no_trades'))}</div>"

        chart = _chart_html(f"{acct}|{bot}", t("chart_bot"), t("chart_no_data"), mini=True)
        panels.append(
            f"<div class='live-panel'><h3>{name} "
            f"<span class='dim'>{escape(acct or '')}</span></h3>{stats}{chart}"
            f"{open_table}{exec_table}</div>"
        )

    if not panels:
        return f"<div class='dim'>{escape(t('lt_none'))}</div>"
    return "".join(panels)


def _chart_html(key: str, title: str, empty_label: str, mini: bool = False) -> str:
    """Container for one evolution chart; the client JS (tbRender) fills .tbc-plot from
    TBSERIES[key]. Defaults to the PnL metric with an Asset toggle."""
    cls = "tbchart mini" if mini else "tbchart"
    return (
        f"<div class='{cls}' data-key='{escape(key)}' data-metric='pnl' "
        f"data-empty='{escape(empty_label)}'>"
        f"<div class='tbc-head'><span class='tbc-title'>{escape(title)}</span>"
        f"<span class='tbc-toggle'>"
        f"<button class='tbc-btn tbc-pnl active' onclick=\"tbToggle(this,'pnl')\">{escape(t('chart_pnl'))}</button>"
        f"<button class='tbc-btn tbc-asset' onclick=\"tbToggle(this,'asset')\">{escape(t('chart_asset'))}</button>"
        f"</span><span class='tbc-readout'></span></div>"
        f"<div class='tbc-plot'></div></div>"
    )


def _render_html(
    heartbeats: list,
    accounts: list,
    generated_at: datetime,
    collection_s: float,
    inventory: list | None = None,
    deploys: list | None = None,
    user_to_label: dict | None = None,
    pnl_windows: dict | None = None,
    scope: str = "overview",
    trades_by_bot: dict | None = None,
    nav_href: str = "",
    audit_heartbeats: list | None = None,
    series: dict | None = None,
) -> str:
    # The expected-vs-actual audit is fleet-wide: it must see EVERY bot's heartbeat, not the
    # scope-filtered subset, or a live bot (filtered off the overview) would falsely read as
    # "expected but silent". Falls back to the page's own heartbeats when not supplied.
    audit_heartbeats = audit_heartbeats if audit_heartbeats is not None else heartbeats
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
        _scope_label = t("scope_live") if scope == "live" else t("scope_sim")
        titles[lang] = f"{t('title')} — {_scope_label}"
        # Cross-link to the other page, labelled with the OTHER scope (translated per-language).
        nav_html = ""
        if nav_href:
            _other = t("scope_sim") if scope == "live" else t("scope_live")
            nav_html = f"<a class='nav-link' href='{escape(nav_href)}'>{escape(_other)} →</a>"

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

        banners_html = _render_banners(accounts, heartbeats)

        # Real-money page: the rich per-trade section IS the body — the family roll-up,
        # expected-vs-actual and per-account cards are the overview page's job, so they are
        # skipped here (and skipping them sidesteps the account-card positional alignment).
        if scope == "live":
            mid_html = (f"<h2>{escape(t('h_live_trades'))}</h2>"
                        + _render_live_trades(heartbeats, trades_by_bot or {}))
        else:
            families_html = _render_bot_families(heartbeats, pnl_windows, now)
            cards_html = "".join(_render_account_card(label, data, _acct_hb(label))
                                 for label, data in zip(_ACCOUNT_LABELS, accounts))
            expected_html = _render_expected_actual(
                inventory or [], deploys or [], audit_heartbeats, user_to_label or {})
            mid_html = (f"<h2>{escape(t('h_bots_family'))}</h2>{families_html}"
                        f"{expected_html}"
                        f"<h2>{escape(t('h_accounts'))}</h2>"
                        f"<div class='accounts'>{cards_html}</div>")

        # The refresh countdown number is a live span JS updates; inject it as the {n} of
        # the (already-translated) "refresh in {n}s" phrase, so word order stays correct.
        _rf_span = "<span class='rf-ct'>60</span>"
        footer = (
            f"<div class='footer'>{escape(t('footer_generated', ts=ts_str))} · "
            f"{escape(t('footer_collection', s=f'{collection_s:.1f}'))} · "
            f"{t('footer_refresh', n=_rf_span)}</div>"
        )

        # Fleet-level evolution chart (this page's scope) right under the summary bar.
        fleet_chart = _chart_html("__fleet__", t("chart_fleet"), t("chart_no_data"))

        body_inner = (
            f"<h1><span class='dot {dot_cls}'></span>"
            f" {escape('tradinebotte')} · {escape(_scope_label)} — {escape(status_text)}"
            f"<span class='h1-right'>{nav_html}{btc_price_html}"
            f"<span style='font-size:.6em;color:#8b949e;font-weight:400'>{ts_str}</span>"
            f"{lang_sel}</span></h1>"
            f"{banners_html}{win_toggle}{summary_bar}"
            f"{fleet_chart}"
            f"{mid_html}"
            f"{footer}"
        )
        langboxes.append(f"<div class='langbox i18-{lang}'>{body_inner}</div>")

    _CUR_LANG = _saved_lang
    default_lang = langs[0]
    series_json = json.dumps(series or {}, separators=(",", ":"))
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
<script>window.TBSERIES={series_json};</script>
<script>
{_CHART_JS}
</script>
<script>
function setWin(w){{
  var b=document.body,c=b.className.replace(/\\bwin-\\w+\\b/g,'').trim();
  b.className=(c?c+' ':'')+'win-'+w;
  try{{localStorage.setItem('tbwin',w);}}catch(e){{}}
  if(window.tbRenderAll)tbRenderAll();
}}
function setLang(l){{
  var b=document.body,c=b.className.replace(/\\blang-\\w+\\b/g,'').trim();
  b.className=(c?c+' ':'')+'lang-'+l;
  try{{localStorage.setItem('tblang',l);}}catch(e){{}}
  var tt=b.getAttribute('data-title-'+l);if(tt)document.title=tt;
  if(window.tbRenderAll)tbRenderAll();
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
    parser.add_argument(
        "--scope",
        choices=("overview", "live", "both"),
        default="both",
        help=("Which page(s) to emit: 'overview' = simulation bots + infra + fleet health "
              "(the current URL); 'live' = real-money bots only, with per-trade detail "
              "(written next to --out as *-live.html); 'both' (default) writes both."),
    )
    args = parser.parse_args()

    if args.lang:
        global _CUR_LANG
        _CUR_LANG = args.lang

    if not os.path.exists(args.conf):
        print(f"Config not found: {args.conf}", file=sys.stderr)
        sys.exit(1)

    conf      = _load_conf(args.conf)
    users     = conf["users"]
    passwords = conf["passwords"]   # kept only for the len()-lockstep account filter below

    # Render only accounts that host a bot in inventory. A TEST_USERS entry with no
    # inventory row (the ephemeral clean-install test account) must not appear as an empty
    # card on the production page. Filter users/passwords/labels in lockstep so the
    # renderer's positional alignment holds; acct-N stays = idx+1 (heartbeats/live_bots
    # are keyed by it), so an excluded index just leaves a numbering gap (no acct-7).
    global _ACCOUNT_LABELS
    _keep = [i for i in (_inv.active_account_idxs(_INV_ROWS) if _INV_ROWS else [])
             if i < len(users) and i < len(passwords) and i < len(_ACCOUNT_LABELS)]
    if _keep:
        users           = [users[i] for i in _keep]
        passwords       = [passwords[i] for i in _keep]
        _ACCOUNT_LABELS = [_ACCOUNT_LABELS[i] for i in _keep]

    n_accounts = min(len(users), len(passwords), len(_ACCOUNT_LABELS))
    if n_accounts == 0:
        print("No accounts found in config", file=sys.stderr)
        sys.exit(1)

    t0 = time.monotonic()

    # Everything comes from the shared state DB — one local read, no SSH per account.
    hb_blob  = _qdb(SHARED_DB, {"rows": _HEARTBEAT_SQL}) or {}
    raw_rows = hb_blob.get("rows", [])
    inv_blob = _qdb(SHARED_DB, {"rows": _INVENTORY_SQL, "base": _INVENTORY_SQL_BASE}) or {}
    dep_blob = _qdb(SHARED_DB, {"rows": _DEPLOYS_SQL}) or {}
    # Prefer the strategy_type form; fall back to the base one if that column isn't migrated
    # yet (see _INVENTORY_SQL) so the inventory is never silently blanked.
    inventory_rows = inv_blob.get("rows") or inv_blob.get("base") or []
    deploy_rows    = dep_blob.get("rows", [])

    # Map OS usernames to "acct-N" labels (avoids exposing real usernames in HTML)
    user_to_label = {u: lbl.split()[0] for u, lbl in zip(users, _ACCOUNT_LABELS)}
    # Fleet rollup counts only inventory accounts. The ephemeral standalone test account
    # (resolved from TEST_STANDALONE_USER_IDX) is excluded from the rendered cards but its
    # bots still heartbeat into the shared DB; left unfiltered, their DEAD test rows inflate
    # the issue count / fleet PnL / family view with problems no card ever shows. Drop any
    # heartbeat from a non-kept account.
    _kept_accounts = set(user_to_label)
    raw_rows = [r for r in raw_rows if r.get("account") in _kept_accounts]
    # Same guard on deploys (feeds the expected-vs-actual view): a test-account deploy row
    # must never leak a non-inventory account into the page.
    deploy_rows = [r for r in deploy_rows if r.get("account") in _kept_accounts]
    heartbeats = _classify_heartbeats(raw_rows, user_to_label)

    # Attach the readable display_name AND the canonical family (from inventory) onto each
    # heartbeat so the account cards show the name (not the raw bot_id) and the family view
    # groups by strategy. bot_name stays the join key. _family precedence: the inventory
    # strategy_type column (authoritative) → derived from bot_type → derived from bot_name →
    # the raw bot_name (never vanish). Same fail-soft path when a heartbeat has no inventory row.
    _inv_by_key = {(r.get("account"), r.get("bot_name")): r for r in inventory_rows}
    for _h in heartbeats:
        _invrow = _inv_by_key.get((_h.get("account"), _h.get("bot_name"))) or {}
        _h["_display"] = _invrow.get("display_name")
        _h["_family"] = (_invrow.get("strategy_type")
                         or _family_of(_invrow.get("bot_type", ""), _h.get("bot_name", ""))
                         or _h.get("bot_name"))

    # Per-account card data, derived from the heartbeats (was: one SSH per account).
    accounts_data = _build_accounts_from_db(users, heartbeats)
    # If the shared DB is unreadable there are no heartbeats — surface it as "collector
    # down" (banner reads accounts[0].error) rather than a falsely-green empty page.
    if not raw_rows and accounts_data:
        accounts_data[0]["error"] = "unreachable"

    pnl_windows = _compute_pnl_windows(SHARED_DB)

    elapsed = time.monotonic() - t0
    print(f"Collected from shared DB in {elapsed:.2f}s", file=sys.stderr)

    # Split key = inventory.is_live: real-money bots to the live page, everything else
    # (sim bots + infra) to the overview page. Trades come from the durable bot_trades log.
    live_keys   = {(r.get("account"), r.get("bot_name"))
                   for r in inventory_rows if r.get("is_live") == 1}
    live_hb     = [h for h in heartbeats if (h.get("account"), h.get("bot_name")) in live_keys]
    overview_hb = [h for h in heartbeats if (h.get("account"), h.get("bot_name")) not in live_keys]
    trades_by_bot = _load_trades(SHARED_DB, live_keys)
    _now = datetime.now(tz=timezone.utc)

    # Scope the windowed PnL to each page's bots (keys are "account|bot_name"). Without this the
    # real-money page's summary bar would aggregate the WHOLE fleet (sim bots included) — e.g. a
    # meaningless alltime of the paper bots' PnL, not the live bot's real result.
    live_pnl_windows     = {k: v for k, v in pnl_windows.items()
                            if tuple(k.split("|", 1)) in live_keys}
    overview_pnl_windows = {k: v for k, v in pnl_windows.items()
                            if tuple(k.split("|", 1)) not in live_keys}

    # Time-series for the evolution charts, scoped per page + a fleet aggregate ("__fleet__").
    all_series = _load_pnl_series(SHARED_DB)

    def _scoped_series(want_live: bool) -> dict:
        sub = {k: v for k, v in all_series.items()
               if (tuple(k.split("|", 1)) in live_keys) == want_live}
        sub["__fleet__"] = _fleet_series(sub)
        return sub

    live_series, overview_series = _scoped_series(True), _scoped_series(False)

    def _render(scope_name, hb_subset, nav_href):
        return _render_html(
            heartbeats=hb_subset,
            accounts=accounts_data,
            generated_at=_now,
            collection_s=elapsed,
            inventory=inventory_rows,
            deploys=deploy_rows,
            user_to_label=user_to_label,
            pnl_windows=(live_pnl_windows if scope_name == "live" else overview_pnl_windows),
            scope=scope_name,
            trades_by_bot=trades_by_bot,
            series=(live_series if scope_name == "live" else overview_series),
            nav_href=nav_href,
            audit_heartbeats=heartbeats,
        )

    def _live_path(p):   # …/x.html → …/x-live.html
        base, ext = os.path.splitext(p)
        return f"{base}-live{ext or '.html'}"

    if not args.out:
        # stdout: single page (default the overview; 'live' if explicitly asked).
        _sc = "live" if args.scope == "live" else "overview"
        print(_render(_sc, live_hb if _sc == "live" else overview_hb, ""))
        return

    ov_path, lv_path = args.out, _live_path(args.out)
    ov_base, lv_base = os.path.basename(ov_path), os.path.basename(lv_path)
    outputs = []
    if args.scope in ("overview", "both"):
        outputs.append((ov_path, _render("overview", overview_hb, lv_base)))
    if args.scope in ("live", "both"):
        outputs.append((lv_path, _render("live", live_hb, ov_base)))
    for path, html in outputs:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Written to {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
