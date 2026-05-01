# Journal des modifications

> 🇬🇧 [English version](CHANGELOG.md)

Toutes les modifications notables de ce projet sont documentées ici.

---

## [Non publié]

---

## [0.3] - 2026-05-01

### Correction de bug
- **`scripts/start_bot.sh`** — le message de lancement et le chemin du log s'affichaient avec le chemin absolu `$HOME` ; remplacé par des chemins relatifs avec `~` via substitution bash (`${VAR/$HOME/\~}`)
- **`scripts/start_bot.sh`** — utilisait `python3` système au lieu du `python3` du virtualenv ; le bot crashait immédiatement car aiohttp/web3/etc. sont installés dans le venv, pas dans le Python système ; corrigé pour utiliser `$INSTALL_DIR/venv/bin/python3` ; la sortie `nohup` est désormais redirigée vers `live.log` au lieu de `/dev/null` pour que les erreurs de démarrage soient visibles dans le tail du log ; ajout d'une vérification d'existence du venv avec un message d'erreur clair

### Amélioration
- **`scripts/setup.py`** — appuyer sur Entrée sans clé privée crée désormais un `config.json` de simulation (credentials vides) et sort proprement ; les imports blockchain sont entièrement ignorés dans ce chemin ; le prompt mentionne l'option ; docs QUICKSTART + INSTALL mis à jour (EN + FR)
- **`scripts/install.sh`** — n'appelle plus `apt-get` directement ; détecte à la place les paquets système manquants (`python3`, `python3-venv`, `python3.X-venv`, `sqlite3`) et affiche la commande `sudo apt-get install` exacte avec le numéro de version Python auto-détecté ; sort en erreur si quelque chose manque, continue silencieusement si tout est présent ; docs mis à jour dans INSTALL, QUICKSTART, README (EN + FR)

### Qualité de code
- `bot/account_bot.py` — pylint 10/10 : import `json` inutilisé supprimé ; `open(lock_file)` et `open(log_path)` portent désormais un encodage explicite (`utf-8`) ou le mode binaire (`ab`) ; les usages intentionnels sans `with` sont annotés `# pylint: disable=consider-using-with` ; `# pylint: disable=duplicate-code` ajouté au niveau module (boucle de purge des marchés expirés identique à `feed.py` par conception)
- `bot/account_bot.py` — ResourceWarning corrigé : `_run()` encapsule désormais sa boucle d'événements dans un `try/finally` pour appeler `sock.close(linger=0)` et `ctx.term()` à l'annulation ; le socket et le contexte ZMQ sont garantis d'être libérés lors de l'annulation de la tâche en test ou à l'arrêt

### Tests
- **`scripts/test_multibot_deploy.sh`** — deux bogues corrigés : (1) `grep -c || echo 0` produisait `"0\n0"` car `grep -c` imprime toujours le compteur sur stdout avant de quitter avec code 1 quand aucun résultat, et `|| echo 0` ajoutait alors un second zéro — causant l'échec de la comparaison arithmétique `[[ -eq ]]` avec une erreur de syntaxe ; corrigé en remplaçant `|| echo 0` par `|| true` (grep a déjà imprimé le compteur) ; (2) `ELAPSED=20` n'avait pas été mis à jour quand l'attente de stabilisation Phase 4 avait été portée de 20 à 30 s, faisant tourner la boucle heartbeat une itération de trop en Phase 6 ; corrigé en `ELAPSED=30`
- **`scripts/test_multibot_deploy.sh`** — test d'intégration de bout en bout pour le multi-bot Option B sur des comptes de test configurables : Phase 1 nettoyage (tue les processus, supprime les répertoires, efface les lock files), Phase 2 déploiement (rsync + création venv + pip install sans root), Phase 3 lancement simultané des N account_bots en mode `--verbose` pour stresser le démarrage automatique du feed avec verrou sans race condition, Phase 4 opération soutenue avec heartbeats toutes les 30s, Phase 5 analyse des logs (confirmation WebSocket du feed, nombre de book updates par bot, comptage des lignes ERROR/CRITICAL), Phase 6 teardown et comptage final des processus ; sort 0 en cas de succès total, 1 en cas d'échec ; `--skip-deploy` réutilise une installation existante ; `--duration N` remplace les 3 minutes par défaut ; l'adresse serveur, le port SSH, les noms d'utilisateur et les mots de passe sont lus depuis `~/.tradinebotte-test.conf` (ou la variable `TEST_MULTIBOT_CONF`) — jamais codés en dur ; `scripts/test_multibot.conf.example` fournit le template

