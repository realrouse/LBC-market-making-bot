# tradinebotte

> 🇬🇧 [English version](README.md)

Plateforme de trading BTC/USDT automatisé multi-stratégies sur marchés spot CEX. Exécute des stratégies d'accumulation, scalping OBI, grid, swing, DCA et SwingHold sur Binance, MEXC et Bitstamp, alimentées par un pipeline de signaux temps réel partagé. Un connecteur pour les marchés de prédiction Polymarket est également inclus.

## Architecture

Des sous-systèmes indépendants communiquant via des sockets ZMQ IPC, bâtis sur un cœur neutre partagé :

| Sous-système | Chemin | Rôle |
|---|---|---|
| Cœur des bots | `tradinebotte-core/` | Paquet neutre `botcore` : protocole Strategy, registre de connecteurs, persistance, schéma de base — aucun code spécifique à un exchange |
| Bots CEX | `tradinebotte-cex/` | Exécution des trades, moteurs de stratégie |
| Indicateurs | `tradinebotte-indicators/` | Pipeline de signaux temps réel |
| Statut | `tradinebotte-status/` | Monitoring de santé, tableau de bord |
| Bibliothèque partagée | `tradinetools/` | Maths, helpers ZMQ, logging |

Chaque famille de bots est un plugin pair derrière les interfaces de `botcore` : une implémentation de `Strategy`, un connecteur (adaptateur d'exchange) et un plan de données. Un connecteur Polymarket optionnel (`tradinebotte-polymarket/`) cible les marchés de prédiction BTC sur Polygon — voir [Module Polymarket](#module-polymarket) ci-dessous.

Voir [docs/design.fr.md](docs/design.fr.md) pour la référence complète de l'architecture multi-processus et des flux de messages ZMQ.

## Bots de trading CEX

### Bot d'accumulation v1.5

[`tradinebotte-cex/accumulation_bot.py`] — Achat sur creux OBI avec ratchet de profit progressif. Surveille le déséquilibre du carnet d'ordres en temps réel via ZMQ ; entre sur BTC/USDT à des seuils de creux configurables avec scale-in adaptatif et trailing stop de rebuy. Quatre gates de signal optionnelles : Fear & Greed (`fear_greed_gate`), Liquidations (`liq_gate`), Ratio Long/Short (`ls_ratio_gate`), RSI 4h (`rsi4h_gate`). Buffer Earn via `earn_buffer_usd` ; gate VWAP sur l'achat initial uniquement. Configuré via `tradinebotte-cex/strategies/accumulation/btc_accumulation.json`. Une variante MEXC spot (`btc_accumulation_mexc.json`) exécute la même stratégie sur le prix/OBI MEXC spot réel (enregistré à la demande depuis le flux partagé sous `btc_scalping_mexc`), avec les frais MEXC et Earn désactivé. Voir [docs/accumulation.md](docs/accumulation.md) pour le document de conception complet de la stratégie.

### Bot de scalping OBI v2.12

[`tradinebotte-cex/orderbook_bot.py`] — Scalping OBI haute fréquence sur Binance spot. Consomme le WebSocket depth20 + aggTrade à ~100 ms ; calcul de l'OBI sur les N meilleurs niveaux bid/ask avec lissage EMA ; long-only depuis v2.4. TP 15 bps, SL 8 bps, durée maximale 3 minutes. Évolutions successives : v2.3 filtre TFI, v2.5 TP/SL calibrés, v2.7 gate VWAP, v2.9 gate profil de volume, v2.10 gate macro OBI multi-timeframe, v2.12 gate liquidations. Configuré via `tradinebotte-cex/strategies/scalping/orderbook_btc.json`.

## Moteurs de stratégie

Moteurs modulaires dans `tradinebotte-cex/strategy_engines/` :

| Moteur | Description |
|---|---|
| **Grid** (`grid.py`) | Grilles statiques ou adaptatives (static / trail=bear / trail=bull) ; backtestées sur trois régimes BTC de 90 jours |
| **Swing** (`swing.py`) | Ordres limit sur supports/résistances ; filtre directionnel EMA(200) 4h, stop-loss dynamique ATR(14), filtre de surachat RSI(14) |
| **SwingHold** (`swinghold.py`) | Entrées swing avec ventes fractionnées à chaque résistance ; conservation du reste pour une accumulation long terme |
| **DCA** (`dca.py`) | Achats DCA cadencés à intervalles configurables avec TP et SL optionnel |

Voir [`docs/AdaptedGridTrading.fr.md`](docs/AdaptedGridTrading.fr.md) pour les résultats de backtest et le guide de sélection des stratégies grid, et [`docs/GridTrading.fr.md`](docs/GridTrading.fr.md) pour le fonctionnement et la configuration.

Stratégie cycle long terme : trois configs de production dans `tradinebotte-cex/strategies/longterm/` (V1 : ×24,0, Calmar 0,54 / V2 : ×24,2, Calmar 0,54 / V3 : paliers de prudence relatifs au halving, Calmar 0,75). Backtest via `analysis/backtest_cycle_strategy.py`.

## Adaptateurs d'exchange

| Adaptateur | Exchange | Authentification |
|---|---|---|
| `api_binance.py` | Binance spot | HMAC-SHA256 |
| `api_mexc.py` | MEXC spot | HMAC-SHA256 (API v3 compatible Binance) ; WS de profondeur public en protobuf (`wbs-api.mexc.com`) |
| `api_mexc_futures.py` | MEXC Futures perpétuel | HMAC-SHA256 (en-têtes ApiKey + Request-Time) |
| `api_bitstamp.py` | Bitstamp spot | OAuth2 |

Helpers partagés dans `api_common.py` : parsing du carnet d'ordres, signature HMAC, mode simulation. Ajouter un exchange ne nécessite qu'un nouveau fichier adaptateur. `validate()` dans le registre `botcore.connectors` (ré-exporté via `tradinebotte-cex/connectors/__init__.py`) vérifie la compatibilité connecteur/stratégie au démarrage et lève une `RuntimeError` avec la liste complète des méthodes manquantes.

**Binance Simple Earn Flexible** (`earn_manager.py`) : `EarnManager` place les USDT inactifs après une vente (`park_idle()`) et les rachète avant un achat (`ensure_liquid()`). Découverte automatique du produit et rapport du taux annuel. Mode simulation si les credentials sont absents. MEXC Earn non pris en charge (API trop instable).

## Pipeline de signaux

`tradinebotte-indicators/indicators.py` tourne en tant que service ZMQ autonome :

- **Entrée** : streams WebSocket depth20 + aggTrade de Binance (100 ms)
- **Indicateurs** : RSI(14/21), SMA, EMA(50/200), ATR(14), OBI, TFI, `spread_bps`, `realized_vol_bps`, VWAP
- **Sortie** : messages `{"t":"indicators"}` enrichis sur un ZMQ PUB (IPC par défaut, ou TCP port 5559)
- **Flux** : configuration unifiée dans `tradinebotte-indicators/strategies/indicators_all.json` ; flux `btc_4h` pour les consommateurs swing ; liquidations via `wss://fstream.binance.com/ws/{symbol}@forceOrder` (public) ; flux full-depth perp optionnel (`btc_full_depth_perp`)
- **Watchdog** : timeout de 120 secondes sur `ws.recv()` (`asyncio.wait_for`) sur toutes les boucles WS

### Flux CEX partagé (plan de données)

`tradinebotte-cex/cex_feed.py` récupère chaque carnet d'ordres CEX externe **une seule fois** et le diffuse via ZMQ (TCP 5563), de sorte que les bots n'ouvrent jamais leur propre WebSocket d'exchange ; le passage d'ordres reste par bot avec les identifiants de chaque compte. Une tâche indépendante par exchange — actuellement **Binance spot**, **MEXC spot** et **MEXC futures** pour BTC. Les consommateurs filtrent par `(exchange, symbole)`, donc plusieurs places peuvent publier le même symbole sans contamination croisée.

- **MEXC spot** utilise le WebSocket public protobuf de MEXC (`wbs-api.mexc.com`, canal `spot@public.limit.depth.v3.api.pb`) ; les trames de profondeur binaires sont décodées via un schéma minimal vendorisé (`tradinebotte-cex/mexc_proto/`). Les sockets publiques MEXC sont maintenues actives par un ping applicatif.
- Le service indicators peut sourcer un flux de scalping depuis ce flux partagé (source `cex_scalping` → p. ex. `btc_scalping_mexc`) au lieu d'ouvrir son propre WS d'exchange.

**Enregistrement des flux à la demande** : les bots déclarent les flux d'indicateurs dont ils ont besoin dans leur config (`indicators_streams`) et les enregistrent auprès de la socket REP indicators (TCP 5561), en se ré-enregistrant périodiquement : un flux s'auto-répare si le service indicators redémarre — aucune config statique maintenue à la main.

Base de données SQLite partagée du carnet d'ordres optionnelle (`orderbook_current` + `orderbook_snapshots`), configurable par flux via `db_path`, `bucket_size_usd`, `db_write_every_n`, `history_retention_h`.

Voir [docs/indicators.fr.md](docs/indicators.fr.md) pour le guide de référence complet.

## Monitoring

`tradinebotte-status/status_collector.py` — collecteur de heartbeats autonome (port ZMQ 5562) :

- Reçoit les heartbeats de chaque bot (envoyés toutes les **120 s**), écrit en SQLite, purge les lignes de plus d'un an. Un bot est marqué **STALE après 240 s** et **DEAD après 600 s** (`HEARTBEAT_STALE_S` / `HEARTBEAT_DEAD_S`), donc une panne réelle apparaît en quelques minutes
- `generate_status.py` interroge tous les comptes de déploiement via SSH et génère une page HTML de santé unique affichant l'état des bots, les versions des services et les détails de payload (PnL, positions, connectivité WebSocket) ; toutes les valeurs de PnL proviennent du payload du heartbeat (source unique de vérité pour les bots Polymarket et CEX)
- Chemin de sortie par défaut : `~/public_html/tradinebottestatus.html`, modifiable via `--out` ou `$TRADINEBOTTE_STATUS_OUT`
- Voir [docs/logging.md](docs/logging.md) pour le vocabulaire canonique des tags de log utilisé par les alertes et les parseurs

## Déploiement

Services systemd utilisateur — aucun `sudo` requis :

```bash
systemctl --user status tradinebotte-live.service
systemctl --user status tradinebotte-indicators.service
systemctl --user status tradinebotte-feed.service
```

Plusieurs comptes isolés, chacun avec son propre répertoire d'installation, sa configuration et ses fichiers de log. Déploiement séquentiel sur tous les comptes :

```bash
bash tradinebotte-cex/scripts/deploy_all.sh
```

## Bibliothèque partagée : tradinetools

Package dans `tradinetools/`, installé avec `pip install -e tradinetools/` :

| Module | Contenu |
|---|---|
| `math.py` | `sma_last`, `ema_last`, `atr_last`, `bollinger_last`, `vwap_last`, `vol_zscore_last`, `rolling_max_last` |
| `zmq.py` | Fabriques de sockets ZMQ |
| `logging.py` | `setup_root_logger()` (fichier rotatif, 10 Mo), `setup_logger()` (logger de service nommé) |
| `schemas.py` | Dataclasses de messages versionnés |

## Analyse et backtesting

| Script | Stratégie |
|---|---|
| `analysis/backtest.py` | Replay snapshots Polymarket ; `--sweep` (135 combos) ; Kelly fractionnel, Sharpe/Sortino, walk-forward |
| `analysis/backtest_grid.py` | Replay grid OHLCV ; `--trail bear/bull`, `--sweep --sort pnl` |
| `analysis/backtest_swing_dca.py` | DCA / Swing / SwingHold ; `--compare`, `--all-dbs`, `--sweep`, `--config` |
| `analysis/backtest_orderbook.py` | Replay scalping OBI |
| `analysis/backtest_cycle_strategy.py` | Stratégie cycle long terme BTC ; configs V1/V2/V3 |
| `analysis/benchmark_api.py` | Benchmark de latence REST + WS sur les trois exchanges |
| `analysis/calibrate_obi_proxy.py` | Calibration des seuils OBI |

Télécharger les données OHLCV BTC historiques (bougies 1 minute Binance) :

```bash
python3 analysis/download_btc_history.py                                       # 90 derniers jours
python3 analysis/download_btc_history.py --start 2022-05-01 --end 2022-08-01  # bear market
python3 analysis/download_btc_history.py --start 2024-10-15 --end 2025-01-15  # bull run
```

## Module Polymarket

`tradinebotte-polymarket/` — connecteur de marchés de prédiction pour les marchés BTC Hausse/Baisse 5 et 15 minutes sur Polygon. Entièrement opérationnel ; inclus comme un connecteur parmi d'autres plutôt que comme fonctionnalité principale.

- **`live_bot.py`** — point d'entrée async au-dessus du cœur neutre `botcore` ; la logique de trading et le plan de données Polymarket vivent dans des modules-plugins plats à côté (`pm_strategy.py`, `pm_data.py`, `pm_types.py`, `pm_calendar.py` + `api_polymarket.py`), ré-exportés par `live_bot` pour la rétrocompatibilité. Entrée sur `best_bid >= 0.95` ; WIN si bid ≥ 0,99, LOSS si bid ≤ 0,01 ; stop-loss journalier (30 $), reprise après crash (restaure les trades non résolus au démarrage), page de statut HTML optionnelle avec HTTP Basic Auth
- **`feed.py` + `account_bot.py`** — partage WebSocket multi-bot via ZMQ IPC ; chaque `account_bot.py` (exécuté à plat, sans sous-répertoire `bot/`) trade avec une base SQLite, un log et une config totalement isolés
- **Fichiers de stratégie** : `tradinebotte-polymarket/strategies/polymarket_BTC5M.json` ; `polymarket_BTC5M_piste3.json` ajoute la mise proportionnelle (`bid_alpha`), le rejet OBI et le stop-loss hebdomadaire ; voir [docs/KellySizing.md](docs/KellySizing.md) pour la conception du sizing Kelly fractionnel
- **Base de données** `live.db` (SQLite WAL) : table `trades` (21 colonnes — contexte complet du signal jusqu'à la résolution), table `snapshots` (snapshots de prix toutes les 5 s pour l'analyse post-session) ; voir [docs/snapshots.md](docs/snapshots.md) pour le schéma et les requêtes de référence
- **Requêtes utiles** :

```bash
sqlite3 live.db "SELECT id, direction, outcome, pnl_net, capital_after FROM trades ORDER BY id DESC LIMIT 10;"
sqlite3 live.db "SELECT COUNT(*) total, SUM(CASE WHEN outcome='WIN' THEN 1 END) wins, ROUND(SUM(pnl_net),2) net_pnl FROM trades WHERE resolved=1;"
```

**Collecte de données** (mode simulation, snapshots à 1 seconde) :

```bash
bash tradinebotte-polymarket/scripts/start_collector.sh           # déploiement + lancement
bash tradinebotte-polymarket/scripts/start_collector.sh --status  # vérifier si en cours
bash tradinebotte-polymarket/scripts/collect_db.sh --rotate       # télécharger + archiver + redémarrer
bash tradinebotte-polymarket/scripts/schedule_collect.sh --install # cron hebdomadaire (dimanche 03:00 UTC)
```

Voir [docs/multi.fr.md](docs/multi.fr.md) pour la référence d'architecture multi-bot complète.

## Installation

**Nouvel utilisateur ?** Voir **[QUICKSTART.fr.md](QUICKSTART.fr.md)** — 5 commandes, bot opérationnel en quelques minutes.

Guide complet (prérequis, configuration du wallet, page de statut web, monitoring, tests) : **[INSTALL.fr.md](INSTALL.fr.md)**.

> **Note administrateur serveur :** `scripts/install.sh` détecte les paquets système manquants et affiche la commande `sudo apt-get install` exacte — aucune recherche manuelle nécessaire. Voir [INSTALL.fr.md — Prérequis administrateur serveur](INSTALL.fr.md#prérequis-administrateur-serveur-debianubuntu).

## Tests

```bash
bash scripts/run_tests.sh
```

1163 tests répartis en 6 suites. Aucun accès réseau ni credentials requis — base SQLite en mémoire pour chaque test.

Voir [docs/HOWTO_tests_and_backtests.fr.md](docs/HOWTO_tests_and_backtests.fr.md) pour le guide pratique d'exécution des tests et des backtests.

## Notes

- Le CLI `sqlite3` est optionnel — le bot utilise le module Python intégré. L'installer (`sudo apt install sqlite3`) uniquement pour les requêtes manuelles.
- Ne pas modifier les paramètres de stratégie sans relancer le backtest correspondant.
- `POLY_PRIVATE_KEY` absent → les ordres Polymarket sont simulés (aucune exécution on-chain).
- Les timeouts recv WebSocket (~30s) en période calme sont normaux — la reconnexion automatique gère ces cas.
- mypy `--ignore-missing-imports` retourne 0 erreur sur l'ensemble des sous-systèmes.

## Licence

Voir [LICENSE](LICENSE).
