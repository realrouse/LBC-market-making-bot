#!/usr/bin/env python3
"""generate_status.py — Generate a full HTML status page for all tradinebotte accounts.

Collects data via one sequential SSH per account (6 total):
  - systemd service states + version stamp (all accounts)
  - live.db trade stats (accounts 1, 2, 3, 4)
  - live_ob.db orderbook stats (account 4)
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
import sqlite3, json, os, time, subprocess, sys

now = int(time.time())
today_start_ms = (now - (now % 86400)) * 1000
week_start_ms  = (now - 7 * 86400) * 1000
month_start_ms = (now - 30 * 86400) * 1000
data = {"version": "?", "services": [], "live": None, "ob": None, "accum": None,
        "heartbeats": None}

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


# live.db: Polymarket bot — uses entry_ts_ms (ms), outcome, pnl_net, capital_after
data["live"] = _qdb("~/tradinebotte/live.db", {
    "totals": (
        "SELECT count(*) t,"
        " sum(CASE WHEN outcome='WIN'  THEN 1 ELSE 0 END) w,"
        " sum(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END) l,"
        " sum(CASE WHEN resolved=0    THEN 1 ELSE 0 END) o"
        " FROM trades"
    ),
    "capital": (
        "SELECT capital_after capital FROM trades WHERE resolved=1"
        " ORDER BY id DESC LIMIT 1"
    ),
    "today": (
        f"SELECT sum(pnl_net) p, count(*) t FROM trades"
        f" WHERE outcome IN ('WIN','LOSS') AND entry_ts_ms >= {today_start_ms}"
    ),
    "week": (
        f"SELECT sum(pnl_net) p, count(*) t FROM trades"
        f" WHERE outcome IN ('WIN','LOSS') AND entry_ts_ms >= {week_start_ms}"
    ),
    "month": (
        f"SELECT sum(pnl_net) p, count(*) t FROM trades"
        f" WHERE outcome IN ('WIN','LOSS') AND entry_ts_ms >= {month_start_ms}"
    ),
    "lifetime": (
        "SELECT sum(pnl_net) p, count(*) t FROM trades"
        " WHERE outcome IN ('WIN','LOSS')"
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
})

# live_ob.db: orderbook CEX bot — table ob_trades, exit_ts_ms NULL = open
data["ob"] = _qdb("~/tradinebotte/live_ob.db", {
    "totals": (
        "SELECT count(*) t,"
        " sum(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END) w,"
        " sum(CASE WHEN pnl_net <= 0 THEN 1 ELSE 0 END) l"
        " FROM ob_trades WHERE exit_ts_ms IS NOT NULL"
    ),
    "recent": (
        "SELECT id, entry_ts_ms/1000 entry_ts, direction,"
        " CASE WHEN pnl_net > 0 THEN 'WIN' ELSE 'LOSS' END result,"
        " entry_price, pnl_net pnl, capital_after capital"
        " FROM ob_trades WHERE exit_ts_ms IS NOT NULL ORDER BY id DESC LIMIT 5"
    ),
})

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
        return {"version": "?", "services": [], "live": None, "ob": None, "accum": None,
                "heartbeats": None, "error": "unreachable"}
    try:
        return json.loads(stdout.strip())
    except json.JSONDecodeError:
        return {"version": "?", "services": [], "live": None, "ob": None, "accum": None,
                "heartbeats": None, "error": "parse_error"}


# ─── Heartbeat classification ────────────────────────────────────────────────

_STALE_AFTER = int(os.environ.get("HEARTBEAT_STALE_S", 7200))
_DEAD_AFTER  = int(os.environ.get("HEARTBEAT_DEAD_S",  14400))


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
  padding:12px 16px;min-width:260px;max-width:480px;
  font-size:.93em;color:#c9d1d9;line-height:1.9;white-space:normal;
  box-shadow:0 8px 28px rgba(0,0,0,.65);
  transition:opacity .13s ease,visibility .13s ease}
.tt:hover .tip{visibility:visible;opacity:1}
.tt .tip.tip-up{top:auto;bottom:calc(100% + 5px)}
.tip-label{color:#8b949e;font-size:.88em;margin-bottom:5px;display:block}
.tip-row{color:#c9d1d9;margin:2px 0}
.tip-dim{color:#484f58;margin-top:6px;font-size:.86em}

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
.btc-price{font-size:.78em;color:#8b949e;margin-left:auto;display:flex;
            align-items:center;gap:6px;white-space:nowrap}
.btc-price .price{color:#e6a817;font-weight:700;font-size:1.05em}
.btc-price .chg.up{color:#3fb950}.btc-price .chg.dn{color:#f85149}

/* ── Open trade badges ────────────────────────────────── */
.open-trades{margin:4px 0;font-size:.8em}

/* ── Footer ───────────────────────────────────────────── */
.footer{margin-top:28px;font-size:.72em;color:#484f58;border-top:1px solid #21262d;
         padding-top:10px}
.no-data{color:#484f58;font-style:italic;font-size:.82em;padding:4px 0}
"""