### Données
- **`data/basicsunday.db`** — 24 870 snapshots d'une session de simulation live ~26h (2026-04-25 20:57 → 2026-04-26 22:55), 312 marchés distincts ; résultat backtest : 42/43 victoires (97,7 %), PnL -3,58 $ ; combiné avec `calmsaturday.db` via `--all` : 52 trades, 51 victoires, **98,1 % de taux de victoire agrégé** ; `README.fr.md` mis à jour avec un tableau comparatif des jeux de données

### Documentation
- **Prérequis administrateur serveur** — confirmé en conditions réelles sur Ubuntu 22.04 / Python 3.10 : les trois paquets `python3-venv`, `python3-pip`, et `python3.10-venv` sont requis ; `python3.10-venv` est désormais un prérequis principal (non un fallback) dans `INSTALL.md`, `INSTALL.fr.md`, `QUICKSTART.md`, `QUICKSTART.fr.md`, `README.md`, `README.fr.md` ; sans lui la création du venv échoue avec *"ensurepip is not available"*

### Fonctionnalité
- **Partage WebSocket multi-bot (Option B — ZeroMQ)** — `bot/feed.py` maintient une seule connexion WebSocket vers Polymarket et publie chaque mise à jour du carnet d'ordres via un socket ZeroMQ PUB (`tcp://127.0.0.1:5557` par défaut, configurable via `TRADINEBOTTE_FEED_ADDR`). `bot/account_bot.py` souscrit au feed et exécute la stratégie complète pour un compte en isolation. Plusieurs processus `account_bot.py` peuvent tourner en parallèle, chacun avec son propre `TRADINEBOTTE_DIR` (config, DB, log), sans ouvrir de connexion WebSocket supplémentaire. `scripts/start_feed.sh` et `scripts/start_account.sh` gèrent le lancement et les logs. `pyzmq` ajouté dans `requirements.txt`.
- **Filtre heure/jour** — nouveau bloc `hour_filter` dans `strategies/polymarket_BTC5M.json` (désactivé par défaut). Quand actif, restreint les entrées à des plages horaires UTC configurables par type de jour (semaine/weekend), avec gestion spéciale de l'ouverture hebdomadaire US (lundi avant 13h30 UTC) et de la fermeture (vendredi à partir de 20h00 UTC). Appliqué identiquement dans le bot live (garde `is_trading_hour()` dans `check_signal()`) et dans le moteur de backtest (`_is_trading_hour()` dans `run_backtest()`). 15 nouveaux tests unitaires.

### Correctif
- `bot/live_bot.py` — `--simulate` n'écrase plus `TRADINEBOTTE_DIR` si la variable est déjà définie dans l'environnement ; plusieurs bots peuvent désormais tourner en simulation parallèle avec des répertoires totalement isolés (`TRADINEBOTTE_DIR=~/compte-a python3 live_bot.py --simulate`)

### Documentation
- `INSTALL.md` / `INSTALL.fr.md` — nouvelle section « Filtre heure / jour » : tableau de justification (sessions asiatique/EU/US, ouverture/fermeture hebdomadaire), référence complète des paramètres, logique de décision pas-à-pas avec exemple du lundi, 3 configurations prêtes à l'emploi, workflow de validation par backtest, exemple de log au démarrage
- `README.md` / `README.fr.md` — nouveau bullet fonctionnalité pour le filtre horaire ; compteur de tests mis à jour à 123
- `docs/multi.md` / `docs/multi.fr.md` — documentation bilingue complète de l'architecture : guide de décision Option A vs B (quand utiliser chaque mode, tableau de tradeoffs), diagramme ASCII, référence des composants, évaluation indépendante du signal par compte, protocole de messages (3 types avec tous les champs), variables d'environnement, arborescence, séquence de lancement, monitoring par compte, modes de défaillance, déploiement cross-user (comptes Linux différents), ajout d'un troisième compte, tableau comparatif avec le mode autonome ; lié depuis README, INSTALL, QUICKSTART, feed.py, account_bot.py
- `QUICKSTART.md` / `QUICKSTART.fr.md` — section de choix de déploiement restructurée : format liste de définitions pour Option A/B, tableau de décision couvrant compte unique, deux wallets même/différents utilisateurs Linux, comparaison de stratégies, tradeoffs simplicité vs efficacité
- `tests/test_multibot.py` — 30 nouveaux tests pour `feed.py` et `account_bot.py` : 7 tests unitaires pour `feed.register_market()`, 9 tests unitaires pour `account_bot._register_from_market_msg()`, 8 tests d'intégration ZMQ async pour l'Option A (bot seul), 6 tests d'intégration ZMQ async pour l'Option B (deux bots simultanés partageant le même feed) ; compteur total porté à 153

