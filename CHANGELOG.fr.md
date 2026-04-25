# Journal des modifications

> 🇬🇧 [English version](CHANGELOG.md)

Toutes les modifications notables de ce projet sont documentées ici.

---

## [Non publié]

### Fonctionnalité
- `bot/live_bot.py` — les écritures de logs sont désormais entièrement asynchrones : un thread daemon `QueueListener` vide la queue de logs sur disque ; le event loop asyncio n'est plus jamais bloqué par des I/O fichier ; aucun changement de comportement pour les déploiements existants
- `bot/live_bot.py` — nouveau flag `--no-log` : supprime entièrement le fichier log (`NullHandler`) ; la DB SQLite (trades + snapshots) n'est pas affectée ; combiner avec `--simulate` pour conserver la sortie stdout ; destiné aux déploiements de production où les I/O disque doivent être minimaux

### Correction de bug
- Tous les scripts, `bot/live_bot.py` et toute la documentation — variable d'environnement renommée de `POLYMARKET_DIR` en `TRADINEBOTTE_DIR` pour correspondre au nom du projet ; mettre à jour tout `export POLYMARKET_DIR=...` dans le profil shell ou l'unité systemd en `export TRADINEBOTTE_DIR=...`

### Correction de bug
- Tous les scripts, `bot/live_bot.py` et toute la documentation — répertoire d'installation par défaut renommé de `~/polymarket` en `~/tradinebotte` pour correspondre au nom du bot ; `TRADINEBOTTE_DIR` reste prioritaire comme avant ; répertoire temporaire de simulation renommé de `/tmp/polymarket-sim` en `/tmp/tradinebotte-sim` ; répertoire temporaire de test renommé de `/tmp/polymarket-test` en `/tmp/tradinebotte-test`

### Correction de bug
- Tous les scripts et `bot/live_bot.py` — chemin d'installation par défaut changé de `/opt/polymarket-live` (nécessitait root) vers `~/polymarket` (aucun root requis) ; `TRADINEBOTTE_DIR` reste prioritaire comme avant ; les entrées historiques du CHANGELOG référençant `/opt/polymarket-live` reflètent l'ancien défaut et sont conservées

### Correction de bug
- `scripts/backtest.py` — le fallback vers le dataset embarqué exige désormais que `live.db` contienne au moins 100 snapshots (auparavant, tout fichier non vide était accepté, ce qui permettait à un artefact de test périmé de 16 snapshots de masquer le dataset embarqué) ; affiche également quel fichier est sélectionné (`(live)` ou `(sample)`) au démarrage

### Fonctionnalité
- `bot/live_bot.py` — nouveau flag `--simulate` : redirige tous les fichiers vers `/tmp/polymarket-sim` (surcharge `TRADINEBOTTE_DIR`), duplique les logs sur stdout en plus du fichier log, et affiche un avertissement `MODE SIMULATION` visible ; le `live.db` et `live.log` de production ne sont jamais touchés ; utilisable sur n'importe quelle machine sans credentials
- `INSTALL.md` / `INSTALL.fr.md` — section Tests mise à jour : `timeout 20 python3 bot/live_bot.py --simulate` remplace la commande précédente qui écrivait dans le chemin de production par défaut
- `README.md` / `README.fr.md` — bullet Mode simulation mis à jour pour décrire `--simulate`

### Correction de bug
- `scripts/start_bot.sh` — refuse de démarrer si une instance de `live_bot.py` tourne déjà (quitte avec erreur et affiche le PID existant) ; précédemment tuait l'instance en cours automatiquement, ce qui pouvait interrompre un trade ouvert en cours de résolution
- `INSTALL.md` / `INSTALL.fr.md` — section Lancement mise à jour pour documenter le nouveau comportement et la commande d'arrêt manuel (`pkill -f live_bot.py`)

### Données
- `data/backtest_sample_btc5m_range_2026.db` — jeu de données SQLite embarqué : 2430 snapshots collectés en mode simulation le 2026-04-25 sur de vrais marchés BTC 5 minutes Polymarket (table snapshots uniquement, aucune credential ni donnée de trade)
- `scripts/backtest.py` — fallback automatique vers `data/backtest_sample_btc5m_range_2026.db` si `TRADINEBOTTE_DIR/live.db` est absent ; permet de lancer le backtest sur n'importe quelle machine sans base de données du bot live

