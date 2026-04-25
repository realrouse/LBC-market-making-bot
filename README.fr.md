# tradinebotte

> 🇬🇧 [English version](README.md)

Bot de trading automatisé pour les marchés de prédiction [Polymarket](https://polymarket.com), ciblant les marchés Bitcoin Hausse/Baisse 5 minutes sur Polygon. Utilise une stratégie quantitative basée sur un signal (`best_bid >= 0.96`) backtestée à **98,3% de taux de victoire** sur 1663 trades (avril 2026).

## Fonctionnalités

- **Stratégie quantitative** — entrée sur `best_bid >= 0.96`, backtestée à 98,3% de victoires sur 1663 trades
- **Flux WebSocket temps réel** — souscrit aux carnets d'ordres Polymarket ; traite chaque mise à jour bid/ask avec une latence inférieure à la seconde
- **Découverte automatique des marchés** — interroge l'API Gamma toutes les 30 s en tâche de fond ; ne suit que les marchés expirant dans ±6 minutes pour éviter les prix figés
- **Résolution automatique des trades** — clôture les positions automatiquement en WIN (bid ≥ 0.99), LOSS (bid ≤ 0.01), ou à l'expiration du marché
- **Stop-loss journalier** — suspend le trading dès 30 $ de perte nette sur la journée ; reprend à la session suivante
- **Persistance SQLite** (mode WAL) — tous les trades et les snapshots de prix toutes les 5 s sont stockés ; l'état survit aux crashs et redémarrages
- **Reprise après crash** — restaure les trades non résolus depuis la base de données au démarrage ; reconstruit le capital à partir du PnL historique
- **Moteur de backtest** — rejoue les données `snapshots` avec n'importe quel jeu de paramètres ; supporte la recherche en grille sur 135 combinaisons ; `--db file1.db file2.db` ou `--db data/*.db` exécute des simulations de capital indépendantes sur plusieurs fichiers de snapshots ; `--all` scanne automatiquement `data/` et ajoute `live.db` en tête si utilisable ; taux de victoire et PnL agrégés affichés sur tous les fichiers ; utilise le jeu de données embarqué (`data/backtest_sample_btc5m_range_2026.db`) si aucune base de données live n'est présente
- **Page de statut HTML optionnelle** — le bot écrit une page auto-rafraîchissante (chemin configurable, authentification HTTP Basic Auth optionnelle) — [aperçu visuel](docs/status_example.html)
- **API exchange modulaire** — tout le code spécifique Polymarket est dans `bot/api_polymarket.py` ; changer d'exchange ne nécessite qu'un nouveau fichier adaptateur et une seule ligne d'import dans `live_bot.py`
- **Fichiers de stratégie JSON** — les paramètres de signal et de capital sont dans `strategies/polymarket_BTC5M.json` ; changer de stratégie se fait en pointant `"strategy"` dans `config.json` vers n'importe quel fichier
- **Mode simulation** — le flag `--simulate` isole tous les fichiers dans `/tmp/tradinebotte-sim`, affiche les logs dans le terminal, et ne place aucun ordre réel ; utilisable sur n'importe quelle machine sans toucher les données de production
- **Code annoté par types** — les 28 fonctions et méthodes de classe de `live_bot.py` portent des annotations de paramètres et de retour complètes ; active l'analyse statique et l'autocomplétion IDE
- **Suite de 108 tests** — `tests/test_bot.py` (80 tests) couvre les 11 gardes du signal, les chemins de résolution, le calcul des frais, le parsing WebSocket, la page de statut HTML, le hashage htpasswd et la restauration d'état ; `tests/test_backtest.py` (28 tests) couvre le moteur de replay de bout en bout ; aucun réseau ni credentials requis
- **Audit de sécurité continu** — `pip-audit` s'exécute à chaque push et chaque semaine pour détecter les CVE dans les dépendances runtime (`aiohttp`, `websockets`, `web3`, `py-clob-client`) ; Dependabot ouvre des PRs automatiques quand de nouvelles versions sont disponibles
- **Logging asynchrone + mesure de latence** — les écritures de logs ne bloquent jamais le event loop ; chaque trade émet une ligne `[LATENCY]` avec `signal_ms` (message WS → décision d'ordre) et `order_rtt_ms` (round-trip API CLOB) ; `scripts/latency.py` parse le log et affiche min/mean/p50/p90/p99/max pour chaque métrique ; un thread daemon `QueueListener` vide la queue de logs sur disque en arrière-plan ; ajouter `--no-log` pour supprimer entièrement le fichier log (la DB SQLite n'est pas affectée) pour un I/O disque minimal en production

## Stratégie

- Surveille les marchés "Bitcoin Up or Down — 5 minutes" dont `endDate` est dans une fenêtre de ±6 minutes
- Signal d'entrée : `best_bid >= 0.96` sur un token UP ou DOWN
- Exécute un ordre LIMIT BUY au `best_ask` via l'API CLOB de Polymarket
- Résolution : WIN si bid >= 0.99, LOSS si bid <= 0.01, ou à l'expiration du marché (bid >= 0.50 = WIN)
- Stop-loss journalier : 30 $ | Mise par trade : 10 $ | Frais : 2%

## Base de données

Le bot utilise **SQLite** (`live.db`) en mode journal WAL, qui autorise la lecture concurrente pendant les écritures (le script de monitoring peut interroger la base pendant que le bot enregistre des trades). Le fichier se trouve dans `TRADINEBOTTE_DIR/live.db` (par défaut : `~/tradinebotte/live.db`).

### Table : `trades`

Une ligne par trade. Toutes les conditions du signal au moment de l'entrée sont capturées avec le résultat de la résolution, ce qui permet une analyse complète après la session.

| Colonne | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Identifiant auto-incrémenté |
| `market_id` | TEXT | Condition ID Polymarket |
| `token_id` | TEXT | Token souscrit (UP ou DOWN) |
| `direction` | TEXT | `"UP"` ou `"DOWN"` |
| `question` | TEXT | Titre du marché (tronqué à 80 caractères) |
| `signal_ts_ms` | INTEGER | Horodatage Unix (ms) du signal |
| `signal_seconds_elapsed` | REAL | Secondes écoulées depuis l'ouverture du marché |
| `signal_secs_remaining` | REAL | Secondes restantes avant la clôture |
| `signal_best_bid` | REAL | Meilleure offre d'achat au moment du signal |
| `signal_best_ask` | REAL | Meilleure offre de vente (= prix d'entrée) |
| `signal_spread` | REAL | Spread à l'entrée |
| `signal_ask_vol` | REAL | Liquidité côté ask à l'entrée (USD) |
| `signal_obi` | REAL | Déséquilibre du carnet d'ordres à l'entrée (−1 à +1) |
| `entry_ts_ms` | INTEGER | Horodatage Unix (ms) de la soumission de l'ordre |
| `entry_price` | REAL | Prix limite soumis |
| `clob_order_id` | TEXT | Identifiant de l'ordre retourné par l'API CLOB |
| `stake` | REAL | USD engagés |
| `tokens_bought` | REAL | Quantité de tokens = stake / entry_price |
| `fee` | REAL | Frais protocole (2% × min(p, 1−p) × tokens) |
| `cost_total` | REAL | stake + fee |
| `resolved` | INTEGER | 0 = ouvert, 1 = résolu |
| `resolution_ts_ms` | INTEGER | Horodatage Unix (ms) de la résolution |
| `resolution_bid` | REAL | Meilleure offre d'achat au moment de la résolution |
| `outcome` | TEXT | `"WIN"` ou `"LOSS"` |
| `pnl_gross` | REAL | PnL brut avant frais |
| `pnl_net` | REAL | PnL net après frais protocole et gas |
| `pnl_roi_pct` | REAL | ROI en pourcentage de la mise |
| `capital_before` | REAL | Capital avant ce trade |
| `capital_after` | REAL | Capital après la résolution |
| `created_at` | INTEGER | Horodatage de création de la ligne (ms) |

### Table : `snapshots`

Snapshots de prix sauvegardés toutes les 5 secondes par token suivi, utilisés pour le graphisme et l'analyse de la stratégie après session.

| Colonne | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-incrémenté |
| `ts_ms` | INTEGER | Horodatage du snapshot (ms) |
| `market_id` | TEXT | Condition ID Polymarket |
| `token_id` | TEXT | Identifiant du token |
| `direction` | TEXT | `"UP"` ou `"DOWN"` |
| `secs_remaining` | REAL | Secondes restantes avant clôture |
| `best_bid` | REAL | Meilleure offre d'achat |
| `best_ask` | REAL | Meilleure offre de vente |
| `spread` | REAL | Spread |
| `ask_vol` | REAL | Profondeur ask top-5 (USD) |
| `obi` | REAL | Déséquilibre du carnet d'ordres |
| `has_open_trade` | INTEGER | 1 si un trade était ouvert à cet instant |

### Options de configuration

Les clés optionnelles suivantes peuvent être ajoutées à `config.json` :

| Clé | Type | Défaut | Description |
|---|---|---|---|
| `db_mmap_mb` | entier | `0` | Mappe le fichier de base de données en mémoire pour des lectures plus rapides. `0` = désactivé. Mettre à ex. `256` pour mapper jusqu'à 256 Mo via le page cache du kernel. Le système garde déjà le fichier en RAM pour cette charge de travail — option facultative. |
| `webstatuspage_html` | booléen | `false` | Active la page de statut HTML statique. Si `true`, le bot écrit la page dans `webstatuspage_path` toutes les 5 minutes et après chaque résolution de trade. Nécessite un serveur web pointant vers le répertoire HTML — voir `INSTALL.fr` pour les prérequis complets. |
| `webstatuspage_path` | chaîne | `~/public_html/tradinebot_status.html` | Chemin sur le système de fichiers pour la page HTML. `~` est développé en répertoire personnel. Le répertoire est créé automatiquement s'il n'existe pas. Pour le chemin par défaut `~/public_html`, le module Apache `mod_userdir` doit être activé. |
| `webstatus_user` | chaîne | `"tradinebot"` | Identifiant pour la protection HTTP Basic Auth via `.htaccess`. Utilisé uniquement si `webstatus_password` est défini. |
| `webstatus_password` | chaîne | `""` | Mot de passe HTTP Basic Auth. Si vide, aucun `.htaccess` n'est créé et la page est publiquement accessible. Si défini, le bot écrit un `.htaccess` dans le répertoire HTML et un `.htpasswd` (format Apache `{SHA}`) dans `TRADINEBOTTE_DIR/.webstatus_htpasswd` (hors de la racine web). **Prérequis Apache :** modules `mod_auth_basic` + `mod_authn_file` activés, `AllowOverride AuthConfig` sur le répertoire HTML, et permission de lecture sur le `.htpasswd` pour le processus Apache (`www-data`). **nginx :** ne traite pas les `.htaccess` — configurer `auth_basic` manuellement dans le bloc server en pointant vers le même fichier `.htpasswd`. |

### Requêtes utiles

```bash
# Derniers trades
sqlite3 live.db "SELECT id, direction, outcome, pnl_net, capital_after
                 FROM trades ORDER BY id DESC LIMIT 10;"

# Résumé de session
sqlite3 live.db "SELECT COUNT(*) total,
                        SUM(CASE WHEN outcome='WIN' THEN 1 END) wins,
                        SUM(CASE WHEN outcome='LOSS' THEN 1 END) losses,
                        ROUND(SUM(pnl_net), 2) net_pnl
                 FROM trades WHERE resolved=1;"

# Positions ouvertes
sqlite3 live.db "SELECT id, market_id, direction, entry_price, signal_ts_ms
                 FROM trades WHERE resolved=0;"

# Historique de prix d'un token
sqlite3 live.db "SELECT ts_ms, best_bid, best_ask, obi
                 FROM snapshots WHERE token_id='<id>'
                 ORDER BY ts_ms DESC LIMIT 100;"
```

## Installation

Voir **[INSTALL.fr.md](INSTALL.fr.md)** pour le guide d'installation complet : prérequis, dépendances, configuration du wallet, lancement, monitoring, et comment tester dans un environnement virtuel.

## Tests

```bash
bash scripts/run_tests.sh
```

La suite exécute 108 tests (80 pour le bot live, 28 pour le moteur de backtest) couvrant : le calcul des frais, le parsing des messages WebSocket, le calcul de l'OBI, l'enregistrement des marchés, les 11 gardes d'entrée du signal (dont le stop-loss journalier), la résolution des trades (WIN/LOSS/expiration), le calcul du PnL, la restauration d'état après un crash, le hashage SHA1 htpasswd, le rendu de la page de statut HTML, la mise à jour d'état asynchrone, et tous les chemins signal/résolution/paramètres du backtest. Aucun accès réseau ni credentials nécessaires — une base SQLite en mémoire est utilisée pour chaque test.

## Backtest

Rejouer les données `snapshots` historiques avec des paramètres de stratégie configurables.
Si `TRADINEBOTTE_DIR/live.db` est absent ou contient moins de 100 snapshots, le script utilise automatiquement le jeu de données embarqué (`data/backtest_sample_btc5m_range_2026.db`, 2430 snapshots issus de vrais marchés BTC 5 minutes collectés le 2026-04-25). Le fichier sélectionné est affiché au démarrage.

```bash
python3 scripts/backtest.py                        # paramètres par défaut
python3 scripts/backtest.py --threshold 0.95       # seuil personnalisé
python3 scripts/backtest.py --detail               # tableau trade par trade
python3 scripts/backtest.py --compare              # comparaison avec les trades réels
python3 scripts/backtest.py --sweep                # recherche en grille (135 combinaisons)
python3 scripts/backtest.py --db data/s1.db data/s2.db  # fichiers explicites
python3 scripts/backtest.py --db data/*.db         # glob shell (capital indépendant par fichier)
python3 scripts/backtest.py --all                  # scanne data/ + live.db si ≥ 100 snapshots
TRADINEBOTTE_DIR=~/mybot python3 scripts/backtest.py # chemin de base de données personnalisé
```

## Notes

- Le CLI `sqlite3` est optionnel — le bot utilise le module Python intégré. L'installer (`sudo apt install sqlite3`) uniquement pour les requêtes manuelles. Sans sudo : `~/tradinebotte/venv/bin/python3 -c "import sqlite3; c=sqlite3.connect('live.db'); print(c.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0])"`

- Les timeouts recv WebSocket (~30s) en période calme sont **normaux** — les keepalives `ping_interval=20` maintiennent la connexion ; le bot ne se reconnecte que si tous les marchés suivis ont expiré
- Le refresh des marchés (polling de l'API Gamma toutes les 30s) s'exécute en **tâche async de fond**, de sorte que le traitement des messages WebSocket n'est jamais bloqué pendant les appels HTTP
- La requête API Gamma utilise `tag_id=102892` (le tag `5M`) pour pré-filtrer côté serveur aux seuls marchés 5 minutes, réduisant chaque poll de potentiellement des milliers de marchés à ~12–20 en **un seul appel API** (sans pagination)
- Si `POLY_PRIVATE_KEY` n'est pas défini, les ordres sont simulés (aucune exécution on-chain)
- Les signaux peuvent être rares en période de faible volatilité BTC — c'est attendu
- Ne pas modifier `SIGNAL_THRESHOLD` (0.96) sans relancer le backtest complet

## Licence

Voir [LICENSE](LICENSE).
