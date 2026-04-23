# Journal des modifications

> 🇬🇧 [English version](CHANGELOG.md)

Toutes les modifications notables de ce projet sont documentées ici.

---

## [Non publié]

### Fonctionnalité
- `tests/test_bot.py` — suite de tests automatisés (71 tests, aucun service externe requis) : `TestComputeFee` (4), `TestParseBookMessage` (14), `TestMarketHelpers` (9), `TestTokenState` (7), `TestRegisterMarket` (5), `TestCheckSignal` (13 gardes dont les 8 conditions d'entrée, le stop-loss journalier et la prévention des doublons), `TestCheckResolution` (7), `TestCloseTrade` (6), `TestRestoreState` (5) ; tous les tests utilisent une base SQLite en mémoire et un `POLYMARKET_DIR=/tmp/polymarket-test` fixe pour ne jamais toucher les fichiers de production
- `scripts/run_tests.sh` — script de lancement des tests : détecte automatiquement le `.venv` du projet ou le venv de production, fixe `POLYMARKET_DIR` sur `/tmp`, lance `unittest discover`
- `config.json` / `config.json.example` — nouvelle clé optionnelle `db_mmap_mb` (entier, défaut `0`) : quand elle est non nulle, active `PRAGMA mmap_size` pour que SQLite mappe le fichier de base de données via le page cache du kernel ; mettre à ex. `256` pour 256 Mo
- `bot/live_bot.py` — `load_config()` refactorisée pour retourner le dict de config complet (extensible pour de futures options) ; `DB_MMAP_MB` dérivé de la config au démarrage ; `init_db()` applique le pragma et enregistre une ligne de confirmation dans les logs quand le mmap est actif

### Documentation
- `README.md` — nouvelle section "Database" : justification SQLite/WAL, schéma complet de la table `trades` (29 colonnes avec type et description), schéma de la table `snapshots`, et 4 exemples de requêtes commentés
- `README.fr.md` — traduction française de la nouvelle section Base de données
- `bot/live_bot.py` — docstring du module traduit en anglais ; docstrings ajoutées à toutes les fonctions et classes ; commentaires inline expliquant les invariants non évidents : rôle du filtre temporel, les 8 gardes du signal (dont la garde `ask_vol=0` d'initialisation et la garde `best_ask>=1.0` pour les marchés expirés), la formule OBI, le calcul du PnL, la résolution dynamique du chemin sysconfig, l'import paresseux de ClobClient, le backoff exponentiel, le nettoyage des marchés expirés, et le mode journal WAL
- `scripts/setup.py` — docstring du module traduit en anglais ; commentaires inline expliquant les décisions de sécurité (getpass, approbations ERC-20 à montant exact), les paramètres du swap Uniswap V3 (fee tier 100, garde slippage 0,5%, deadline 5 min), le chemin dynamique sysconfig, la dérivation ECDSA des clés API, et le chmod 600

### Fonctionnalité
- La variable d'environnement `POLYMARKET_DIR` contrôle désormais le chemin d'installation dans tous les scripts et dans le bot, avec `/opt/polymarket-live` comme valeur par défaut
- `scripts/install.sh` — accepte le répertoire d'installation en argument positionnel ou via `POLYMARKET_DIR` ; génère un wrapper `run.sh` dans le répertoire d'installation avec le chemin pré-défini
- `scripts/start_bot.sh` — lit `POLYMARKET_DIR` et l'exporte lors du lancement du bot
- `scripts/monitor.sh` — lit `POLYMARKET_DIR` pour les chemins des logs et de la base de données
- `scripts/setup.py` — lit `POLYMARKET_DIR` pour le chemin de `config.json` et les site-packages du venv ; corrige également le chemin venv codé en dur pour Python 3.12 (utilise `sysconfig` comme le bot)
- `bot/live_bot.py` — `DB_PATH`, `LOG_PATH`, `CONFIG_PATH` et la recherche du venv sont tous dérivés de `POLYMARKET_DIR`

### Documentation
- `INSTALL` — nouveau guide d'installation en anglais extrait du README.md (prérequis, dépendances, configuration du wallet, lancement, monitoring, test en environnement virtuel)
- `INSTALL.fr` — traduction française du guide d'installation
- `README.md` — sections d'installation remplacées par une référence au fichier INSTALL
- `README.fr.md` — sections d'installation remplacées par une référence au fichier INSTALL.fr

---

## [2026-04-23]

### Sécurité — `9e6247c`
**Correctifs de sécurité suite à l'audit (4 vulnérabilités)**
- `scripts/setup.py` — clé privée lue via `getpass()` au lieu de `sys.argv[1]` : n'apparaît plus dans `ps aux` ni dans l'historique shell
- `scripts/setup.py` — suppression de tout affichage de credentials sur stdout (clé privée partielle, api_key, api_secret, api_passphrase) : la sortie n'affiche plus que l'adresse du wallet
- `scripts/setup.py` — remplacement des approbations ERC-20 illimitées (`2**256-1`) par des montants exacts : `amount_in` pour le swap Uniswap V3, `bal_e` pour le CTF Exchange

### Documentation — `6aa8360`
**Ajout des instructions de test en environnement virtuel dans le README**
- `README.md` — ajout d'une section complète "Testing in a virtual environment" avec toutes les commandes : installation de `uv`, création du venv, vérification de syntaxe, import check, dry-run de 20 secondes, sortie attendue

### Correction de bug — `3c0ad40`
**Correction d'une erreur d'indentation dans le bloc except WebSocket et de la version Python codée en dur**
- `bot/live_bot.py` — correction d'une `IndentationError` sur le bloc `except:` dans `_run_ws()` (10 espaces au lieu de 12) qui empêchait le démarrage du bot
- `bot/live_bot.py` — le chemin des site-packages du venv était codé en dur pour Python 3.12 ; remplacé par `sysconfig.get_path()` qui résout le bon chemin dynamiquement selon la version Python installée

### Fonctionnalité — `cbdbf2a`
**Migration des credentials des variables d'environnement vers config.json**
- `bot/live_bot.py` — ajout de `CONFIG_PATH` et d'une fonction `load_config()` qui lit `/opt/polymarket-live/config.json` en priorité, avec fallback sur les variables d'environnement
- `scripts/setup.py` — écrit automatiquement `config.json` (chmod 600) après dérivation des clés API, au lieu d'afficher des `export` à copier manuellement
- `scripts/start_bot.sh` — suppression des `export` de variables ; vérifie l'existence de `config.json` au démarrage
- `config.json.example` — template de référence ajouté au dépôt
- `.gitignore` — `config.json` ajouté pour éviter tout commit accidentel de credentials

### Documentation — `f452161`
**Rédaction complète du README**
- `README.md` — réécriture complète avec description de la stratégie, prérequis, dépendances, installation, configuration, monitoring et notes opérationnelles

### Documentation — `beed5e1`
**Ajout du fichier CLAUDE.md avec l'architecture et les consignes opérationnelles**
- `CLAUDE.md` — documentation de l'architecture pour Claude Code : commandes, flux de données, paramètres critiques à ne pas modifier, décisions de conception, chemins de déploiement

### CI — `d225a5f`
**Ajout du workflow GitHub Actions pour Claude Code**
- `.github/workflows/claude.yml` — workflow permettant de déclencher Claude Code depuis les issues et pull requests GitHub

### Initial — `85886ea`
**Import de la base — bot de trading Polymarket v3**
- `bot/live_bot.py` — bot async complet (617 lignes) : state machine WebSocket, signal `best_bid >= 0.96`, placement d'ordres LIMIT via `py_clob_client`, résolution WIN/LOSS, persistance SQLite (WAL), reconnexion automatique avec backoff exponentiel
- `scripts/install.sh` — installation des dépendances système et création du venv `/opt/polymarket-live/venv`
- `scripts/setup.py` — setup wallet : vérification des balances, swap USDC natif → USDC.e via Uniswap V3, approbation CTF Exchange, dérivation des clés API Polymarket
- `scripts/start_bot.sh` — script de lancement avec vérification des prérequis et gestion d'instance unique
- `scripts/monitor.sh` — dashboard de monitoring : statut du processus, logs temps réel, stats SQLite (trades, win rate, PnL)
- `docs/CONTEXT_AI.md` — documentation technique complète : stratégie, architecture, historique des bugs corrigés, résultats de backtest (1663 trades, 98,3% de taux de victoire), checklist de déploiement

### Initial — `7c119fd`
**Premier commit**
- Initialisation du dépôt git
