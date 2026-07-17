"""Test isolation for the CEX suite.

⚠ The bot accounts and the operator share ONE host (single-server topology), so the engine's
real push_trade / heartbeat push would reach the PRODUCTION status collector on 127.0.0.1:5562
and pollute the shared state DB with test data (observed: a live-exec test's fills landed as
'neofutur/unknown' heartbeats). Neutralise both paths for the whole test session:
  1. Point the status channel at a dead loopback port so any stray push connects to nothing.
  2. Monkeypatch the accumulation engine's _push_trade to a no-op (guaranteed, socket-independent).
No test should ever emit a trade or heartbeat to the real fleet collector.
"""

import os
import sys

import pytest

# A port nothing binds (production uses 5557/5559/5561/5562/5563). Set before any push_trade
# call caches its socket — conftest imports before tests run.
os.environ.setdefault("TRADINEBOTTE_STATUS_ADDR", "tcp://127.0.0.1:5599")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True, scope="session")
def _no_real_trade_push():
    import strategy_engines.accumulation as _acc  # noqa: PLC0415
    _orig = _acc._push_trade
    _acc._push_trade = lambda *a, **k: None
    yield
    _acc._push_trade = _orig