_ACCOUNT_LABELS = [
    "acct-1 [poly+cex+status]",
    "acct-2 [poly]",
    "acct-3 [poly+accum]",
    "acct-4 [poly+ob+accum]",
    "acct-5 [swing]",
    "acct-6 [grid-mexc-sim]",
]

# Bots running with REAL money — all others default to SIM.
# Key: (acct_short_label, bot_name) — acct_short = first word of _ACCOUNT_LABELS entry.
# Add an entry here when a bot receives real credentials on the remote.
_LIVE_BOTS: set[tuple[str, str]] = set()


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
            return "never"
        age = now - int(ts)
        if age < 120:
            return f"{age}s ago"
        return f"{age // 60}min ago"

    parts = []
    if bot_name in ("live_bot", "grid_bot", "swing_bot"):
        pnl = payload.get("daily_pnl")
        if pnl is not None:
            parts.append(f"pnl=${pnl:+.2f}")
        cap = payload.get("capital")
        if cap is not None:
            parts.append(f"cap=${cap:.0f}")
        ot = payload.get("open_trades")
        if ot is not None:
            parts.append(f"trades={ot}")
        ts = payload.get("last_book_ts")
        if ts:
            parts.append(f"book={_ago(ts)}")
    elif bot_name == "account_bot":
        pnl = payload.get("daily_pnl")
        if pnl is not None:
            parts.append(f"pnl=${pnl:+.2f}")
        ot = payload.get("open_trades")
        if ot is not None:
            parts.append(f"trades={ot}")
        ts = payload.get("last_feed_msg_ts")
        if ts:
            parts.append(f"feed={_ago(ts)}")
    elif bot_name == "accumulation_bot":
        h = payload.get("holdings_btc")
        if h is not None:
            parts.append(f"btc={h:.4f}")
        u = payload.get("free_usdt")
        if u is not None:
            parts.append(f"usdt=${u:.0f}")
        e = payload.get("avg_entry")
        if e is not None:
            parts.append(f"entry={e:.0f}")
        r = payload.get("total_realized")
        if r is not None:
            parts.append(f"pnl=${r:+.2f}")
    elif bot_name == "orderbook_bot":
        op = payload.get("open_positions")
        if op is not None:
            parts.append(f"pos={op}")
        tp = payload.get("total_pnl")
        if tp is not None:
            parts.append(f"pnl=${tp:+.2f}")
        lp = payload.get("last_price")
        if lp:
            parts.append(f"${lp:,.0f}")
    elif bot_name == "feed":
        ws = payload.get("ws_connected")
        if ws is not None:
            parts.append("ws=✓" if ws else "ws=✗")
        mt = payload.get("msgs_total")
        if mt is not None:
            parts.append(f"msgs={mt}")
        ts = payload.get("last_book_ts")
        if ts:
            parts.append(f"book={_ago(ts)}")
    elif bot_name == "indicators":
        ts = payload.get("last_pub_ts")
        if ts:
            parts.append(f"pub={_ago(ts)}")

    return " · ".join(parts) if parts else "—"


