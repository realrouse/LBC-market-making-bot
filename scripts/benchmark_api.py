#!/usr/bin/env python3
"""
Benchmark REST + WebSocket latency for the three supported exchanges:
  - Polymarket Gamma API  (market discovery)
  - Binance spot REST     (book ticker)
  - MEXC spot REST        (book ticker)

Measures HTTP round-trip time (DNS + TCP + TLS + server processing + response)
over N sequential requests per endpoint, then reports min/mean/p50/p90/p99/max.

Also probes WebSocket: time from connect() to first data message.

Usage:
    python3 scripts/benchmark_api.py            # 15 rounds, all exchanges
    python3 scripts/benchmark_api.py --rounds 30
    python3 scripts/benchmark_api.py --no-ws    # skip WebSocket probes
"""

import argparse, asyncio, json, statistics, time
import aiohttp

# WebSocket backend: prefer the standalone `websockets` library if installed;
# fall back to aiohttp's built-in WS client which is always available.
try:
    import websockets
    _WS_BACKEND = "websockets"
except ImportError:
    _WS_BACKEND = "aiohttp"

# ─── ENDPOINTS ────────────────────────────────────────────────────────────────
REST_ENDPOINTS = {
    "Polymarket Gamma": {
        "url":    "https://gamma-api.polymarket.com/markets",
        "params": {"closed": "false", "limit": 1, "tag_id": 102892},
        "headers": {"User-Agent": "Mozilla/5.0"},
    },
    "Binance": {
        "url":    "https://api.binance.com/api/v3/ticker/bookTicker",
        "params": {"symbol": "BTCUSDT"},
        "headers": {},
    },
    "MEXC": {
        "url":    "https://api.mexc.com/api/v3/ticker/bookTicker",
        "params": {"symbol": "BTCUSDT"},
        "headers": {},
    },
}

WS_ENDPOINTS = {
    "Polymarket WS": {
        "url":   "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        "send":  None,  # no subscribe needed — server sends ping on connect
    },
    "Binance WS": {
        "url":   "wss://stream.binance.com:9443/ws/btcusdt@depth5@100ms",
        "send":  None,  # depth stream starts automatically
    },
    "MEXC WS": {
        "url":   "wss://wbs.mexc.com/ws",
        "send":  json.dumps({
            "method": "SUBSCRIPTION",
            "params": ["spot@public.limit.depth.v3.api@BTCUSDT@5"],
        }),
    },
}

TIMEOUT = aiohttp.ClientTimeout(total=15)