- **Mode diagnostic `--verbose`** — `bot/feed.py` et `bot/account_bot.py` acceptent tous deux `--verbose` ; active le niveau DEBUG et émet des traces détaillées : messages WebSocket bruts (tronqués à 200 caractères), chaque publication ZMQ PUB avec les champs clés, résultats et timing des sondes ZMQ, étapes de la course au verrou dans `_ensure_feed()` et boucle d'attente seconde par seconde, comparaisons de seuil de signal sur les books, tokens inconnus ignorés, enregistrements de marchés, paramètres d'init au démarrage ; le sous-processus `feed.py` hérite de `--verbose` quand `account_bot.py` est démarré avec ce flag ; la sortie INFO normale est inchangée sans le flag

- **Démarrage automatique du feed dans account_bot.py** — `_ensure_feed()` sonde l'adresse du feed pendant 5 s au démarrage ; si inaccessible, acquiert un verrou exclusif (`/tmp/tradinebotte-feed-<hash>.lock`) et lance `feed.py` en sous-processus, attendant jusqu'à 30 s qu'il soit prêt avant de libérer le verrou ; les account_bots concurrents qui perdent la course se bloquent sur un verrou partagé et se connectent dès que le gagnant libère le verrou — tous les bots peuvent désormais être démarrés simultanément sans gestion manuelle du feed ; les logs du feed vont dans `/tmp/tradinebotte-feed-<hash>.log` ; `docs/multi.md`, `QUICKSTART.md` mis à jour pour refléter la séquence de lancement simplifiée
- **Services systemd pour le multi-bot (Option B)** — `scripts/tradinebotte-feed.service` et `scripts/tradinebotte-account.service` sont des templates d'unité pour le feed ZeroMQ et les bots par compte. `scripts/install_feed_service.sh` auto-détecte le virtualenv (`.venv` pour dev, `venv` pour prod), valide `feed.py` et `pyzmq`, génère un service système prêt à installer avec `User=`, `WorkingDirectory=`, `ExecStart=`, et `TRADINEBOTTE_FEED_ADDR=`. `scripts/install_account_service.sh` dérive le nom du service depuis le basename du répertoire de compte (`tradinebotte-account-<nom>`), applique la même auto-détection du venv, et déclare `Requires=tradinebotte-feed.service` pour que systemd impose l'ordre de démarrage/redémarrage. Les deux scripts affichent les commandes `sudo cp / daemon-reload / enable / start` exactes. `docs/multi.md` et `docs/multi.fr.md` mis à jour avec un guide d'installation systemd complet.

### Précédent
- `scripts/tradinebotte.service` — template d'unité systemd : `After=network-online.target`, `Restart=on-failure`, `RestartSec=30`, `StartLimitBurst=5` (max 5 redémarrages par 5 min) ; les placeholders `__USER__` et `__TRADINEBOTTE_DIR__` sont substitués à l'installation
- `scripts/install_service.sh` — script générateur : lit `TRADINEBOTTE_DIR` (ou utilise `~/tradinebotte` par défaut), valide que l'installation existe, substitue les placeholders avec `sed`, écrit dans `/tmp/tradinebotte.service` et affiche les quatre commandes `sudo` pour activer le service

### Qualité de code
- `bot/live_bot.py` — mypy strict : 0 erreur ; ajout d'annotations de type explicites pour `_log_handlers: list[logging.Handler]`, `_log_queue: queue.Queue[logging.LogRecord]` et les cinq attributs dict/set de `BotState.__init__` (`tokens`, `market_tokens`, `open_trades`, `traded_direction`, `signalled`) ; `cur.lastrowid or 0` protège le type de retour `int | None`
- `tests/test_bot.py` — ResourceWarning corrigé : suppression du `warnings.filterwarnings` global ; les sept classes de test créant des connexions SQLite utilisent désormais `setUp`/`tearDown` ou `self.addCleanup(conn.close)` ; aucun avertissement de connexion non fermée sur Python 3.13
- `.github/workflows/mypy.yml` — nouveau workflow CI : exécute `mypy bot/live_bot.py bot/api_polymarket.py --ignore-missing-imports` à chaque push et pull request (Python 3.12)
- `requirements-dev.txt` — `mypy` ajouté