def _mode_badge(acct_short: str, bot_name: str) -> str:
    if (acct_short, bot_name) in _LIVE_BOTS:
        return "<span class='badge live-mode'>LIVE</span>"
    return "<span class='badge sim'>SIM</span>"


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
        tp = payload.get("total_pnl")
        if tp is not None:
            sign = "+" if tp >= 0 else "-"
            return f"{sign}${abs(tp):.2f}"
    return ""


def _render_heartbeat_pills(hb_rows: list) -> str:
    """Compact pill-grid: status+mode+name always visible; full details on hover."""
    if not hb_rows:
        return "<p class='no-data'>No heartbeat data available</p>"
    now = int(time.time())
    pills = ""
    for r in hb_rows:
        flag = r["flag"].lower()
        acct_label = r.get("_label") or r["account"]
        acct_short = acct_label.split()[0]
        bot = r["bot_name"]
        age_min = r["age_s"] // 60
        age_str = f"{age_min}min" if age_min < 120 else f"{age_min//60}h{age_min%60:02d}m"
        detail = _render_payload_summary(bot, r.get("payload", {}), now)
        km = _key_metric(bot, r.get("payload", {}))
        bounds_ok = r["bounds_ok"]
        bounds_warn = bounds_ok not in ("ok", "-", "")
        mode = _mode_badge(acct_short, bot)
        km_cls = ""
        if km.startswith("+"):
            km_cls = " pnl-pos"
        elif km.startswith("-"):
            km_cls = " pnl-neg"
        km_html = (
            f"<span class='pill-metric{km_cls}'>{escape(km)}</span>" if km else ""
        )
        tip_content = (
            f"<span class='tip-label'>{escape(acct_short)} · {escape(bot)}</span>"
            f"<div class='tip-row'>{escape(detail)}</div>"
            f"<div class='tip-dim'>"
            f"age {age_str}"
            + (f" · <span style='color:#f85149'>bounds {escape(bounds_ok)}</span>" if bounds_warn
               else f" · bounds {escape(bounds_ok)}")
            + f" · v={escape(r['version'])}"
            f"</div>"
        )
        pills += (
            f"<div class='bot-pill tt'>"
            f"<span class='badge {flag}' style='font-size:.63em'>{r['flag']}</span>"
            f"{mode}"
            f"<span class='pill-name'>{escape(bot)}</span>"
            f"<span class='pill-acct'>{escape(acct_short)}</span>"
            f"{km_html}"
            f"<div class='tip'>{tip_content}</div>"
            f"</div>"
        )
    return f"<div class='hb-pills'>{pills}</div>"


def _render_services(services: list) -> str:
    if not services:
        return ""
    items = "".join(
        f"<span class='svc {'ok' if s['active'] else 'bad'}'>{escape(s['unit'])}</span>"
        for s in services
    )
    return f"<div class='svc-list'>{items}</div>"