### Performance
- `bot/live_bot.py` — `MARKET_REFRESH` réduit de 90 s à 30 s : le bot découvre désormais les nouveaux marchés au plus 30 s après leur entrée dans la fenêtre ±6 minutes, au lieu de 90 s maximum ; l'appel Gamma API reste une requête unique (filtre tag_id=102892, sans pagination), la surcharge est négligeable

### Documentation
- `INSTALL.md` / `INSTALL.fr.md` — CLI sqlite3 ajouté aux prérequis avec note indiquant que le bot fonctionne sans lui ; commande Python de remplacement fournie pour les hôtes sans sudo

### Fonctionnalité
- `strategies/polymarket_BTC5M.json` — nouveau fichier de stratégie : tous les paramètres de signal et de capital backtestés (`signal_threshold`, `entry_max`, `min_secs_remaining`, `min_ask_vol`, `win_threshold`, `loss_threshold`, `obi_reject_thresh`, `daily_stop_loss`, `stake`, `capital_start`, `gas_fee_usd`) extraits des constantes en dur vers un fichier JSON versionné ; ajouter `"strategy": "<chemin>"` dans `config.json` pour changer de stratégie
- `bot/live_bot.py` — `load_strategy()` charge le fichier JSON au démarrage ; les paramètres surchargent les valeurs par défaut ; fallback silencieux sur les valeurs par défaut si le fichier est absent (dev/tests) ; nom de la stratégie loggé au démarrage
- `config.json.example` — nouvelle clé optionnelle `strategy` pointant vers le fichier de stratégie
- `scripts/install.sh` — copie `strategies/*.json` dans le répertoire d'installation
- `scripts/install.sh` — nouveau flag `--with-tests` : copie `tests/` et `scripts/backtest.py` dans le répertoire d'installation et lance immédiatement la suite complète de 99 tests ; fonctionne avec n'importe quel chemin d'installation ; usage : `bash scripts/install.sh ~/polymarket --with-tests`
- `INSTALL.md` / `INSTALL.fr.md` — option `--with-tests` documentée

### Refactorisation
- `bot/api_polymarket.py` — nouvel adaptateur API Polymarket : tout le code spécifique à l'exchange extrait de `live_bot.py` dans un module dédié (`get_markets`, `post_order`, `parse_book_update`, `compute_fee`, `get_market_id/question/end_ts/start_ts/up_token/down_token`, `make_subscribe_msg`, `WS_URL`, `WS_BATCH_SIZE`, `FEE_RATE`). Pour cibler un autre exchange à l'avenir, il suffira de créer `api_<exchange>.py` avec la même interface publique et de modifier l'unique ligne d'import dans `live_bot.py`.
- `bot/live_bot.py` — importe `api_polymarket as api` ; toutes les constantes et fonctions spécifiques Polymarket remplacées par des appels `api.xxx()` ; `register_market` utilise `api.get_market_id/question/etc.` ; `enter_live_trade` appelle `api.compute_fee` et `api.post_order` ; la boucle WebSocket utilise `api.WS_URL`, `api.WS_BATCH_SIZE`, `api.make_subscribe_msg`, `api.get_markets`, `api.parse_book_update` ; imports de haut niveau inutilisés supprimés (`hashlib`, `hmac`, `uuid`)
- `tests/test_bot.py` — `TestComputeFee`, `TestParseBookMessage`, `TestMarketHelpers` importent désormais depuis `api_polymarket` directement ; l'utilitaire `insert_trade` utilise `api_poly.compute_fee`
- `scripts/install.sh` — copie `bot/api_polymarket.py` aux côtés de `live_bot.py` ; vérification syntaxique étendue aux deux fichiers

### Documentation
- `docs/status_example.html` — aperçu HTML statique de la page de statut web ; illustre la mise en page en thème sombre, les cartes de métriques (capital, PnL, taux de victoire, trades, stats journalières, positions ouvertes) et le tableau des trades résolus avec coloration WIN/LOSS
- `README.md` / `README.fr.md` — lien vers `docs/status_example.html` ajouté dans la ligne de fonctionnalité "Page de statut HTML optionnelle"
- `INSTALL.md` / `INSTALL.fr.md` — référence à `docs/status_example.html` ajoutée dans la section "Page de statut web"
- `README.md` / `README.fr.md` — section Fonctionnalités ajoutée listant les 11 capacités du bot avec descriptions en une ligne
- `INSTALL.md` / `INSTALL.fr.md` — section "Page de statut web" développée avec les prérequis Apache pas-à-pas : `a2enmod userdir auth_basic authn_file`, directive `AllowOverride AuthConfig`, deux options pour donner au processus `www-data` l'accès en lecture au `.htpasswd` (`chmod o+r` ou `usermod -aG`) ; note nginx expliquant que `.htaccess` n'est pas traité et montrant le bloc server équivalent avec `auth_basic` / `auth_basic_user_file` ; note sur les chemins personnalisés en dehors de `~/public_html`
- `README.md` / `README.fr.md` — entrées du tableau de configuration `webstatuspage_*` mises à jour pour mentionner les modules Apache requis, `AllowOverride AuthConfig`, les permissions `www-data`, et la configuration manuelle nginx dans la colonne description