# ─── HELPERS ─────────────────────────────────────────────────────────────────
async def fetch_polymarket_token(session):
    """Return one active Polymarket BTC-5M token_id for WS subscription."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    params = {
        "closed":       "false",
        "limit":        1,
        "tag_id":       102892,
        "end_date_min": (now - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_date_max": (now + timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        async with session.get(
            "https://gamma-api.polymarket.com/markets",
            headers={"User-Agent": "Mozilla/5.0"},
            params=params,
            timeout=TIMEOUT,
        ) as resp:
            data = await resp.json(content_type=None)
        markets = data if isinstance(data, list) else data.get("data", [])
        for m in markets:
            clob = m.get("clobTokenIds")
            if clob:
                ids = json.loads(clob) if isinstance(clob, str) else clob
                if ids:
                    return str(ids[0])
    except Exception:
        pass
    return None


# ─── STATS ────────────────────────────────────────────────────────────────────
def compute_stats(samples):
    if not samples:
        return None
    s = sorted(samples)
    n = len(s)
    def pct(p):
        """Nearest-rank percentile; clamped to [0, n-1] to handle boundary cases."""
        idx = max(0, min(n - 1, int(p / 100 * n)))
        return s[idx]
    return {
        "n":    n,
        "min":  s[0],
        "mean": statistics.mean(s),
        "p50":  pct(50),
        "p90":  pct(90),
        "p99":  pct(99),
        "max":  s[-1],
        "stdev": statistics.stdev(s) if n > 1 else 0.0,
    }


def print_stats(label, st, unit="ms"):
    if st is None:
        print(f"  {label:<22}  ERROR — aucune mesure")
        return
    print(
        f"  {label:<22}  "
        f"min={st['min']:7.1f}  mean={st['mean']:7.1f}  "
        f"p50={st['p50']:7.1f}  p90={st['p90']:7.1f}  "
        f"p99={st['p99']:7.1f}  max={st['max']:7.1f}  "
        f"σ={st['stdev']:6.1f}  ({st['n']} mesures, {unit})"
    )


# ─── REST BENCHMARK ───────────────────────────────────────────────────────────
async def bench_rest(session, name, cfg, rounds):
    samples = []
    errors = 0
    for _ in range(rounds):
        t0 = time.perf_counter()
        try:
            async with session.get(
                cfg["url"],
                params=cfg.get("params"),
                headers=cfg.get("headers", {}),
                timeout=TIMEOUT,
            ) as resp:
                await resp.read()
                if resp.status == 200:
                    samples.append((time.perf_counter() - t0) * 1000)
                else:
                    errors += 1
        except Exception:
            errors += 1
    return samples, errors


# ─── WEBSOCKET BENCHMARK ──────────────────────────────────────────────────────
async def bench_ws_once_aiohttp(session, url, send_msg):
    """Return ms from connect() call to first data message (aiohttp backend)."""
    t0 = time.perf_counter()
    try:
        async with session.ws_connect(
            url, timeout=aiohttp.ClientWSTimeout(ws_receive=10),
            heartbeat=None,
        ) as ws:
            if send_msg:
                await ws.send_str(send_msg)
            for _ in range(8):
                msg = await asyncio.wait_for(ws.receive(), timeout=10)
                if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.CLOSING):
                    break
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    # Accept any non-empty message — includes ack + book snapshots
                    if msg.data:
                        return (time.perf_counter() - t0) * 1000
    except Exception:
        pass
    return None


async def bench_ws_once_websockets(url, send_msg):
    """Return ms from connect() call to first data message (websockets backend)."""
    t0 = time.perf_counter()
    try:
        async with websockets.connect(url, open_timeout=10, close_timeout=5) as ws:
            if send_msg:
                await ws.send(send_msg)
            for _ in range(5):
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                msg = json.loads(raw) if raw else {}
                # Binance/MEXC send {"result": null, "id": N} as a subscribe ack
                # before the first real book snapshot — skip those frames.
                if msg and msg.get("result") is not None:
                    return (time.perf_counter() - t0) * 1000
    except Exception:
        pass
    return None


async def bench_ws(session, name, cfg, rounds):
    samples = []
    errors = 0
    for _ in range(rounds):
        if _WS_BACKEND == "websockets":
            ms = await bench_ws_once_websockets(cfg["url"], cfg.get("send"))
        else:
            ms = await bench_ws_once_aiohttp(session, cfg["url"], cfg.get("send"))
        if ms is not None:
            samples.append(ms)
        else:
            errors += 1
        await asyncio.sleep(0.3)
    return samples, errors


# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main(rounds, no_ws):
    print(f"\n{'='*82}")
    print(f"  BENCHMARK API — {rounds} requests per endpoint")
    print(f"{'='*82}")

    # ── REST ──────────────────────────────────────────────────────────────────
    print(f"\n  {'REST':─<78}")
    print(f"  {'Exchange':<22}  {'min':>7}  {'mean':>7}  {'p50':>7}  "
          f"{'p90':>7}  {'p99':>7}  {'max':>7}  {'σ':>6}")
    print(f"  {'-'*78}")

    connector = aiohttp.TCPConnector(limit=5, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = {}
        for name, cfg in REST_ENDPOINTS.items():
            samples, errors = await bench_rest(session, name, cfg, rounds)
            results[name] = (samples, errors)
            label = name + (f" ({errors} err)" if errors else "")
            print_stats(label, compute_stats(samples))

    # ── CLASSEMENT REST ───────────────────────────────────────────────────────
    ranked = sorted(
        [(n, compute_stats(s)) for n, (s, _) in results.items() if s],
        key=lambda x: x[1]["mean"]
    )
    print("\n  Classement REST par latence moyenne (ms) :")
    for rank, (name, st) in enumerate(ranked, 1):
        spark = "█" * int(st["mean"] / 5)
        print(f"    {rank}. {name:<22} {st['mean']:6.1f} ms  {spark}")

    if no_ws:
        print(f"\n{'='*82}\n")
        return

    # ── WEBSOCKET ─────────────────────────────────────────────────────────────
    # WS probes open a full TCP+TLS handshake each round — more expensive than REST.
    # 1/3 of REST rounds keeps total runtime reasonable; floor of 5 for basic stats.
    ws_rounds = max(5, rounds // 3)
    ws_header = f"WEBSOCKET — temps jusqu'au 1er message (backend: {_WS_BACKEND})"
    print(f"\n  {ws_header:─<78}")
    print(f"  ({ws_rounds} connexions par endpoint)")
    print(f"  {'Exchange':<22}  {'min':>7}  {'mean':>7}  {'p50':>7}  "
          f"{'p90':>7}  {'p99':>7}  {'max':>7}  {'σ':>6}")
    print(f"  {'-'*78}")

    connector2 = aiohttp.TCPConnector(limit=5, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=connector2) as ws_session:
        # Polymarket WS requires a subscription with a live token_id
        token_id = await fetch_polymarket_token(ws_session)
        if token_id:
            WS_ENDPOINTS["Polymarket WS"]["send"] = json.dumps({
                "type": "subscribe", "channel": "market",
                "assets_ids": [token_id],
            })
        else:
            print("  [WS] Polymarket: no active token found (outside time window?)")

        ws_results = {}
        for name, cfg in WS_ENDPOINTS.items():
            samples, errors = await bench_ws(ws_session, name, cfg, ws_rounds)
            ws_results[name] = (samples, errors)
            label = name + (f" ({errors} err)" if errors else "")
            print_stats(label, compute_stats(samples))

        ws_ranked = sorted(
            [(n, compute_stats(s)) for n, (s, _) in ws_results.items() if s],
            key=lambda x: x[1]["mean"]
        )
        print("\n  WS ranking by mean latency (ms):")
        for rank, (name, st) in enumerate(ws_ranked, 1):
            spark = "█" * int(st["mean"] / 10)
            print(f"    {rank}. {name:<22} {st['mean']:6.1f} ms  {spark}")

    print(f"\n{'='*82}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark REST + WS latency")
    parser.add_argument("--rounds", type=int, default=15,
                        help="REST requests per endpoint (default: 15)")
    parser.add_argument("--no-ws", action="store_true",
                        help="skip WebSocket tests")
    args = parser.parse_args()
    asyncio.run(main(args.rounds, args.no_ws))