### Documentation
- `QUICKSTART.md` / `QUICKSTART.fr.md` — nouveau guide de démarrage rapide bilingue : cinq commandes (clone, install, setup, start, monitor) couvrant le chemin minimal du zéro à un bot opérationnel ; inclut une note sur le mode simulation et la commande d'arrêt ; croisé depuis `README`, `INSTALL` et `CLAUDE.md`
- `CLAUDE.md` — règle de documentation bilingue étendue de 6 à 8 fichiers pour inclure `QUICKSTART.md` / `QUICKSTART.fr.md`

### Fonctionnalité
- `scripts/backtest.py` — backtest multi-fichiers : `--db` accepte désormais un ou plusieurs chemins (expansion de glob shell, ex. `--db data/*.db`) ; nouveau flag `--all` qui scanne le répertoire `data/` pour tous les fichiers `.db` et ajoute `live.db` en tête s'il contient ≥ 100 snapshots ; le capital se réinitialise à `capital_start` indépendamment par fichier ; le bloc BACKTEST par fichier affiche le nom du fichier quand plusieurs fichiers sont traités ; un bloc AGGREGATE (wins/losses/PnL/taux de victoire/pire drawdown combinés) est affiché après tous les fichiers lorsqu'il y en a plus d'un et que `--sweep` n'est pas actif

### Sécurité
- `requirements.txt` / `requirements-dev.txt` — manifestes de dépendances : toutes les dépendances runtime (`aiohttp`, `websockets`, `web3`, `py-clob-client`) et dev (`pylint`, `pip-audit`) sont désormais déclarées dans des fichiers versionnés ; tous les workflows CI installent depuis ces fichiers plutôt que de lister les paquets en dur
- `.github/workflows/audit.yml` — nouveau workflow CI : exécute `pip-audit -r requirements.txt` à chaque push et chaque lundi à 06 h 00 UTC pour détecter les CVE connus dans les dépendances runtime avant qu'elles n'atteignent la production
- `.github/dependabot.yml` — Dependabot activé pour les paquets `pip` et les `github-actions` ; crée des PRs automatiques chaque lundi lorsque des versions plus récentes sont disponibles

### Qualité de code
- `bot/live_bot.py` — annotations de types complètes : les 28 fonctions et méthodes de classe portent désormais des annotations de paramètres et de retour (`dict[str, Any]`, `list[str]`, `Optional[float]`, `sqlite3.Connection`, `aiohttp.ClientSession`, etc.) ; le paramètre websocket `ws` utilise `Any` pour rester compatible avec les différentes versions de la bibliothèque websockets
- `tests/test_bot.py` — 9 nouveaux tests ajoutés (71 → 80) : `TestHtpasswd` (préfixe SHA1, valeur connue, collision), `TestGenerateStatusHtml` (capital, table, taux de victoire, état vide), `TestHandleBookUpdate` (mise à jour d'état depuis un message parsé, token inconnu ignoré) ; utilise SQLite en mémoire et `unittest.IsolatedAsyncioTestCase` — aucun réseau ni credentials requis
- `.github/workflows/tests.yml` — nouveau workflow CI : exécute `unittest discover` sur Python 3.10, 3.11 et 3.12 à chaque push ; suite totale : 108 tests (80 bot + 28 backtest)

### Fonctionnalité
- `bot/live_bot.py` — mesure de latence : chaque trade émet une ligne `[LATENCY]` avec `signal_ms` (message WebSocket reçu → décision d'ordre, inclut tous les gardes du signal et la requête SQLite PnL journalier) et `order_rtt_ms` (round-trip HTTP API CLOB) ; les timestamps utilisent `time.monotonic()` et sont passés en paramètre optionnel `_t_ws` à travers `handle_book_update` → `check_signal` → `enter_live_trade` ; zéro surcharge sur les messages sans trade
- `scripts/latency.py` — nouvel outil d'analyse : parse les lignes `[LATENCY]` de `live.log` et affiche min / mean / p50 / p90 / p99 / max pour signal_ms, order_rtt_ms et total_ms ; usage : `python3 scripts/latency.py [logfile]`

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