### Fonctionnalité
- `bot/live_bot.py` — nouvelle page de statut web : si `webstatuspage_html` vaut `true` dans `config.json`, le bot écrit une page HTML autonome en thème sombre affichant le capital, le PnL total et journalier, le taux de victoire, les positions ouvertes et les 10 derniers trades résolus ; la page est écrite toutes les `DASHBOARD_INTERVAL` secondes (5 min) et immédiatement après chaque résolution de trade ; le répertoire est créé automatiquement ; la page inclut un `<meta http-equiv="refresh" content="60">` pour le rechargement automatique dans le navigateur
- `bot/live_bot.py` — protection `.htaccess` / `.htpasswd` Basic Auth : si `webstatus_password` est défini, `setup_htaccess()` écrit un `.htpasswd` (format Apache `{SHA}`, sans dépendance externe) dans `TRADINEBOTTE_DIR` (hors de la racine web) et un `.htaccess` pointant vers lui dans le répertoire de la page HTML ; les changements de mot de passe sont appliqués à la prochaine écriture de la page ; le `.htaccess` n'est écrit qu'une fois et non écrasé s'il existe déjà pour préserver les modifications manuelles
- `config.json.example` — quatre nouvelles clés optionnelles : `webstatuspage_html` (booléen, défaut `false`), `webstatuspage_path` (chaîne, défaut `~/public_html/tradinebot_status.html`), `webstatus_user` (chaîne, défaut `"tradinebot"`), `webstatus_password` (chaîne, défaut `""`)