def _render_trade_table(rows: list, db_type: str = "live") -> str:
    if not rows:
        return "<p class='no-data'>No trades</p>"
    head_extra = "<th>Market</th>" if db_type == "live" else ""
    html = (
        "<table class='tr-table'><thead><tr>"
        "<th>#</th><th>Time</th><th>Dir</th><th>Result</th>"
        f"<th>Entry</th><th>PnL</th><th>Capital</th>{head_extra}"
        "</tr></thead><tbody>"
    )
    for r in rows:
        result = r.get("result", "")
        cls = "win" if result == "WIN" else ("loss" if result == "LOSS" else "open-t")
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
            f"<td class='{cls}'>{escape(result)}</td>"
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
            f"<div class='tip-dim'>age {age_str} · v={escape(r['version'])}</div>"
        )
        rows_html += (
            f"<div class='bot-row tt'>"
            f"<span class='badge {flag}' style='font-size:.62em'>{r['flag']}</span>"
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
            f"<span class='tip-label'>Services</span>{svc_tip_rows}"
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

    if error == "unreachable":
        return f"<div class='account'>{header}<p class='no-data'>⚠ unreachable</p></div>"

    # Determine whether this account runs Polymarket bots (live_bot / account_bot).
    _POLY_BOT_NAMES = {"live_bot", "account_bot"}
    has_poly = any(r["bot_name"] in _POLY_BOT_NAMES for r in (hb_rows or []))

    live = data.get("live")
    stats_html = ""
    trade_html = ""
    open_html  = ""

    if live and has_poly:
        totals    = (live.get("totals") or [{}])[0]
        cap_row   = (live.get("capital") or [{}])[0]
        today_row = (live.get("today") or [{}])[0]
        week_row  = (live.get("week") or [{}])[0]
        month_row = (live.get("month") or [{}])[0]
        life_row  = (live.get("lifetime") or [{}])[0]
        w         = totals.get("w", 0) or 0
        l         = totals.get("l", 0) or 0
        t         = totals.get("t", 0) or 0
        cap       = cap_row.get("capital")
        today_pnl = today_row.get("p")
        today_t   = today_row.get("t", 0) or 0
        week_pnl  = week_row.get("p")
        week_t    = week_row.get("t", 0) or 0
        month_pnl = month_row.get("p")
        month_t   = month_row.get("t", 0) or 0
        life_pnl  = life_row.get("p")
        life_t    = life_row.get("t", 0) or 0
        cap_str   = f"${cap:.2f}" if cap is not None else "—"
        pnl_sign  = "+" if (today_pnl or 0) >= 0 else "-"
        pnl_cls   = "pnl-pos" if (today_pnl or 0) >= 0 else "pnl-neg"
        pnl_str   = (f"{pnl_sign}${abs(today_pnl):.2f}" if today_pnl is not None else "—")
        stats_html = (
            f"<div class='big-metrics'>"
            f"<div class='metric-big tt'>"
            f"<div class='lbl'>Capital</div>"
            f"<div class='val'>{cap_str}</div>"
            f"<div class='metric-sub'>{_wr(w, l)} · {t}T</div>"
            f"<div class='tip tip-up'>"
            f"<span class='tip-label'>All-time</span>"
            f"<div class='tip-row'>Win rate {_wr(w, l)}</div>"
            f"<div class='tip-row'>Total trades {t}</div>"
            f"</div></div>"
            f"<div class='metric-big tt'>"
            f"<div class='lbl'>Today PnL</div>"
            f"<div class='val {pnl_cls}'>{pnl_str}</div>"
            f"<div class='metric-sub'>{today_t}T today</div>"
            f"<div class='tip tip-up'>"
            f"<span class='tip-label'>PnL breakdown</span>"
            f"<div class='tip-row'><span style='color:#8b949e;min-width:60px;display:inline-block'>Today</span>"
            f" {_fmt_pnl(today_pnl)} · {today_t}T</div>"
            f"<div class='tip-row'><span style='color:#8b949e;min-width:60px;display:inline-block'>7 days</span>"
            f" {_fmt_pnl(week_pnl)} · {week_t}T</div>"
            f"<div class='tip-row'><span style='color:#8b949e;min-width:60px;display:inline-block'>30 days</span>"
            f" {_fmt_pnl(month_pnl)} · {month_t}T</div>"
            f"<div class='tip-row'><span style='color:#8b949e;min-width:60px;display:inline-block'>Lifetime</span>"
            f" {_fmt_pnl(life_pnl)} · {life_t}T</div>"
            f"</div></div>"
            f"</div>"
        )
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
            open_html = f"<div class='open-trades'><span style='color:#d29922'>▶ Open:</span> {badges}</div>"
        recent = live.get("recent", [])
        if recent:
            trade_html = (
                f"<details><summary>Last {len(recent)} trades ▾</summary>"
                f"{_render_trade_table(recent, 'live')}"
                f"</details>"
            )

    elif not has_poly:
        # CEX-only account: big metrics from heartbeat payload
        primary = next(
            (r for r in (hb_rows or []) if r["bot_name"] in ("grid_bot", "swing_bot")),
            None,
        )
        if primary:
            p          = primary.get("payload", {})
            cap        = p.get("capital")
            pnl_d      = p.get("daily_pnl")
            trades_o   = p.get("open_trades")
            acct_s     = (primary.get("_label") or "").split()[0]
            is_sim     = (acct_s, primary["bot_name"]) not in _LIVE_BOTS
            mode_badge = _mode_badge(acct_s, primary["bot_name"])
            cap_str    = f"${cap:.0f}" if cap is not None else "—"
            pnl_sign   = "+" if (pnl_d or 0) >= 0 else "-"
            pnl_cls    = "pnl-pos" if (pnl_d or 0) >= 0 else "pnl-neg"
            pnl_str    = f"{pnl_sign}${abs(pnl_d):.2f}" if pnl_d is not None else "—"
            stats_html = (
                f"<div class='big-metrics'>"
                f"<div class='metric-big tt'>"
                f"<div class='lbl'>Capital</div>"
                f"<div class='val'>{cap_str}</div>"
                f"<div class='metric-sub'>{mode_badge}</div>"
                f"<div class='tip tip-up'>"
                f"<div class='tip-row'>Open orders: {trades_o if trades_o is not None else '—'}</div>"
                f"</div></div>"
                f"<div class='metric-big'>"
                f"<div class='lbl'>Today PnL</div>"
                f"<div class='val {pnl_cls}'>{pnl_str}</div>"
                f"<div class='metric-sub'>open: {trades_o if trades_o is not None else '—'}</div>"
                f"</div>"
                f"</div>"
            )

    cex_html = ""
    ob = data.get("ob")
    if ob:
        ob_tot    = (ob.get("totals") or [{}])[0]
        ob_recent = ob.get("recent", [])
        ow = ob_tot.get("w", 0) or 0
        ol = ob_tot.get("l", 0) or 0
        cex_html += (
            f"<div style='margin-top:6px;font-size:.8em'>"
            f"<span style='color:#8b949e;text-transform:uppercase;letter-spacing:.8px;"
            f"font-size:.85em'>orderbook_bot</span>"
            f" — {ob_tot.get('t', 0)}T · WR {_wr(ow, ol)}"
            f"</div>"
        )
        if ob_recent:
            cex_html += (
                f"<details><summary>Recent ob trades ▾</summary>"
                f"{_render_trade_table(ob_recent, 'ob')}"
                f"</details>"
            )
    accum = data.get("accum")
    if accum:
        accum_port = (accum.get("portfolio") or [{}])[0]
        accum_tot  = (accum.get("totals") or [{}])[0]
        btc  = accum_port.get("holdings_btc")
        usdt = accum_port.get("free_usdt")
        avg  = accum_port.get("avg_entry")
        btc_str  = f"{btc:.6f} BTC" if btc is not None else "—"
        usdt_str = f" · ${usdt:.0f} free" if usdt is not None else ""
        avg_str  = f" · avg ${avg:.0f}" if avg is not None else ""
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
        f"{header}{stats_html}{open_html}{trade_html}{cex_html}{bot_section}"
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
                flag, fcol = "n/a", _C_MUTE
            else:
                flag, fcol = "MISSING", _C_BAD
                n_missing += 1
        else:
            flag = hb["flag"]
            fcol = {"ALIVE": _C_OK, "STALE": _C_WARN}.get(flag, _C_BAD)

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
            f"<td style='color:{fcol};font-weight:600'>{escape(flag)}</td>"
            f"<td>{dep_cell}</td></tr>"
        )

    note = []
    if n_missing:
        note.append(f"<span style='color:{_C_BAD}'>{n_missing} expected but silent</span>")
    if n_mismatch:
        note.append(f"<span style='color:{_C_BAD}'>{n_mismatch} mode mismatch</span>")
    sub = "  ·  ".join(note) if note else f"<span style='color:{_C_OK}'>all expected bots present</span>"
    return (
        f"<h2>Expected vs Actual <span style='font-size:.5em;color:{_C_MUTE};font-weight:400'>"
        f"{len(inventory)} declared · {sub}</span></h2>"
        "<table class='hb-table'><thead><tr>"
        "<th>acct</th><th>bot</th><th>kind</th><th>mode</th><th>heartbeat</th><th>last deploy</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def _render_html(
    heartbeats: list,
    accounts: list,
    generated_at: datetime,
    collection_s: float,
    inventory: list | None = None,
    deploys: list | None = None,
    user_to_label: dict | None = None,
) -> str:
    hb_issues  = sum(1 for r in heartbeats if r["flag"] != "ALIVE")
    svc_issues = sum(1 for acc in accounts for svc in acc.get("services", []) if not svc["active"])
    unreachable = sum(1 for a in accounts if a.get("error") == "unreachable")
    total_issues = hb_issues + svc_issues + unreachable

    dot_cls     = "ok" if total_issues == 0 else ("warn" if hb_issues == 0 else "bad")
    status_text = "All systems nominal" if total_issues == 0 else f"{total_issues} issue(s)"
    ts_str      = generated_at.strftime("%Y-%m-%d %H:%M UTC")

    total_bots = len(heartbeats)
    alive_bots = sum(1 for r in heartbeats if r["flag"] == "ALIVE")
    alive_cls  = "alive" if alive_bots == total_bots else "stale"
    hb_cls     = "alive" if hb_issues == 0 else "dead"
    svc_cls    = "alive" if svc_issues == 0 else "dead"
    unr_cls    = "alive" if unreachable == 0 else "dead"

    # Aggregate PnL across all Polymarket accounts for each time window
    def _sum_pnl(key: str) -> float:
        return sum(
            float((acc.get("live") or {}).get(key, [{}])[0].get("p") or 0)
            for acc in accounts if acc.get("live")
        )

    today_pnl_total = _sum_pnl("today")
    week_pnl_total  = _sum_pnl("week")
    month_pnl_total = _sum_pnl("month")
    life_pnl_total  = _sum_pnl("lifetime")
    pnl_sign    = "+" if today_pnl_total >= 0 else ""
    pnl_val_cls = "pnl-pos" if today_pnl_total >= 0 else "pnl-neg"

    summary_bar = (
        f"<div class='summary-bar'>"
        f"<div class='sb-item'><div class='lbl'>Bots alive</div>"
        f"<div class='val {alive_cls}'>{alive_bots}/{total_bots}</div></div>"
        f"<div class='sb-item tt'>"
        f"<div class='lbl'>Today PnL</div>"
        f"<div class='val {pnl_val_cls}'>{pnl_sign}${today_pnl_total:.2f}</div>"
        f"<div class='tip'>"
        f"<span class='tip-label'>PnL breakdown (all accounts)</span>"
        f"<div class='tip-row'><span style='color:#8b949e;min-width:60px;display:inline-block'>Today</span>"
        f" {_fmt_pnl(today_pnl_total)}</div>"
        f"<div class='tip-row'><span style='color:#8b949e;min-width:60px;display:inline-block'>7 days</span>"
        f" {_fmt_pnl(week_pnl_total)}</div>"
        f"<div class='tip-row'><span style='color:#8b949e;min-width:60px;display:inline-block'>30 days</span>"
        f" {_fmt_pnl(month_pnl_total)}</div>"
        f"<div class='tip-row'><span style='color:#8b949e;min-width:60px;display:inline-block'>Lifetime</span>"
        f" {_fmt_pnl(life_pnl_total)}</div>"
        f"</div>"
        f"</div>"
        f"<div class='sb-item'><div class='lbl'>HB issues</div>"
        f"<div class='val {hb_cls}'>{hb_issues}</div></div>"
        f"<div class='sb-item'><div class='lbl'>Svc issues</div>"
        f"<div class='val {svc_cls}'>{svc_issues}</div></div>"
        f"<div class='sb-item'><div class='lbl'>Unreachable</div>"
        f"<div class='val {unr_cls}'>{unreachable}</div></div>"
        f"</div>"
    )

    # BTC 24h: try Binance API first, fall back to orderbook_bot heartbeat payload
    btc_24h   = _fetch_btc_24h()
    btc_price = float(btc_24h.get("lastPrice") or 0)
    btc_chg   = float(btc_24h.get("priceChangePercent") or 0)
    btc_high  = float(btc_24h.get("highPrice") or 0)
    btc_low   = float(btc_24h.get("lowPrice") or 0)
    if not btc_price:
        for r in heartbeats:
            if r["bot_name"] == "orderbook_bot":
                lp = r.get("payload", {}).get("last_price")
                if lp:
                    btc_price = float(lp)
                break
    if btc_price:
        chg_cls  = "up" if btc_chg >= 0 else "dn"
        chg_sign = "+" if btc_chg >= 0 else ""
        chg_html = (
            f"<span class='chg {chg_cls}'>{chg_sign}{btc_chg:.2f}%</span>"
            if btc_24h else ""
        )
        range_html = (
            f"<span style='color:#484f58;font-size:.88em'>H&thinsp;${btc_high:,.0f}"
            f" &nbsp; L&thinsp;${btc_low:,.0f}</span>"
            if btc_high else ""
        )
        btc_price_html = (
            f"<div class='btc-price'>"
            f"<span>BTC</span>"
            f"<span class='price'>${btc_price:,.0f}</span>"
            f"{chg_html}{range_html}"
            f"</div>"
        )
    else:
        btc_price_html = ""

    hb_html    = _render_heartbeat_pills(heartbeats)

    # Build per-account heartbeat rows (acct_short = first word of label, e.g. "acct-3")
    def _acct_hb(label: str) -> list:
        short = label.split()[0]
        return [r for r in heartbeats if (r.get("_label") or "").split()[0] == short]

    cards_html = "".join(
        _render_account_card(label, data, _acct_hb(label))
        for label, data in zip(_ACCOUNT_LABELS, accounts)
    )

    expected_html = _render_expected_actual(
        inventory or [], deploys or [], heartbeats, user_to_label or {})

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="60">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>tradinebotte — status</title>
<style>{_CSS}</style>
</head>
<body>
<h1>
  <span class="dot {dot_cls}"></span>
  tradinebotte — {escape(status_text)}
  {btc_price_html}
  <span style="font-size:.6em;color:#8b949e;font-weight:400">{ts_str}</span>
</h1>
{summary_bar}
{expected_html}
<h2>Infrastructure — Heartbeats</h2>
{hb_html}
<h2>Accounts — Services &amp; Trades</h2>
<div class="accounts">
{cards_html}
</div>
<div class="footer">
  Generated {ts_str} · collection {collection_s:.1f}s
  · <span id="rf-ct">refresh in 60s</span>
</div>
<script>
(function(){{
  var t=60,el=document.getElementById('rf-ct');
  setInterval(function(){{t--;if(t<=0)t=60;el&&(el.textContent='refresh in '+t+'s');}},1000);
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
    args = parser.parse_args()

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

    html = _render_html(
        heartbeats=heartbeats,
        accounts=accounts_data,
        generated_at=datetime.now(tz=timezone.utc),
        collection_s=elapsed,
        inventory=inventory_rows,
        deploys=deploy_rows,
        user_to_label=user_to_label,
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
