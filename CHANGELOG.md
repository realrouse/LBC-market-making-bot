# Changelog

> 🇫🇷 [Version française](CHANGELOG.fr.md)

All notable changes to this project are documented here.

---

## [Unreleased]

### Documentation
- `bot/live_bot.py` — module docstring translated to English; docstrings added to all functions and classes; inline comments explain non-obvious invariants: temporal filter rationale, all 8 signal guards (including `ask_vol=0` initialization guard and expired-market `best_ask>=1.0` guard), OBI formula, PnL calculation, sysconfig path resolution, lazy ClobClient import, exponential backoff, expired-market cleanup, WAL mode rationale
- `scripts/setup.py` — module docstring translated to English; inline comments explain the security decisions (getpass, exact-amount ERC-20 approvals), Uniswap V3 swap parameters (fee tier 100, 0.5% slippage guard, 5-min deadline), sysconfig dynamic path, API key ECDSA derivation, and chmod 600 rationale

### Feature
- `POLYMARKET_DIR` environment variable now controls the install path across all scripts and the bot itself, defaulting to `/opt/polymarket-live`
- `scripts/install.sh` — accepts install directory as a positional argument or via `POLYMARKET_DIR`; generates a `run.sh` wrapper in the install dir with the path pre-set
- `scripts/start_bot.sh` — reads `POLYMARKET_DIR`, exports it when launching the bot
- `scripts/monitor.sh` — reads `POLYMARKET_DIR` for log and database paths
- `scripts/setup.py` — reads `POLYMARKET_DIR` for `config.json` path and venv site-packages; also fixes hardcoded Python 3.12 venv path (uses `sysconfig` like the bot)
- `bot/live_bot.py` — `DB_PATH`, `LOG_PATH`, `CONFIG_PATH` and venv lookup all derived from `POLYMARKET_DIR`

### Documentation
- `INSTALL` — new English installation guide extracted from README.md (requirements, dependencies, wallet setup, configuration, running, monitoring, virtual environment testing)
- `INSTALL.fr` — French translation of the installation guide
- `README.md` — installation sections replaced by a reference to INSTALL
- `README.fr.md` — installation sections replaced by a reference to INSTALL.fr

---

## [2026-04-23]

### Security — `9e6247c`
**Apply security fixes from audit (4 vulns)**
- `scripts/setup.py` — clé privée lue via `getpass()` au lieu de `sys.argv[1]` : n'apparaît plus dans `ps aux` ni dans l'historique shell
- `scripts/setup.py` — suppression de tout affichage de credentials sur stdout (clé privée partielle, api_key, api_secret, api_passphrase) : la sortie n'affiche plus que l'adresse du wallet
- `scripts/setup.py` — remplacement des approbations ERC-20 illimitées (`2**256-1`) par des montants exacts : `amount_in` pour le swap Uniswap V3, `bal_e` pour le CTF Exchange

### Documentation — `6aa8360`
**Add virtual environment test instructions to README**
- `README.md` — ajout d'une section complète "Testing in a virtual environment" avec toutes les commandes : installation de `uv`, création du venv, vérification de syntaxe, import check, dry-run de 20 secondes, sortie attendue

### Bugfix — `3c0ad40`
**Fix indentation error in WS except clause and hardcoded Python version**
- `bot/live_bot.py` — correction d'une `IndentationError` sur le bloc `except:` dans `_run_ws()` (10 espaces au lieu de 12) qui empêchait le démarrage du bot
- `bot/live_bot.py` — le chemin des site-packages du venv était codé en dur pour Python 3.12 ; remplacé par `sysconfig.get_path()` qui résout le bon chemin dynamiquement selon la version Python installée

### Feature — `cbdbf2a`
**Move credentials from env vars to config.json**
- `bot/live_bot.py` — ajout de `CONFIG_PATH` et d'une fonction `load_config()` qui lit `/opt/polymarket-live/config.json` en priorité, avec fallback sur les variables d'environnement
- `scripts/setup.py` — écrit automatiquement `config.json` (chmod 600) après dérivation des clés API, au lieu d'afficher des `export` à copier manuellement
- `scripts/start_bot.sh` — suppression des `export` de variables ; vérifie l'existence de `config.json` au démarrage
- `config.json.example` — template de référence ajouté au dépôt
- `.gitignore` — `config.json` ajouté pour éviter tout commit accidentel de credentials

### Documentation — `f452161`
**Add comprehensive README**
- `README.md` — réécriture complète avec description de la stratégie, prérequis, dépendances, installation, configuration, monitoring et notes opérationnelles

### Documentation — `beed5e1`
**Add CLAUDE.md with architecture and operational guidance**
- `CLAUDE.md` — documentation de l'architecture pour Claude Code : commandes, flux de données, paramètres critiques à ne pas modifier, décisions de conception, chemins de déploiement

### CI — `d225a5f`
**Add Claude Code GitHub Actions workflow**
- `.github/workflows/claude.yml` — workflow permettant de déclencher Claude Code depuis les issues et pull requests GitHub

### Initial — `85886ea`
**Import base — bot de trading Polymarket v3**
- `bot/live_bot.py` — bot async complet (617 lignes) : state machine WebSocket, signal `best_bid >= 0.96`, placement d'ordres LIMIT via `py_clob_client`, résolution WIN/LOSS, persistance SQLite (WAL), reconnexion automatique avec backoff exponentiel
- `scripts/install.sh` — installation des dépendances système et création du venv `/opt/polymarket-live/venv`
- `scripts/setup.py` — setup wallet : vérification des balances, swap USDC natif → USDC.e via Uniswap V3, approbation CTF Exchange, dérivation des clés API Polymarket
- `scripts/start_bot.sh` — script de lancement avec vérification des prérequis et gestion d'instance unique
- `scripts/monitor.sh` — dashboard de monitoring : statut du processus, logs temps réel, stats SQLite (trades, win rate, PnL)
- `docs/CONTEXT_AI.md` — documentation technique complète : stratégie, architecture, historique des bugs corrigés, résultats de backtest (1663 trades, 98.3% win rate), checklist de déploiement

### Initial — `7c119fd`
**Initial commit**
- Initialisation du dépôt git