### Performance
- `bot/live_bot.py` — `_market_refresh_loop()` extrait en tâche `asyncio.Task` de fond : le polling de l'API Gamma (jusqu'à 15 s de timeout HTTP toutes les 90 s) ne bloque plus le traitement des messages WebSocket ; la boucle `recv()` et la découverte des marchés s'exécutent désormais en concurrence dans le même event loop
- `bot/live_bot.py` — `_run_ws()` : timeout recv réduit de 90 s à 30 s ; `TimeoutError` déclenche maintenant `continue` au lieu d'une reconnexion complète — les keepalives `ping_interval=20` / `ping_timeout=10` détectent les connexions mortes ; la reconnexion ne s'active que lorsque tous les marchés suivis ont expiré ; le bloc `finally` garantit l'annulation de la tâche de refresh à toute déconnexion WebSocket
- `bot/live_bot.py` — `fetch_markets()` : `tag_id=102892` (tag Polymarket `5M`) ajouté à `POLY_GAMMA_PARAMS` pour un pré-filtrage côté serveur ; la fenêtre temporelle ±6 minutes retourne désormais ~12–20 marchés au lieu de potentiellement des milliers ; la boucle de pagination (jusqu'à 20 requêtes × 100 marchés) remplacée par un seul appel API ; le filtre Python `BTC_5M_KEYWORDS` conservé comme filet de sécurité
- `README.md` / `README.fr.md` — section Notes mise à jour pour refléter le timeout à 30 s, la tâche de refresh en fond et le filtre tag de l'API Gamma

### Fonctionnalité
- `scripts/backtest.py` — moteur de backtest autonome : rejoue la table `snapshots` chronologiquement, applique la logique de signal configurable et produit des statistiques de trades simulés ; mode `--sweep` pour une recherche en grille (5×3×3×3 = 135 combinaisons de paramètres triées par taux de victoire), `--detail` pour un tableau trade par trade, et `--compare` pour afficher les résultats réels du bot en regard de la simulation ; configurable via le dataclass `Params` (`signal_threshold`, `entry_max`, `min_secs_remaining`, `min_ask_vol`, `win_threshold`, `loss_threshold`, `obi_reject_thresh`, `stake`, `daily_stop_loss`)
- `tests/test_backtest.py` — 28 tests pour le moteur de backtest : `TestFeeHelper` (2), `TestRunBacktestBasic` (14, couvrant toutes les gardes du signal et les chemins de résolution), `TestRunBacktestMultiMarket` (4, marchés indépendants, isolation de direction, résolution à l'expiration), `TestRunBacktestDailyStopLoss` (1), `TestRunBacktestParams` (3, sensibilité aux seuils win/loss), `TestSummarize` (6, drawdown, taux de victoire, comptage des trades ouverts)
- `tests/test_bot.py` — suite de tests automatisés (71 tests, aucun service externe requis) : `TestComputeFee` (4), `TestParseBookMessage` (14), `TestMarketHelpers` (9), `TestTokenState` (7), `TestRegisterMarket` (5), `TestCheckSignal` (13 gardes dont les 8 conditions d'entrée, le stop-loss journalier et la prévention des doublons), `TestCheckResolution` (7), `TestCloseTrade` (6), `TestRestoreState` (5) ; tous les tests utilisent une base SQLite en mémoire et un `TRADINEBOTTE_DIR=/tmp/polymarket-test` fixe pour ne jamais toucher les fichiers de production
- `scripts/run_tests.sh` — le lanceur de tests supprime désormais les `ResourceWarning` Python 3.13 pour les connexions SQLite en mémoire non fermées (`-W ignore::ResourceWarning`) ; suite totale : 99 tests
- `config.json` / `config.json.example` — nouvelle clé optionnelle `db_mmap_mb` (entier, défaut `0`) : quand elle est non nulle, active `PRAGMA mmap_size` pour que SQLite mappe le fichier de base de données via le page cache du kernel ; mettre à ex. `256` pour 256 Mo
- `bot/live_bot.py` — `load_config()` refactorisée pour retourner le dict de config complet (extensible pour de futures options) ; `DB_MMAP_MB` dérivé de la config au démarrage ; `init_db()` applique le pragma et enregistre une ligne de confirmation dans les logs quand le mmap est actif

### Documentation
- `README.md` — nouvelle section "Database" : justification SQLite/WAL, schéma complet de la table `trades` (29 colonnes avec type et description), schéma de la table `snapshots`, et 4 exemples de requêtes commentés
- `README.fr.md` — traduction française de la nouvelle section Base de données
- `bot/live_bot.py` — docstring du module traduit en anglais ; docstrings ajoutées à toutes les fonctions et classes ; commentaires inline expliquant les invariants non évidents : rôle du filtre temporel, les 8 gardes du signal (dont la garde `ask_vol=0` d'initialisation et la garde `best_ask>=1.0` pour les marchés expirés), la formule OBI, le calcul du PnL, la résolution dynamique du chemin sysconfig, l'import paresseux de ClobClient, le backoff exponentiel, le nettoyage des marchés expirés, et le mode journal WAL
- `scripts/setup.py` — docstring du module traduit en anglais ; commentaires inline expliquant les décisions de sécurité (getpass, approbations ERC-20 à montant exact), les paramètres du swap Uniswap V3 (fee tier 100, garde slippage 0,5%, deadline 5 min), le chemin dynamique sysconfig, la dérivation ECDSA des clés API, et le chmod 600

### Fonctionnalité
- La variable d'environnement `TRADINEBOTTE_DIR` contrôle désormais le chemin d'installation dans tous les scripts et dans le bot, avec `/opt/polymarket-live` comme valeur par défaut
- `scripts/install.sh` — accepte le répertoire d'installation en argument positionnel ou via `TRADINEBOTTE_DIR` ; génère un wrapper `run.sh` dans le répertoire d'installation avec le chemin pré-défini
- `scripts/start_bot.sh` — lit `TRADINEBOTTE_DIR` et l'exporte lors du lancement du bot
- `scripts/monitor.sh` — lit `TRADINEBOTTE_DIR` pour les chemins des logs et de la base de données
- `scripts/setup.py` — lit `TRADINEBOTTE_DIR` pour le chemin de `config.json` et les site-packages du venv ; corrige également le chemin venv codé en dur pour Python 3.12 (utilise `sysconfig` comme le bot)
- `bot/live_bot.py` — `DB_PATH`, `LOG_PATH`, `CONFIG_PATH` et la recherche du venv sont tous dérivés de `TRADINEBOTTE_DIR`

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
