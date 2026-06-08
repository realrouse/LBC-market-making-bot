# tradinebotte

> 🇬🇧 [English version](README.md)

Bot de trading automatisé pour les marchés de prédiction [Polymarket](https://polymarket.com), ciblant les marchés Bitcoin Hausse/Baisse 5 minutes sur Polygon. Utilise une stratégie quantitative basée sur un signal (`best_bid >= 0.95`) backtestée à **98,3% de taux de victoire** sur 1663 trades (avril 2026).

## Fonctionnalités

- **Stratégie quantitative** — entrée sur `best_bid >= 0.95`, backtestée à 98,3% de victoires sur 1663 trades
- **Flux WebSocket temps réel** — souscrit aux carnets d'ordres Polymarket ; traite chaque mise à jour bid/ask avec une latence inférieure à la seconde
- **Découverte automatique des marchés** — interroge l'API Gamma toutes les 30 s en tâche de fond ; ne suit que les marchés expirant dans ±6 minutes pour éviter les prix figés
- **Résolution automatique des trades** — clôture les positions automatiquement en WIN (bid ≥ 0.99), LOSS (bid ≤ 0.01), ou à l'expiration du marché
- **Stop-loss journalier** — suspend le trading dès 30 $ de perte nette sur la journée ; reprend à la session suivante
- **Persistance SQLite** (mode WAL) — tous les trades et les snapshots de prix toutes les 5 s sont stockés ; l'état survit aux crashs et redémarrages
- **Reprise après crash** — restaure les trades non résolus depuis la base de données au démarrage ; reconstruit le capital à partir du PnL historique
- **Moteur de backtest** — rejoue les données `snapshots` avec n'importe quel jeu de paramètres ; supporte la recherche en grille sur 135 combinaisons ; `--db file1.db file2.db` ou `--db data/*.db` exécute des simulations de capital indépendantes sur plusieurs fichiers de snapshots ; `--all` scanne automatiquement `data/` et ajoute `live.db` en tête si utilisable ; taux de victoire et PnL agrégés affichés sur tous les fichiers ; utilise `data/paper3.db` si aucune base de données live avec suffisamment de snapshots n'est présente ; `analysis/backtest.py` supporte désormais le sizing Kelly fractionnel, les ratios Sharpe et Sortino, l'optimisation walk-forward, le filtre de volatilité par jour de semaine (P1) et le sizing de mise par paliers (P2)
- **Page de statut HTML optionnelle** — le bot écrit une page auto-rafraîchissante (chemin configurable, authentification HTTP Basic Auth optionnelle) — [aperçu visuel](docs/status_example.html)
- **Partage WebSocket multi-bot** — `tradinebotte-polymarket/feed.py` maintient une seule connexion WebSocket et diffuse chaque mise à jour via ZeroMQ PUB ; un ou plusieurs processus `tradinebotte-polymarket/account_bot.py` souscrivent et tradent avec des bases SQLite, logs et configs totalement isolés ; chaque bot évalue les signaux avec **ses propres** paramètres de stratégie (seuils, mises ou filtres horaires différents) ; fonctionne entre utilisateurs Linux différents (`/home/user1`, `/home/user2`) ; **feed auto-démarré** — le premier account_bot à se lancer démarre feed.py automatiquement via un verrou fichier sans race condition, aucune gestion manuelle du feed requise — voir [docs/multi.fr.md](docs/multi.fr.md) pour le guide de décision et la référence d'architecture complète
- **API exchange modulaire** — tout le code spécifique Polymarket est dans `tradinebotte-polymarket/api_polymarket.py` ; changer d'exchange ne nécessite qu'un nouveau fichier adaptateur et une seule ligne d'import dans `live_bot.py` ; les connecteurs Binance spot (`tradinebotte-cex/api_binance.py`), MEXC spot (`tradinebotte-cex/api_mexc.py`) et Bitstamp spot (`tradinebotte-cex/api_bitstamp.py`) sont inclus, implémentant l'interface identique avec signature HMAC-SHA256, calcul de l'OBI et mode simulation
- **Gestionnaire Binance Simple Earn Flexible** (`tradinebotte-cex/earn_manager.py`) — `EarnManager` place les USDT inactifs après une vente (`park_idle()`) et les rachète avant un achat (`ensure_liquid()`) ; découverte automatique du produit et rapport du taux annuel ; mode simulation si les identifiants sont absents ; MEXC Earn non pris en charge (API trop instable)
- **Service d'indicateurs techniques** (`tradinebotte-indicators/indicators.py`) — étage pipeline ZeroMQ : souscrit au PUB de feed.py (port 5557), calcule RSI, SMA, EMA et volatilité glissante sur la série `best_bid` de chaque token, republie des messages `{"t":"indicators"}` enrichis sur un second socket PUB (port 5559) ; maths en stdlib pur (sans numpy) ; les consommateurs souscrivent au port 5559 pour recevoir les indicateurs en temps réel en parallèle des données de carnet ; source `binance_scalping` combinant les streams Binance depth20 + aggTrade pour calculer OBI, EMA, décélération, `spread_bps`, `realized_vol_bps` et TFI en temps réel ; configuration unifiée en 9 flux dans `tradinebotte-indicators/strategies/indicators_all.json` (PUB 5559, REP 5561) ; le flux `btc_4h` inclut EMA(50), EMA(200) et ATR(14) pour les consommateurs de la stratégie swing ; les trois boucles WebSocket Binance sont protégées par un watchdog de 120 secondes sur `ws.recv()` (`asyncio.wait_for`, constante `_WS_RECV_TIMEOUT_S`) pour éviter les blocages indéfinis ; les liquidations sont publiées via le WebSocket public `wss://fstream.binance.com/ws/{symbol}@forceOrder` — aucun credential API requis ; flux full-depth futures perpétuels (`btc_full_depth_perp`) aux côtés du flux spot (`btc_full_depth`), avec paramètre `market` (`"spot"` ou `"perp"`), réduction dynamique du carnet via `bid_depth_pct` / `ask_depth_pct`, et validation de synchronisation futures correcte par chaînage `pu` ; base de données SQLite partagée du carnet d'ordres optionnelle avec les fonctions `_init_depth_db()` / `_write_depth_to_db()` — la table `orderbook_current` stocke le dernier carnet bucketisé, la table `orderbook_snapshots` fournit un ring-buffer de snapshots horodatés ; activée par flux via `db_path`, `bucket_size_usd` (défaut 50), `db_write_every_n` (défaut 60), `history_retention_h` (défaut 24) ; fichier DB créé avec les droits `0o644` pour un accès en lecture multi-utilisateur ; les écritures sont déléguées via `run_in_executor` pour ne pas bloquer l'event loop async
- **Grid trading** — stratégie grid spot BTC/USDT pour Binance/MEXC : place des ordres d'achat sur N niveaux équidistants, collecte le profit sur chaque cycle BUY→SELL ; trois modes : `static` (arrêt quand le prix sort de la plage), `trail=bear` (recentrage vers le bas — sortie rentable sur rebond ; −3,3 % → +2,0 % sur le crash LUNA 2022), `trail=bull` (recentrage vers le haut — capture la hausse complète ; +0,1 % → +3,7 % sur la bull run 2024) ; `trail_mode` peut être défini directement dans le JSON de stratégie pour survivre aux redémarrages ; backtesté sur trois régimes BTC de 90 jours (latéral 2026, bear 2022, bull 2024) ; voir [`docs/AdaptedGridTrading.fr.md`](docs/AdaptedGridTrading.fr.md)
- **Bot de scalping OBI Binance** (`tradinebotte-cex/orderbook_bot.py`) v2.12 — connexion aux streams WebSocket depth20 spot et perpétuel de Binance (100 ms), calcul de l'OBI sur les N meilleurs niveaux bid/ask avec lissage EMA ; long-only depuis v2.4 ; évolutions successives : v2.3 filtre TFI, v2.5 TP/SL calibrés, v2.7 gate VWAP, v2.9 gate profil de volume, v2.10 gate macro OBI multi-timeframe, v2.12 gate liquidations (`liq_gate`, désactivée par défaut) ; tous les paramètres surchargeables via JSON ; TP 15 bps, SL 8 bps, durée maximale 3 minutes ; mode simulation d'ordres à cours limité avec identifiants préfixés `sim_` ; snapshots et trades enregistrés dans `live_ob.db` ; configuré via `tradinebotte-cex/strategies/scalping/orderbook_btc.json` (`entry_thresh`, `confirm_n`, `tp`, `sl`, `n_levels`, `liq_gate`, `liq_long_block_usd`)
- **Stratégie swing trading** (`tradinebotte-cex/strategy_engines/swing.py`) — moteur `SwingStrategy` qui place des ordres limit BUY sur des niveaux de support et des ordres SELL sur des résistances ; filtre directionnel EMA(200) 4h — les achats sont ignorés quand le prix est sous l'EMA 200 périodes ; stop-loss dynamique ATR(14) ; filtre RSI(14, 4h) de surachat ; souscription au service d'indicateurs partagé (ZMQ SUB) ; persistance SQLite avec restauration des positions au redémarrage ; configurée via `tradinebotte-cex/strategies/swing/swing_BTCUSDT.json` (`supports`, `resistances`, `position_size`, `max_positions`, `atr_sl_multiplier`) ; déploiement via `tradinebotte-cex/scripts/update_swing.sh`
- **Bibliothèque mathématique partagée** (`tradinetools/tradinetools/math.py`) — ATR, bandes de Bollinger, VWAP, z-score de volume et maximum glissant (`sma_last`, `ema_last`, `atr_last`, `bollinger_last`, `vwap_last`, `vol_zscore_last`, `rolling_max_last`) ; installée comme package éditable (`pip install -e tradinetools/`) ; partagée entre les quatre sous-services
- **Benchmark de latence API** — `analysis/benchmark_api.py` mesure le temps aller-retour REST et le temps jusqu'au premier message WebSocket sur les trois exchanges ; rapporte min/mean/p50/p90/p99/max/σ ; options `--rounds N` et `--no-ws`
- **Interface bilingue** — `scripts/setup.py`, `install.sh`, `start_bot.sh` et `monitor.sh` proposent `[E] English / [F] Français` au démarrage ; le choix est persisté sous la clé `"lang"` dans `config.json` et hérité automatiquement par les scripts suivants
- **Fichiers de stratégie JSON** — les paramètres de signal et de capital sont dans `tradinebotte-polymarket/strategies/polymarket_BTC5M.json` ; changer de stratégie se fait en pointant `"strategy"` dans `config.json` vers n'importe quel fichier ; `tradinebotte-polymarket/strategies/polymarket_BTC5M_piste3.json` ajoute la mise proportionnelle (`bid_alpha`), le rejet OBI (`obi_reject_thresh`) et le stop-loss hebdomadaire (`weekly_stop_loss`) ; backtest comparé à l'original : PnL +85 %, MaxDD −28 %, Sharpe 3,28 vs 1,97
- **Stratégie BTC cycle long terme** — trois configs prêtes pour la production : `tradinebotte-cex/strategies/longterm/longtermcyclestrategygridV1.json` (rebond 5 %/tranche 25 %, ×24,0, Calmar 0,54), `V2.json` (4 %/20 %, ×24,2, Calmar 0,54), `V3.json` (paliers de prudence relatifs au halving T1/T2, Calmar 0,75) ; backtest via `analysis/backtest_cycle_strategy.py` avec les options `--top-mm`, `--rebound`, `--drawback`, `--tranche`, `--prudence`, `--compare` ; analyse de cycles via `analysis/analyze_btc_cycles.py` et `analysis/analyze_cycle_volatility.py`
- **Intervalle de marché configurable** — `market_tag_id` et `market_window_mins` dans le JSON de stratégie permettent de basculer entre les marchés BTC Hausse/Baisse 5 minutes et 15 minutes ; `tradinebotte-polymarket/strategies/polymarket_BTC15M_piste3.json` est livré prêt à l'emploi ; le log de démarrage confirme le tag actif et la fenêtre
- **Vérification de compatibilité connecteur/stratégie** — `validate()` dans `tradinebotte-cex/connectors/__init__.py` lève une `RuntimeError` au démarrage si le connecteur manque des méthodes requises par la stratégie choisie, en listant toutes les méthodes manquantes ; évite les erreurs silencieuses lors du changement de connecteur
- **Mode simulation** — le flag `--simulate` isole tous les fichiers dans `~/tradinebotte-sim` par défaut, affiche les logs dans le terminal, et ne place aucun ordre réel ; définir `TRADINEBOTTE_DIR` avant le lancement pour utiliser un répertoire personnalisé — plusieurs bots peuvent ainsi tourner en parallèle sans conflit de répertoire
- **Code annoté par types** — les 28 fonctions et méthodes de classe de `live_bot.py` portent des annotations de paramètres et de retour complètes ; active l'analyse statique et l'autocomplétion IDE
- **Filtre heure/jour** — bloc optionnel `hour_filter` dans le JSON de stratégie ; restreint les entrées à des plages horaires UTC configurables par type de jour (semaine/weekend), avec gestion intégrée de l'ouverture hebdomadaire US (lundi avant 13h30 UTC) et de la fermeture (vendredi à partir de 20h00 UTC) ; désactivé par défaut ; appliqué identiquement dans le bot live et le moteur de backtest ; voir [INSTALL.fr.md](INSTALL.fr.md#filtre-heure--jour) pour la documentation complète
- **Suite de tests — 1090 tests répartis en 14 suites** — `tradinebotte-polymarket/tests/test_bot.py` (360 tests) couvre les 11 gardes du signal, les chemins de résolution, le calcul des frais, le parsing WebSocket, la page de statut HTML, le hashage htpasswd, la restauration d'état, la logique du filtre horaire, la mise proportionnelle, le stop-loss hebdomadaire et la configuration de découverte des marchés ; `tests/test_backtest.py` (111 tests) couvre le moteur de replay, le sizing Kelly fractionnel, les ratios Sharpe/Sortino, l'optimisation walk-forward et le filtre de volatilité par jour de semaine ; `tradinebotte-polymarket/tests/test_multibot.py` (30 tests) couvre `feed.py` et `account_bot.py` avec intégration ZMQ round-trip ; `tradinebotte-polymarket/tests/test_regression.py` (25 tests) couvre la régression de performance et la cohérence des paramètres ; `tradinebotte-indicators/tests/test_indicators.py` (136 tests) couvre le pipeline d'indicateurs ; `tests/test_api_cex.py` (121 tests) couvre les adaptateurs Bitstamp, MEXC et Binance ; `tests/test_cycle_strategy.py` (41), `tests/test_grid_trail.py` (15), `tests/test_scalping.py` (52) couvrent les stratégies CEX ; `tradinebotte-cex/tests/test_earn_manager.py` (48), `tradinebotte-cex/tests/test_strategy_engines.py` (64) couvrent earn et moteurs de stratégie CEX ; `tradinetools/tests/test_math.py` (37), `tradinetools/tests/test_schemas.py` (32), `tradinetools/tests/test_zmq.py` (18) couvrent la bibliothèque partagée ; aucun réseau ni credentials requis
- **Services systemd user** — `tradinebotte-polymarket/scripts/install_service.sh` (Option A) et `tradinebotte-polymarket/scripts/install_feed_service.sh` / `tradinebotte-polymarket/scripts/install_account_service.sh` (Option B) génèrent des fichiers d'unité prêts à installer ; les bots redémarrent automatiquement en cas d'erreur ou de reboot (`Restart=on-failure`) ; le service feed utilise `RestartSec=10`, les bots de compte `RestartSec=30` ; `Requires=tradinebotte-feed.service` impose l'ordre de démarrage/redémarrage ; les déploiements multi-comptes utilisent des unités `~/.config/systemd/user/` gérées via `systemctl --user` — aucun sudo requis au moment du déploiement
- **Vérification de types mypy** — `mypy tradinebotte-polymarket tradinebotte-cex tradinebotte-indicators tradinetools --ignore-missing-imports` retourne 0 erreur ; workflow CI exécuté à chaque push et pull request
- **Bibliothèque partagée `tradinetools`** (package `tradinetools/`) — `zmq.py` (fabriques de sockets ZMQ), `schemas.py` (dataclasses de messages versionnés), `math.py` (helpers scalaires d'indicateurs : `sma_last`, `ema_last`, `atr_last`, `bollinger_last`, `vwap_last`, `vol_zscore_last`, `rolling_max_last`), `logging.py` ; installée comme package éditable (`pip install -e tradinetools/`) ; partagée entre les quatre sous-services
- **Stratégie DCA** (`tradinebotte-cex/strategy_engines/dca.py` — `DCAStrategy`) — achats DCA cadencés à intervalles configurables ; take-profit et stop-loss optionnel ; persistance SQLite ; configurée via `tradinebotte-cex/strategies/dca/btc_dca.json`
- **Stratégie SwingHold** (`tradinebotte-cex/strategy_engines/swinghold.py` — `SwingHoldStrategy`) — vend `sell_fraction` de la position à chaque niveau de résistance au-dessus de l'entrée ; conserve le reste pour une accumulation long terme ; stop-loss sur la totalité de la position restante ; configurée via `tradinebotte-cex/strategies/swing/btc_swinghold.json`
- **Bot d'accumulation BTC v1.5** (`tradinebotte-cex/accumulation_bot.py`) — achat sur creux OBI avec ratchet de profit progressif ; scale-in adaptatif avec trailing stop de rebuy et expiration ; buffer Earn configurable (`earn_buffer_usd`) ; gate VWAP sur l'achat initial uniquement (`vwap_gate_initial`) ; v1.5 ajoute quatre gates de signal : Fear & Greed (`fear_greed_gate`), liquidations (`liq_gate`), ratio Long/Short (`ls_ratio_gate`), RSI 4h (`rsi4h_gate`) ; tous les paramètres surchargeables via `tradinebotte-cex/strategies/accumulation/btc_accumulation.json` ; déploiement via `tradinebotte-cex/scripts/deploy_accumulation_claude4.sh`
- **Nouveaux scripts d'analyse** — `analysis/backtest_swing_dca.py` (backtester DCA/Swing/SwingHold ; options : `--compare`, `--all-dbs`, `--sweep`, `--config`) ; `analysis/backtest_orderbook.py` (replay scalping OBI) ; `analysis/calibrate_obi_proxy.py` (calibration des seuils OBI)
- **Script de test d'intégration** — `scripts/test_multibot_deploy.sh` automatise un test complet d'install propre et de bout en bout sur un ensemble configurable de comptes Linux de test : nettoyage, déploiement rsync, création du venv, lancement simultané des N bots en mode `--verbose` (stress-test du démarrage automatique du feed avec verrou sans race condition), monitoring par heartbeats toutes les 30s, analyse des logs (WebSocket, nombre de book updates, lignes d'erreur), teardown ; serveur, port, utilisateurs et mots de passe lus depuis `~/.tradinebotte-test.conf` (template : `scripts/test_multibot.conf.example`) ; `--skip-deploy` réutilise une install existante ; `--duration N` ajuste la fenêtre de test ; sort 0 en cas de succès total
- **Audit de sécurité continu** — `pip-audit` s'exécute à chaque push et chaque semaine pour détecter les CVE dans les dépendances runtime (`aiohttp`, `websockets`, `web3`, `py-clob-client`) ; Dependabot ouvre des PRs automatiques quand de nouvelles versions sont disponibles
- **Logging asynchrone + mesure de latence** — les écritures de logs ne bloquent jamais le event loop ; chaque trade émet une ligne `[LATENCY]` avec `signal_ms` (message WS → décision d'ordre) et `order_rtt_ms` (round-trip API CLOB) ; `analysis/latency.py` parse le log et affiche min/mean/p50/p90/p99/max pour chaque métrique ; un thread daemon `QueueListener` vide la queue de logs sur disque en arrière-plan ; ajouter `--no-log` pour supprimer entièrement le fichier log (la DB SQLite n'est pas affectée) pour un I/O disque minimal en production ; ajouter `--no-snapshots` pour ne pas écrire les snapshots de prix toutes les 5 s dans la DB (les trades continuent d'être enregistrés) — réduit la pression d'écriture sur les longues sessions ; ajouter `--snapshot-interval SECS` pour remplacer l'intervalle d'écriture des snapshots en secondes (défaut : 5 ; utiliser 1 pour le mode collecte de données) ; ajouter `--reset-db` pour sauvegarder `live.db` dans un fichier horodaté puis le supprimer avant le lancement, de sorte que le bot repart de zéro (capital et historique de trades) après confirmation interactive (sans effet si la DB est absente)
- **Collecte de données** (premier compte de déploiement — mode simulation, snapshots à 1 seconde) :
  - Déployer et lancer le collecteur :
    `bash tradinebotte-polymarket/scripts/start_collector.sh`           # déploiement + lancement
    `bash tradinebotte-polymarket/scripts/start_collector.sh --status`  # vérifier si en cours
    `bash tradinebotte-polymarket/scripts/start_collector.sh --stop`    # arrêter
  - Télécharger la base de données hebdomadaire :
    `bash tradinebotte-polymarket/scripts/collect_db.sh --status`       # compteurs distants de lignes
    `bash tradinebotte-polymarket/scripts/collect_db.sh --rotate`       # télécharger + archiver + redémarrer
  - Automatiser la collecte hebdomadaire (cron) :
    `bash tradinebotte-polymarket/scripts/schedule_collect.sh --install`   # tous les dimanches à 03:00 UTC
    `bash tradinebotte-polymarket/scripts/schedule_collect.sh --status`    # afficher l'entrée cron
    `bash tradinebotte-polymarket/scripts/schedule_collect.sh --run-now`   # exécuter immédiatement

## Stratégie

- Surveille les marchés "Bitcoin Up or Down — 5 minutes" dont `endDate` est dans une fenêtre de ±6 minutes
- Signal d'entrée : `best_bid >= 0.95` sur un token UP ou DOWN
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

**Nouvel utilisateur ?** Voir **[QUICKSTART.fr.md](QUICKSTART.fr.md)** — 5 commandes, bot opérationnel en quelques minutes.

Guide complet (prérequis, configuration du wallet, page de statut web, monitoring, tests) : **[INSTALL.fr.md](INSTALL.fr.md)**.

> **Note administrateur serveur :** `scripts/install.sh` détecte les paquets système manquants et affiche la commande `sudo apt-get install` exacte à exécuter en root — aucune recherche manuelle de paquets nécessaire. Voir [INSTALL.fr.md — Prérequis administrateur serveur](INSTALL.fr.md#prérequis-administrateur-serveur-debianubuntu).

## Tests

```bash
bash scripts/run_tests.sh
```

1090 tests répartis en 14 suites. La suite couvre : le calcul des frais, le parsing des messages WebSocket, le calcul de l'OBI, l'enregistrement des marchés, les 11 gardes d'entrée du signal (dont le stop-loss journalier), la résolution des trades (WIN/LOSS/expiration), le calcul du PnL, la restauration d'état après un crash, le hashage SHA1 htpasswd, le rendu de la page de statut HTML, la mise à jour d'état asynchrone, tous les chemins signal/résolution/paramètres du backtest, le sizing Kelly fractionnel, les ratios Sharpe/Sortino, l'optimisation walk-forward, le filtre de volatilité par jour de semaine, l'intégration ZMQ feed/account_bot avec un et deux bots simultanés, les flux subscribe/redeem/mode-sim de `EarnManager`, le backtest de la stratégie cycle long terme, le pipeline d'indicateurs et l'adaptateur Bitstamp. Aucun accès réseau ni credentials nécessaires — une base SQLite en mémoire est utilisée pour chaque test.

## Backtest

Rejouer les données `snapshots` historiques avec des paramètres de stratégie configurables.
La base est sélectionnée dans cet ordre : `$TRADINEBOTTE_DIR/live.db` (si ≥ 100 snapshots), puis `data/paper3.db` (session de paper trading, 764k snapshots). Le fichier sélectionné est affiché au démarrage.

Lancer plusieurs bases en une commande avec `--all` (scanne `data/` pour tous les fichiers `.db` et ajoute `live.db` si utilisable).

```bash
python3 analysis/backtest.py                        # paramètres par défaut
python3 analysis/backtest.py --threshold 0.95       # seuil personnalisé
python3 analysis/backtest.py --detail               # tableau trade par trade
python3 analysis/backtest.py --compare              # comparaison avec les trades réels
python3 analysis/backtest.py --sweep                # recherche en grille (135 combinaisons)
python3 analysis/backtest.py --sweep-all            # grille étendue (405 combos, toutes les BDs)
python3 analysis/backtest.py --sweep-all --sort pnl # trier par pnl|ratio|wr
python3 analysis/backtest.py --sweep-all --top 10   # top-10 configs uniques (dédupliqué)
python3 analysis/backtest.py --db data/s1.db data/s2.db  # fichiers explicites
python3 analysis/backtest.py --db data/*.db         # glob shell (capital indépendant par fichier)
python3 analysis/backtest.py --all                  # scanne data/ + live.db si ≥ 100 snapshots
TRADINEBOTTE_DIR=~/mybot python3 analysis/backtest.py # chemin de base de données personnalisé
```

## Backtest grid trading

Rejouer des données OHLCV BTC/USDT historiques contre une stratégie grid configurable. Modèle de remplissage : touche de prix sur l'intervalle `[low, high]` de la bougie. Nécessite des bases SQLite de bougies 1 minute dans `data/` — à télécharger avec `analysis/download_btc_history.py`.

```bash
python3 analysis/backtest_grid.py --all                           # grid statique, toutes les BDs
python3 analysis/backtest_grid.py --all --trail bear              # trailing bear-adapté
python3 analysis/backtest_grid.py --all --trail bull --compare    # trailing bull vs statique
python3 analysis/backtest_grid.py --all --sweep --sort pnl        # balayage de paramètres
```

Télécharger les données OHLCV historiques depuis Binance :

```bash
python3 analysis/download_btc_history.py                                          # 90 derniers jours
python3 analysis/download_btc_history.py --start 2022-05-01 --end 2022-08-01     # bear market
python3 analysis/download_btc_history.py --start 2024-10-15 --end 2025-01-15     # bull run 2024
```

Voir [`docs/AdaptedGridTrading.fr.md`](docs/AdaptedGridTrading.fr.md) pour la documentation complète des stratégies, les résultats de backtest et le guide de sélection.

## Notes

- Le CLI `sqlite3` est optionnel — le bot utilise le module Python intégré. L'installer (`sudo apt install sqlite3`) uniquement pour les requêtes manuelles. Sans sudo : `~/tradinebotte/.venv/bin/python3 -c "import sqlite3; c=sqlite3.connect('live.db'); print(c.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0])"`

- Les timeouts recv WebSocket (~30s) en période calme sont **normaux** — les keepalives `ping_interval=20` maintiennent la connexion ; le bot ne se reconnecte que si tous les marchés suivis ont expiré
- Le refresh des marchés (polling de l'API Gamma toutes les 30s) s'exécute en **tâche async de fond**, de sorte que le traitement des messages WebSocket n'est jamais bloqué pendant les appels HTTP
- La requête API Gamma utilise `tag_id=102892` (le tag `5M`) pour pré-filtrer côté serveur aux seuls marchés 5 minutes, réduisant chaque poll de potentiellement des milliers de marchés à ~12–20 en **un seul appel API** (sans pagination)
- Si `POLY_PRIVATE_KEY` n'est pas défini, les ordres sont simulés (aucune exécution on-chain)
- Les signaux peuvent être rares en période de faible volatilité BTC — c'est attendu
- Ne pas modifier `SIGNAL_THRESHOLD` (0.95) sans relancer le backtest complet

## Licence

Voir [LICENSE](LICENSE).
