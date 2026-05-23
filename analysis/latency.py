#!/usr/bin/env python3
"""
Analyze latency measurements written by live_bot.py to live.log.

Each trade emits one [LATENCY] line:
    [LATENCY] signal_ms=2.31 order_rtt_ms=143.72 total_ms=146.03 direction=UP market=0x1234...

Metrics:
    signal_ms    — time from WebSocket message received to order decision
                   (includes all signal guards + daily-PnL SQLite query)
    order_rtt_ms — CLOB API round-trip time (HTTP POST → response)
    total_ms     — end-to-end: WebSocket message → order confirmed

Usage:
    python3 scripts/latency.py                        # default log path
    python3 scripts/latency.py ~/tradinebotte/live.log
    TRADINEBOTTE_DIR=~/tradinebotte python3 scripts/latency.py
"""

import os, re, sys
from statistics import mean, quantiles

# ── Resolve log path ──────────────────────────────────────────────────────────
if len(sys.argv) > 1:
    LOG_PATH = os.path.expanduser(sys.argv[1])
else:
    install_dir = os.path.expanduser(os.environ.get("TRADINEBOTTE_DIR", "~/tradinebotte"))
    LOG_PATH = os.path.join(install_dir, "live.log")

# ── Parse ─────────────────────────────────────────────────────────────────────
_PATTERN = re.compile(
    r"\[LATENCY\]"
    r".*?signal_ms=(?P<signal>[\d.]+)"
    r".*?order_rtt_ms=(?P<rtt>[\d.]+)"
    r".*?total_ms=(?P<total>[\d.]+)"
    r".*?direction=(?P<direction>\w+)"
)

records = []
try:
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            m = _PATTERN.search(line)
            if m:
                records.append({
                    "signal_ms":    float(m.group("signal")),
                    "order_rtt_ms": float(m.group("rtt")),
                    "total_ms":     float(m.group("total")),
                    "direction":    m.group("direction"),
                })
except FileNotFoundError:
    print(f"Log file not found: {LOG_PATH}")
    sys.exit(1)

if not records:
    print(f"No [LATENCY] lines found in {LOG_PATH}")
    print("Run the bot in production mode — latency lines are emitted on each trade.")
    sys.exit(0)

# ── Stats ──────────────────────────────────────────────────────────────────────
def stats(values):
    if len(values) < 2:
        return {"min": values[0], "mean": values[0], "p50": values[0],
                "p90": values[0], "p99": values[0], "max": values[0]}
    qs = quantiles(values, n=100)
    return {
        "min":  min(values),
        "mean": mean(values),
        "p50":  qs[49],
        "p90":  qs[89],
        "p99":  qs[98],
        "max":  max(values),
    }

metrics = {
    "signal_ms":    [r["signal_ms"]    for r in records],
    "order_rtt_ms": [r["order_rtt_ms"] for r in records],
    "total_ms":     [r["total_ms"]     for r in records],
}

# ── Report ────────────────────────────────────────────────────────────────────
ups   = sum(1 for r in records if r["direction"] == "UP")
downs = sum(1 for r in records if r["direction"] == "DOWN")

print(f"\n{'='*62}")
print(f"  LATENCY REPORT — {LOG_PATH}")
print(f"  Trades: {len(records)}  (UP={ups}  DOWN={downs})")
print(f"{'='*62}")
print(f"  {'Metric':<16} {'min':>7} {'mean':>7} {'p50':>7} {'p90':>7} {'p99':>7} {'max':>7}")
print(f"  {'-'*58}")
labels = {
    "signal_ms":    "signal (ms)",
    "order_rtt_ms": "order RTT (ms)",
    "total_ms":     "total (ms)",
}
for key, label in labels.items():
    s = stats(metrics[key])
    print(f"  {label:<16} {s['min']:>7.1f} {s['mean']:>7.1f} {s['p50']:>7.1f}"
          f" {s['p90']:>7.1f} {s['p99']:>7.1f} {s['max']:>7.1f}")
print(f"{'='*62}\n")
