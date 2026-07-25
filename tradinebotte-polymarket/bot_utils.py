#!/usr/bin/env python3
"""Utility helpers for live_bot: log dashboard and web status page."""

import html, logging, os, sqlite3
from datetime import datetime, timezone
from typing import Any

try:
    import bcrypt as _bcrypt
    _BCRYPT_AVAILABLE = True
except ImportError:
    _BCRYPT_AVAILABLE = False

logger = logging.getLogger("live")


def _today_ms_utc() -> int:
    """UTC midnight of the current day, in milliseconds — for daily DB aggregations."""
    return int(
        datetime.now(timezone.utc)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp() * 1000
    )

# ─── DASHBOARD ───────────────────────────────────────────────────────────────

def print_dashboard(state: Any, config: Any) -> None:
    """Log a periodic summary of bot status to the log file."""
    logger.info("=" * 65)
    logger.info("  LIVE BOT  %s UTC", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    logger.info("  Capital=$%.2f  PnL=$%+.2f  Trades=%d  WR=%.1f%%",
                state.capital, state.total_pnl, state.total_trades, state.win_rate)
    logger.info("  Tokens=%d  Markets=%d  Open=%d",
                len(state.tokens), len(state.market_tokens), len(state.open_trades))
    logger.info("=" * 65)
    write_web_status(state, config)


# ─── WEB STATUS PAGE ─────────────────────────────────────────────────────────

def _htpasswd(password: str) -> str:
    """Return an Apache-compatible htpasswd hash using bcrypt ($2y$)."""
    if not _BCRYPT_AVAILABLE:
        raise ImportError("bcrypt is required for web status auth — run: uv pip install bcrypt")
    return _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()


def setup_htaccess(html_path: str, config: Any) -> None:
    """
    Create .htaccess in the HTML directory and .htpasswd in config.install_dir.
    .htpasswd lives outside the web root so it cannot be downloaded.
    Skipped silently if config.webstatus_password is not set.
    """
    if not config.webstatus_password:
        return
    htpasswd_path = os.path.join(config.install_dir, ".webstatus_htpasswd")
    htaccess_path = os.path.join(os.path.dirname(html_path), ".htaccess")
    with open(htpasswd_path, "w", encoding="utf-8") as f:
        f.write(f"{config.webstatus_user}:{_htpasswd(config.webstatus_password)}\n")
    os.chmod(htpasswd_path, 0o640)
    if not os.path.exists(htaccess_path):
        with open(htaccess_path, "w", encoding="utf-8") as f:
            f.write(
                f'AuthType Basic\n'
                f'AuthName "Tradinebot Status"\n'
                f'AuthUserFile {os.path.abspath(htpasswd_path)}\n'
                f'Require valid-user\n'
            )
        logger.info("htaccess created: %s", htaccess_path)


def _status_html_trade_rows(conn: sqlite3.Connection) -> str:
    """Return HTML <tr> rows for the 10 most recent resolved trades."""
    rows = conn.execute(
        "SELECT id, direction, outcome, entry_price, pnl_net, capital_after, "
        "resolution_ts_ms, question "
        "FROM trades WHERE resolved=1 ORDER BY resolution_ts_ms DESC LIMIT 10"
    ).fetchall()
    if not rows:
        return '<tr><td colspan="8" style="color:#8b949e">No resolved trades</td></tr>'
    parts = []
    for tid, direction, outcome, entry, pnl, cap, ts_ms, question in rows:
        ts_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S") if ts_ms else "—"
        css = "win" if outcome == "WIN" else "loss"
        q_safe  = html.escape(question or "")
        q_short = q_safe[:42] + ("…" if len(q_safe) > 42 else "")
        parts.append(
            f'<tr class="{css}">'
            f'<td>#{tid}</td><td>{ts_str}</td><td>{direction}</td>'
            f'<td>{outcome}</td><td>{entry:.4f}</td>'
            f'<td>${pnl:+.2f}</td><td>${cap:.2f}</td>'
            f'<td title="{q_safe}">{q_short}</td></tr>'
        )
    return "\n".join(parts)


def generate_status_html(state: Any) -> str:
    """Build a self-contained HTML status page from current bot state."""
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    today_ms = _today_ms_utc()
    daily_row = state.conn.execute(
        "SELECT COALESCE(SUM(pnl_net),0), "
        "COALESCE(SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END),0), "
        "COUNT(*) FROM trades WHERE resolved=1 AND signal_ts_ms>=?",
        (today_ms,)
    ).fetchone()
    daily_pnl, _, daily_count = daily_row

    open_count  = len(state.open_trades)
    trade_rows  = _status_html_trade_rows(state.conn)

    pnl_cls  = "win"     if state.total_pnl >= 0 else "loss"
    day_cls  = "win"     if daily_pnl       >= 0 else "loss"
    wr_cls   = "win"     if state.win_rate  >= 80 else ("neutral" if state.win_rate >= 50 else "loss")
    open_cls = "neutral" if open_count      >  0  else ""

    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta http-equiv="refresh" content="60">\n'
        '<title>Tradinebot Status</title>\n'
        '<style>\n'
        'body{font-family:monospace;background:#0d1117;color:#c9d1d9;margin:0;padding:20px}\n'
        'h1{color:#58a6ff;border-bottom:1px solid #30363d;padding-bottom:10px}\n'
        'h2{color:#8b949e;font-size:1em;margin-top:24px}\n'
        '.cards{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px}\n'
        '.card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:14px 20px;min-width:120px}\n'
        '.card .label{font-size:.72em;color:#8b949e;text-transform:uppercase;letter-spacing:1px}\n'
        '.card .value{font-size:1.35em;font-weight:bold;margin-top:4px}\n'
        '.win{color:#3fb950}.loss{color:#f85149}.neutral{color:#d29922}\n'
        'table{width:100%;border-collapse:collapse;margin-top:8px;font-size:.88em}\n'
        'th{background:#21262d;color:#8b949e;padding:7px 11px;text-align:left;border-bottom:1px solid #30363d}\n'
        'td{padding:5px 11px;border-bottom:1px solid #21262d}\n'
        'tr.win td{color:#3fb950}tr.loss td{color:#f85149}\n'
        '.footer{margin-top:20px;font-size:.72em;color:#8b949e}\n'
        '.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3fb950;'
        'margin-right:6px;animation:pulse 2s infinite}\n'
        '@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}\n'
        '</style>\n</head>\n<body>\n'
        f'<h1><span class="dot"></span>Tradinebot — Live Status</h1>\n'
        '<div class="cards">\n'
        f'  <div class="card"><div class="label">Capital</div>'
        f'<div class="value">${state.capital:.2f}</div></div>\n'
        f'  <div class="card"><div class="label">PnL total</div>'
        f'<div class="value {pnl_cls}">${state.total_pnl:+.2f}</div></div>\n'
        f'  <div class="card"><div class="label">Win Rate</div>'
        f'<div class="value {wr_cls}">{state.win_rate:.1f}%</div></div>\n'
        f'  <div class="card"><div class="label">Trades</div>'
        f'<div class="value">{state.total_trades}</div></div>\n'
        f'  <div class="card"><div class="label">Today</div>'
        f'<div class="value {day_cls}">${daily_pnl:+.2f} ({daily_count}T)</div></div>\n'
        f'  <div class="card"><div class="label">Open</div>'
        f'<div class="value {open_cls}">{open_count}</div></div>\n'
        '</div>\n'
        '<h2>10 latest resolved trades</h2>\n'
        '<table>\n'
        '<thead><tr><th>#</th><th>Time (UTC)</th><th>Direction</th><th>Result</th>'
        '<th>Entry</th><th>PnL</th><th>Capital</th><th>Market</th></tr></thead>\n'
        f'<tbody>{trade_rows}</tbody>\n'
        '</table>\n'
        f'<div class="footer">Last updated: {now_str} UTC — '
        'Auto-refresh every 60&nbsp;s</div>\n'
        '</body>\n</html>\n'
    )


def write_web_status(state: Any, config: Any) -> None:
    """Write the HTML status page to disk. No-op when config.webstatus_enabled is false."""
    if not config.webstatus_enabled:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(config.webstatus_path)), exist_ok=True)
        setup_htaccess(config.webstatus_path, config)
        with open(config.webstatus_path, "w", encoding="utf-8") as f:
            f.write(generate_status_html(state))
    except Exception as e:
        logger.warning("Web status page error: %s", e)
