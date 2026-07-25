# Contributing

> 🇫🇷 [Version française](CONTRIBUTING.fr.md)

## Table of contents

- [Development setup](#development-setup)
- [Project structure](#project-structure)
- [Running tests](#running-tests)
- [Code quality](#code-quality)
- [Git workflow](#git-workflow)
- [Commit message style](#commit-message-style)
- [Release process](#release-process)
- [Language policy](#language-policy)
- [Bilingual documentation rule](#bilingual-documentation-rule)
- [Security rules](#security-rules)
- [Adding an exchange adapter](#adding-an-exchange-adapter)
- [Adding a strategy engine](#adding-a-strategy-engine)
- [Changing shared code: the symmetry rule](#changing-shared-code-the-symmetry-rule)

---

## Development setup

**Prerequisites**: Python 3.8+, Linux or macOS.

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte

# Install dev dependencies (pylint, mypy, pip-audit)
pip install -r requirements-dev.txt
```

Preferred: use `uv` for a faster isolated virtualenv:

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements-dev.txt
```

`tradinetools` (the shared library used by every subsystem) is **not**
pip-installed — production deploys wire it in via a source `.pth` file
(`scripts/install.sh`) rather than an editable install, because `pip
install -e` used to shadow the sibling `tradinetools/` directory and drift
from source, causing live bots to crash-loop on restart. Reproduce the
same thing for your dev `.venv`:

```bash
python3 - <<'PY'
import pathlib, sysconfig
sp = pathlib.Path(sysconfig.get_paths()["purelib"])
(sp / "tradinetools-source.pth").write_text(str(pathlib.Path("tradinetools").resolve()) + "\n")
PY
```

The test runner (`scripts/run_tests.sh`) auto-detects `.venv` at the project root.

**Activate the pre-commit hook** (one-time per clone — blocks commits that expose infrastructure details):

```bash
git config core.hooksPath .git-hooks
```

---

## Project structure

```
tradinebotte/
├── tradinebotte-cex/            # CEX trading bots and strategy engines
│   ├── cex_consumer.py          # CEX glue: feeds strategy_engines from cex_feed / indicators
│   ├── api_binance.py           # Binance spot adapter
│   ├── api_mexc.py              # MEXC spot adapter
│   ├── api_mexc_futures.py      # MEXC Futures perpetual adapter
│   ├── api_bitstamp.py          # Bitstamp spot adapter
│   ├── api_common.py            # Shared helpers: parse_levels(), book_snapshot(), hmac_sign()
│   ├── earn_manager.py          # Binance Simple Earn Flexible manager
│   ├── connectors/__init__.py   # validate() — connector/strategy compatibility check
│   ├── strategy_engines/        # Pluggable strategy engines
│   │   ├── base.py              # BaseStrategy interface
│   │   ├── grid.py              # Grid (static / trail=bear / trail=bull)
│   │   ├── swing.py             # Swing with EMA200 + ATR + RSI filters
│   │   ├── swinghold.py         # SwingHold — fractional sells, long-term hold
│   │   ├── dca.py               # Timed DCA with TP/SL
│   │   ├── accumulation.py      # Maker accumulation ladder (ratchet), hosts BAMM's live/shadow gate
│   │   └── bamm.py              # BAMM: fixed-rung, floor-anchored accumulating maker grid
│   ├── strategies/              # JSON config files per strategy
│   └── tests/                   # CEX-specific unit tests
│
├── tradinebotte-indicators/     # ZMQ signal pipeline
│   ├── indicators.py            # Main pipeline (RSI, EMA, OBI, TFI, liquidations)
│   ├── strategies/              # Stream config files (indicators_all.json, ...)
│   └── tests/
│
├── tradinebotte-polymarket/     # Polymarket prediction-market connector
│   ├── live_bot.py              # Shared host process: async state machine, dispatches to a
│   │                             # strategy/connector plugin (pm_strategy for Polymarket, or a
│   │                             # tradinebotte-cex strategy_engine for grid/swing/dca/accumulation)
│   ├── pm_*.py                  # Polymarket plugin (pm_types/pm_calendar/pm_strategy/pm_data)
│   ├── feed.py                  # WebSocket feed (ZMQ PUB)
│   ├── api_polymarket.py        # Polymarket CLOB adapter
│   ├── strategies/              # JSON strategy files
│   └── tests/
│
├── tradinebotte-core/            # Neutral core (botcore/): Strategy protocol, connector
│   └── botcore/                  # registry, persistence, base schema — no exchange-specific code
│
├── tradinebotte-status/         # Health monitoring
│   ├── status_collector.py      # Heartbeat collector (ZMQ → SQLite)
│   ├── inventory_labels.py      # Derives account labels / live-bot set from inventory.toml
│   └── generate_status.py       # HTML dashboard generator (DB-only, no per-account SSH)
│
├── tradinetools/                # Shared library (wired via source .pth, see Development setup)
│   └── tradinetools/
│       ├── math.py              # sma_last, ema_last, atr_last, bollinger_last, ...
│       ├── zmq.py               # ZMQ socket factories
│       ├── logging.py           # setup_root_logger(), setup_logger()
│       └── schemas.py           # Versioned message dataclasses
│
├── analysis/                    # Backtesting and analysis scripts
├── scripts/                     # Install, deploy, test, release scripts
├── systemd/                     # Canonical systemd unit templates (one dir, all services)
├── tests/                       # Core test suite (CEX strategies, API adapters, ...)
├── docs/                        # Documentation (see docs/ reference below)
├── inventory.toml.example       # Fleet topology TEMPLATE — copy to inventory.toml (local,
│                                 # git-ignored) and edit; drives deploy_all.sh, generate_status.py,
│                                 # bot_status.sh
├── requirements.txt             # Runtime dependencies
├── requirements-dev.txt         # Dev dependencies (pylint, mypy, pip-audit)
└── version.py                   # Single source of truth for version number
```

### docs/ reference

| File | Contents |
|---|---|
| [`docs/design.md`](docs/design.md) | Process architecture and ZMQ message-flow |
| [`docs/accumulation.md`](docs/accumulation.md) | Accumulation bot strategy design |
| [`docs/indicators.md`](docs/indicators.md) | Indicators pipeline reference |
| [`docs/GridTrading.md`](docs/GridTrading.md) | Grid strategy operation and setup |
| [`docs/AdaptedGridTrading.md`](docs/AdaptedGridTrading.md) | Grid backtest results and selection guide |
| [`docs/snapshots.md`](docs/snapshots.md) | Snapshots table schema and query reference |
| [`docs/logging.md`](docs/logging.md) | Canonical log tag vocabulary |
| [`docs/KellySizing.md`](docs/KellySizing.md) | Fractional Kelly sizing design |
| [`docs/multi.md`](docs/multi.md) | Multi-bot WebSocket architecture |
| [`docs/HOWTO_tests_and_backtests.md`](docs/HOWTO_tests_and_backtests.md) | Practical test and backtest guide |

---

## Running tests

```bash
bash scripts/run_tests.sh
```

1163 tests across 6 suites. No network access or credentials required — in-memory SQLite for every test.

The script also runs pylint on all tracked `.py` files. To run a single suite directly:

```bash
# Core suite (CEX strategies, API adapters, backtest engine)
python3 -m unittest discover -s tests/ -p "test_*.py" -v

# Specific subsystem
python3 -m unittest discover -s tradinebotte-cex/tests/ -p "test_*.py" -v
python3 -m unittest discover -s tradinebotte-indicators/tests/ -p "test_*.py" -v
python3 -m unittest discover -s tradinebotte-polymarket/tests/ -p "test_*.py" -v
python3 -m unittest discover -s tradinebotte-status/tests/ -p "test_*.py" -v
python3 -m unittest discover -s tradinetools/tests/ -p "test_*.py" -v
```

New code must include tests. No exception for strategy logic, API adapters, or new utility functions.

---

## Code quality

```bash
# Linter — target: ≥ 9.90/10; anything below blocks release
pylint tradinebotte-cex tradinebotte-indicators tradinebotte-polymarket tradinebotte-status tradinetools

# Type checker — must report 0 errors
mypy tradinebotte-polymarket tradinebotte-cex tradinebotte-indicators tradinetools --ignore-missing-imports

# Shell scripts — must be shellcheck-clean at warning level
shellcheck -S warning scripts/*.sh tradinebotte-*/scripts/*.sh
```

All three are run automatically by `scripts/prepare_release.sh`. Pylint below 9.90 blocks release.

---

## Git workflow

- `main` — stable releases only; never push directly
- `dev` — active development; all work targets `dev`
- Feature branches: branch off `dev`, open a PR targeting `dev`
- Merge `dev → main` only after running `scripts/prepare_release.sh` (see [Release process](#release-process))

---

## Commit message style

```
type: brief description in imperative mood

Optional longer body if the why isn't obvious.
```

Types used in this project:

| Prefix | When to use |
|---|---|
| `fix:` | Bug fix |
| `feat:` | New feature |
| `refactor:` | Code restructure without behaviour change |
| `docs:` | Documentation changes only |
| `test:` | New or updated tests |
| `scripts:` | Build, deploy, or tooling scripts |
| `logging:` | Logging-only changes |
| `release(vX.Y):` | Release preparation commit |

Keep the subject line under 72 characters. Reference issues or PRs in the body when relevant, not in the subject.

---

## Release process

Before every merge from `dev` to `main`:

```bash
bash scripts/prepare_release.sh
```

The script runs 7 checks in order:

| Step | Check | Blocking? |
|---|---|---|
| 1 | Unit tests (all 6 suites) | Yes |
| 2 | Pylint score ≥ 9.90 | Yes |
| 3 | Shellcheck clean on all `.sh` files | Yes |
| 4 | All 10 bilingual doc files present | Yes |
| 5 | CHANGELOG freshness (latest entry = today) | Warning |
| 6 | Data quality scan on `data/*.db` | Warning |
| 7 | Integration tests (if config present) | Warning |

Optional flags:
- `--skip-integration` — skip step 7 when the test environment is unavailable
- `--tag v0.XX` — create a git tag after a successful run

**Never merge `dev → main` without a green run.**

---

## Language policy

The project is **bilingual at the documentation level** and **English-only at the code level**:

| Artifact | Language |
|---|---|
| `README.md`, `CHANGELOG.md`, `INSTALL.md`, `QUICKSTART.md`, `UPDATE.md`, `CONTRIBUTING.md` | English |
| `README.fr.md`, `CHANGELOG.fr.md`, `INSTALL.fr.md`, `QUICKSTART.fr.md`, `UPDATE.fr.md`, `CONTRIBUTING.fr.md` | French |
| Source code (`.py`, `.sh`, `.json`) | **English only** |
| Code comments | **English only** |
| Log messages | **English only** |
| Docstrings | **English only** |

Never write French in source code, comments, log messages, or docstrings.

---

## Bilingual documentation rule

Documentation is maintained as paired EN/FR files. **Both files in a pair must be updated in the same commit** — never modify one without updating its counterpart:

| English | French |
|---|---|
| `README.md` | `README.fr.md` |
| `CHANGELOG.md` | `CHANGELOG.fr.md` |
| `INSTALL.md` | `INSTALL.fr.md` |
| `QUICKSTART.md` | `QUICKSTART.fr.md` |
| `UPDATE.md` | `UPDATE.fr.md` |
| `CONTRIBUTING.md` | `CONTRIBUTING.fr.md` |

`prepare_release.sh` step 4 verifies all 10 files are present and blocks the release if any are missing.

---

## Security rules

Never include the following in any public file (README, CHANGELOG, INSTALL, commit messages, etc.):

- Server hostnames or domain names
- IP addresses
- Deployment usernames
- Passwords or API tokens

Use generic descriptions instead: "the deployment accounts", "the production VPS", "the test server".

The pre-commit hook in `.git-hooks/pre-commit` blocks commits that contain known sensitive patterns. Activate it once per clone:

```bash
git config core.hooksPath .git-hooks
```

---

## Adding an exchange adapter

1. Create `tradinebotte-cex/api_<exchange>.py` implementing the same interface as `api_binance.py`:
   - `get_markets(session, symbol)` → order book snapshot
   - `post_order(session, symbol, side, quantity, price)` → order ID
   - `post_market_order(session, symbol, side, quantity)` → order ID
   - Shared utilities from `api_common.py`: `parse_levels()`, `book_snapshot()`, `hmac_sign()`

2. Add tests in `tests/test_api_cex.py` covering happy path, HTTP error codes, and network failures.

3. The compatibility check in `tradinebotte-cex/connectors/__init__.py` (`validate()`) will automatically verify that the new adapter exposes all methods required by the chosen strategy. No manual wiring needed.

---

## Adding a strategy engine

1. Create `tradinebotte-cex/strategy_engines/<name>.py` subclassing `BaseStrategy` from `base.py`.
   Implement at minimum: `on_tick()`, `on_fill()`, `restore_state()`.

2. Create a JSON config skeleton in `tradinebotte-cex/strategies/<name>/` documenting every parameter.

3. Add tests in `tradinebotte-cex/tests/test_strategy_engines.py` covering entry logic, exit logic, SL/TP, and state restore on restart.

4. If the strategy consumes indicator data, subscribe to the ZMQ PUB from `indicators.py` — see `docs/design.md` for the message format.

## Changing shared code: the symmetry rule

No strategy family is "main". Polymarket (threshold/grid), CEX grid/swing, and
accumulation are **peers**. One consequence is not obvious: `live_bot.py` lives under
`tradinebotte-polymarket/` but is the **universal** entrypoint — it runs the CEX
grid/swing strategies too (selected by `strategy_type` / `connector`). Several
side-effects are coupled to its shared functions; for example snapshot persistence used
to ride *inside* `handle_book_update`.

This is exactly how the 2026-06-16 silent-recording bug shipped: a new CEX consumer loop
(`cex_feed_consumer_loop`) bypassed `handle_book_update` for good reasons (it doesn't
need the Polymarket token bookkeeping) and **silently dropped the snapshot-persistence
side-effect** that rode inside it — and no test caught it, because that side-effect was
only ever exercised on the Polymarket path. The bots kept heartbeating; only a
background table stopped growing, for ~10 days.

**The rule:** before merging a change to code shared across families, enumerate *every*
side-effect of the function you touch or bypass, and verify each **for all families** —
not just the one you are working on. When you bypass a shared function, re-check what
*else* it did and re-create or test the parts you still need.

Checklist for any new or changed data-consumer path / shared hot-path function:

- [ ] **Snapshots persisted?** Route the write through the shared step
      (`_persist_snapshot` in `botcore.persistence`, `_record_snapshot` in
      `strategy_engines/accumulation.py`) — do not inline a bare `INSERT`. The 2026-06-16 bug above
      is fixed: `cex_feed_consumer_loop` now calls `save_cex_snapshot` (`cex_consumer.py`).
- [ ] **Data-freshness clock advanced?** Set `last_write_ts` on every persisted row.
      The status page `⚠data` badge depends on it — a bot that records but never updates
      it (or that records nothing) must show stale, not silent.
- [ ] **Trades / strategy state** written to that family's ledger (`trades`,
      `grid_levels`, `swing_orders`, `accum_trades`, …)?
- [ ] **Cumulative PnL** exported on the heartbeat (`pnl_total`)?
- [ ] **A test for each of the above, for THIS family.** See
      `docs/test_coverage_matrix.md` for the current grid and gaps. The structural guard
      `tradinebotte-polymarket/tests/test_bot.py::TestDataPathCoverage` fails any
      `live_bot` consumer that drives a strategy without persisting a snapshot — extend
      it (or add an equivalent) when you add a consumer elsewhere.
