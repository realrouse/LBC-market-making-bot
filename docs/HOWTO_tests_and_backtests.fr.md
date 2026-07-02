# HOWTO — Tests et Backtests

> 🇬🇧 [English version](HOWTO_tests_and_backtests.md)

Ce guide explique comment lancer la suite de tests automatisés et le moteur de backtest,
définit chaque concept, champ et colonne présents dans leurs sorties, et décrit comment
interpréter les résultats pour prendre des décisions éclairées sur la stratégie.

---

## Table des matières

1. [Glossaire](#1-glossaire)
2. [Lancer les tests](#2-lancer-les-tests)
3. [Lancer un backtest](#3-lancer-un-backtest)
4. [Tous les flags du backtest](#4-tous-les-flags-du-backtest)
5. [Sortie standard expliquée](#5-sortie-standard-expliquée)
6. [Recherche en grille — `--sweep` et `--sweep-all`](#6-recherche-en-grille----sweep-et---sweep-all)
7. [Comparaison trois colonnes — `--compare`](#7-comparaison-trois-colonnes----compare)
8. [Le problème de modélisation STOP/GHOST](#8-le-problème-de-modélisation-stopghost)
9. [Workflow complet — `strategy_compare.sh`](#9-workflow-complet----strategy_comparesh)
10. [Fichiers de stratégie JSON](#10-fichiers-de-stratégie-json)
11. [Interpréter les résultats et prendre des décisions](#11-interpréter-les-résultats-et-prendre-des-décisions)
12. [Autres scripts de backtest](#12-autres-scripts-de-backtest)

---

## 1. Glossaire

### Données de marché et de prix

| Terme | Définition |
|-------|-----------|
| **Snapshot** | Un enregistrement de prix écrit dans la table SQLite `snapshots` toutes les ~5 secondes par le bot live. Chaque ligne capture un marché à un instant donné : best bid, best ask, volumes, OBI, secondes restantes. |
| **best_bid** | Prix le plus élevé qu'un acheteur accepte de payer pour le token YES (de 0 à 1). Une valeur de 0,96 signifie que les acheteurs estiment que le marché se résoudra YES avec 96 % de probabilité. |
| **best_ask** | Prix le plus bas qu'un vendeur accepte de recevoir. Toujours ≥ best_bid. Une valeur ≥ 1,0 signifie que le marché est déjà résolu. |
| **bid_vol** | Volume total en USD sur les 5 premiers niveaux côté acheteur du carnet d'ordres. |
| **ask_vol** | Volume total en USD sur les 5 premiers niveaux côté vendeur du carnet d'ordres. |
| **OBI** | **Order Book Imbalance** (déséquilibre du carnet d'ordres) = `(bid_vol − ask_vol) / (bid_vol + ask_vol)`. Varie de −1 (tous vendeurs) à +1 (tous acheteurs). Un OBI négatif indique une pression vendeuse. Formule calculée sur les 5 meilleurs niveaux de chaque côté. |
| **secs_remaining** | Secondes avant la clôture du marché. Un marché avec zéro secondes ou moins est expiré. |
| **spread** | `best_ask − best_bid`. Un spread serré (< 0,01) indique un marché liquide. |

### Issues d'un trade

| Issue | Quand elle survient |
|-------|-------------------|
| **WIN** | `best_bid` atteint le seuil de victoire (≥ 0,99). Le trade est clôturé en profit. PnL net = `tokens × 1,0 − mise − frais`. |
| **LOSS** | `best_bid` descend au seuil de perte (≤ 0,01). Le trade est clôturé en quasi-perte totale. PnL net ≈ `−mise`. |
| **OPEN** | Le marché a expiré avant qu'aucun seuil ne soit atteint et les données se sont terminées. Le trade reste non résolu. Seul le moteur de backtest produit des OPEN ; le bot live gère ces cas comme GHOST ou WIN/LOSS à expiration. |
| **STOP** | Bot live uniquement. Le stop-loss journalier a été déclenché pendant la session ; le bot a cessé d'ouvrir de nouveaux trades pour le reste de la journée. Les trades STOP sont comptabilisés dans les stats réelles mais ne sont pas modélisés par le moteur de backtest. |
| **GHOST** | Bot live uniquement. Un trade a été ouvert mais le marché a expiré sans qu'un exit WIN ou LOSS ne soit enregistré (ex : déconnexion WebSocket, timeout API). Comptabilisé dans les stats réelles mais non modélisé en backtest. |

### Paramètres de stratégie

| Paramètre | Défaut | Où il se trouve | Signification |
|-----------|--------|----------------|---------------|
| `signal_threshold` | 0,95 | JSON stratégie / `--threshold` | Minimum de `best_bid` requis pour ouvrir un trade. Le signal d'entrée principal. |
| `entry_max` | 0,998 | JSON stratégie | Maximum de `best_bid` accepté. Protège contre les marchés déjà résolus qui passent au travers du filtre temporel. |
| `min_secs_remaining` | 45 | JSON stratégie / `--min-secs` | Secondes minimum restantes dans le marché à l'entrée. Trop court → pas de temps pour récupérer ; trop long → signal moins précis. |
| `min_ask_vol` | 10 | JSON stratégie / `--min-ask` | Liquidité minimum côté vendeur en USD. Les entrées sont ignorées quand le marché est trop illiquide. 0 = désactivé. |
| `obi_reject_thresh` | −0,25 (CLI) / −0,65 (piste3 live) | JSON stratégie / `--obi` | Plancher OBI. Les entrées sont rejetées quand `OBI < obi_reject_thresh`, c.-à-d. quand la pression vendeuse est trop forte. Le flag CLI `--obi` utilise −0,25 par défaut ; la stratégie live active (piste3) utilise −0,65. |
| `stake` | 10 | JSON stratégie / `--stake` | Montant en USD misé par trade. |
| `daily_stop_loss` | 30 | JSON stratégie | Perte cumulée maximale par jour calendaire (UTC). Une fois atteint, le bot cesse d'entrer de nouveaux trades pour le reste de la journée. |
| `capital_start` | 100 | JSON stratégie | Capital de départ pour un run de backtest. Utilisé comme dénominateur pour PnL%. |
| `win_threshold` | 0,99 | JSON stratégie | `best_bid` auquel un trade ouvert est auto-résolu en WIN. Ne pas modifier sans relancer le backtest complet. |
| `loss_threshold` | 0,01 | JSON stratégie | `best_bid` auquel un trade ouvert est auto-résolu en LOSS. Ne pas modifier sans relancer le backtest complet. |
| `fee_rate` | 2 % | constante du connecteur (`FEE_RATE` dans `api_polymarket.py`, via `state.connector.FEE_RATE`) | Frais de preneur (taker) Polymarket appliqués à chaque trade. |

### Métriques de performance

| Métrique | Définition |
|----------|-----------|
| **Trades** | Total des trades simulés (ou réels), incluant OPEN, WIN, LOSS. En backtest, les trades ouverts en fin de données sont listés séparément. |
| **WR% (taux de victoire)** | `wins / (wins + losses) × 100`. OPEN, STOP, GHOST sont exclus du dénominateur. Plage normale : 97–99 %. |
| **Total PnL** | Profit/perte net en USD sur tous les trades résolus. PnL par WIN ≈ `mise × (1/prix_entrée − 1) × (1 − fee_rate)`. PnL par LOSS = `−mise`. |
| **PnL%** | `total_pnl / capital_start × 100`. Comparable entre configurations quelle que soit la mise ou la taille du capital. |
| **MaxDD (drawdown maximum)** | Pire perte cumulée en un seul jour calendaire (UTC) sur toutes les sessions. Proxy d'exposition au risque. Exprimé en USD. |
| **Ratio PnL/DD** | `total_pnl / max_drawdown`. Métrique de type Calmar, ajustée au risque. Plus élevé = mieux. Un ratio ≥ 3,5 est bon ; ≥ 4,0 est excellent. Un ratio `∞` signifie zéro drawdown (à traiter avec méfiance — trop peu de trades ou période très favorable). |

### Terminologie du backtest

| Terme | Définition |
|-------|-----------|
| **Backtest** | Rejeu de la table `snapshots` à travers la logique de stratégie. Le moteur lit les lignes chronologiquement, applique les conditions d'entrée/sortie et accumule le PnL simulé. Aucun ordre réel n'est passé. |
| **Backtest aligné** | Une **simulation** (rejeu des données `snapshots`, aucun ordre réel) qui utilise les paramètres que le bot avait *réellement* à l'exécution, inférés depuis la table `trades`. C'est toujours purement simulé — mais ses paramètres (threshold, stake, min_secs, capital_start, DSL) sont corrigés pour correspondre à la configuration réelle du bot, ce qui en fait la simulation la plus précise de ce qui *aurait dû* se passer. Affiché comme colonne centrale dans `--compare`. |
| **Bot réel** | Les **trades réellement exécutés** par le bot live sur Polymarket, tels qu'enregistrés dans la table `trades` lors d'une session live. Ce n'est pas une simulation — ce sont les événements réels : argent réel, fills d'ordres aux prix du marché, latence d'exécution réelle. Inclut les issues non modélisées en backtest (STOP, GHOST), et est affecté par les déconnexions WebSocket et les délais API. Affiché comme colonne de droite dans `--compare`. |
| **Remise à zéro du capital** | Quand plusieurs fichiers DB sont traités, chaque fichier repart avec un `capital_start` frais. Cela isole les sessions pour qu'un mauvais jour dans un fichier n'affecte pas un autre. |
| **Sweep / recherche en grille** | Exécution du backtest pour chaque combinaison de valeurs de paramètres dans une grille prédéfinie, puis classement des résultats. Utilisé pour trouver les paramètres de stratégie optimaux. |
| **Déduplication (--top)** | Le tableau du sweep peut contenir des lignes quasi-identiques où seuls `min_ask` ou `dsl` diffèrent mais le résultat est identique. `--top N` réduit le tableau aux N meilleures configurations *uniques* (dédupliquées sur `threshold / min_secs / obi`). |

---

## 2. Lancer les tests

### Démarrage rapide

```bash
bash scripts/run_tests.sh
```

Ce script :
1. Localise le virtualenv du projet (`.venv/` ou `~/tradinebotte/venv/`).
2. Lance la suite unittest complète (`tests/test_*.py`) en mode verbeux.
3. Lance le contrôle qualité des données (`analysis/check_data_quality.py --no-gaps --warn-only`) sur tous les fichiers `data/*.db` trouvés (non bloquant).
4. Lance un backtest `--all` sur tous les fichiers `data/*.db` trouvés (non bloquant).
5. Invoque l'agent `doc-sync` pour auditer la documentation des flags (nécessite le CLI `claude`).
6. Lance pylint sur tous les fichiers Python trackés (nécessite l'installation de `requirements-dev.txt`, non bloquant).

### Lire la sortie

```
test_compute_fee_win (TestComputeFee) ... ok
test_parse_book_message_bid (TestParseBookMessage) ... ok
...
Ran 368 tests in 12.8s
OK (skipped=1)
```

| Statut | Signification |
|--------|--------------|
| `ok` | Test passé. |
| `FAIL` | Une assertion a échoué — le code ne correspond pas au comportement attendu. |
| `ERROR` | Une exception inattendue a été levée dans le test. |
| `skipped` | Test ignoré intentionnellement (généralement : fichier DB requis absent). |
| `OK` en fin | Tous les tests sont passés. |
| `FAILED (failures=N)` | N tests ont échoué — à investiguer avant de committer. |

### Fichiers de tests et ce qu'ils couvrent

| Fichier | Classes | Ce qui est testé |
|---------|---------|-----------------|
| `tests/test_bot.py` | 20 classes | Logique cœur du bot : calcul des frais, parsing des messages book, détection du signal, résolution des trades, cache PnL journalier, migrations du schéma DB, chargement du fichier de stratégie, échappement HTML, filtre OBI, filtre des heures de trading, circuit-breaker |
| `tests/test_backtest.py` | 9 classes | Moteur de backtest : `run_backtest`, `summarize`, `_ratio`, helper percentile, `detect_actual_params`, `_actual_stats`, `_collect_dbs` |
| `tests/test_regression.py` | 2 classes | **Régression de performance** contre `data/paper3.db` (WR ≥ 98 %, PnL ≥ 80 $, MaxDD < 100 $) ; **cohérence des paramètres** entre les constantes de `live_bot.py` et les valeurs par défaut de `backtest.py` — ces deux fichiers doivent toujours être en accord |
| `tests/test_multibot.py` | 4 classes | Intégration multi-bot : feed et account bot (register_market, coordination deux bots) |
| `tests/test_api_cex.py` | 10 classes | Contrat des adaptateurs CEX : calcul des frais, parsing des métadonnées, parsing du carnet d'ordres pour Binance et MEXC |

### Les tests de régression

`TestBacktestPerformance` est le filet de sécurité le plus important. Il lance le backtest sur `data/paper3.db` avec les paramètres par défaut actuels et vérifie :

- Nombre de snapshots plausible (≥ 2 700)
- Taux de victoire ≥ 98 %
- PnL total ≥ 80 $
- Pertes < 50
- MaxDD < 100 $
- Identité comptable du capital

Si un changement de paramètre dégrade silencieusement les performances, ce test le détecte. Il est automatiquement **ignoré** quand `data/paper3.db` est absent (ex : checkout CI sans données).

`TestParamConsistency` vérifie que les valeurs des paramètres dans `tradinebotte-polymarket/live_bot.py` (constantes au niveau module) correspondent aux valeurs par défaut dans `analysis/backtest.py` (le dataclass `Params`). Si elles divergent, les backtests ne prédisent plus les performances live.

### Checklist pré-release

`bash scripts/run_tests.sh` couvre la partie automatisée. Avant de tagger une release, exécuter également :

**Contrôle qualité complet avec détection des gaps** (plus lent, ~22 s — omis par `run_tests.sh` pour la rapidité) :

```bash
python3 analysis/check_data_quality.py --warn-only
```

Lance les requêtes LAG() de détection de gaps ignorées par `--no-gaps`. Tout résultat `FAIL` (gap de collecte > 2 h, contamination de prix, violation d'invariant de capital) doit être investigué avant la release. Les items `WARN` sont informatifs.

**Tests d'intégration** (requis pour les releases touchant le multibot, l'IPC ou le déploiement ; nécessite `~/.tradinebotte-test.conf`) :

```bash
bash scripts/run_integration_tests.sh
```

---

## 3. Lancer un backtest

### Résolution de la base de données (sans flag `--db` ni `--all`)

Le moteur de backtest essaie dans l'ordre et utilise le premier trouvé :

1. `$TRADINEBOTTE_DIR/live.db` — la base de données du bot live, si elle a ≥ 100 snapshots.
2. `data/paper3.db` — la session de paper-trading (764k snapshots).
3. `data/backtest_sample_btc5m_range_2026.db` — le jeu de données exemple livré avec le projet.

### Utilisation de base

```bash
# Défaut : sélectionne automatiquement la meilleure DB disponible
python3 scripts/backtest.py

# Fichier explicite
python3 scripts/backtest.py --db ~/tradinebotte/live.db

# Plusieurs fichiers (capital indépendant par fichier)
python3 scripts/backtest.py --db data/session_a.db data/session_b.db

# Glob shell (identique, le shell développe le motif)
python3 scripts/backtest.py --db data/*.db

# Scanner data/ automatiquement + live.db si utilisable
python3 scripts/backtest.py --all
```

---

## 4. Tous les flags du backtest

### Sélection de la base de données

| Flag | Description |
|------|-------------|
| `--db PATH [PATH…]` | Un ou plusieurs chemins de fichiers DB explicites. Accepte les globs shell (développés par le shell). |
| `--all` | Scanner `data/` pour tous les fichiers `.db` et ajouter `live.db` si ≥ 100 snapshots. |

### Overrides des paramètres de stratégie

Ces flags remplacent la valeur correspondante du JSON de stratégie pour un seul run. Ils ne modifient pas le fichier de stratégie de manière permanente.

| Flag | Défaut | Description |
|------|--------|-------------|
| `--threshold FLOAT` | 0,95 | Signal d'entrée : `best_bid` minimum pour ouvrir un trade. |
| `--min-secs FLOAT` | 30,0 | Secondes minimum restantes à l'entrée. |
| `--min-ask FLOAT` | 10,0 | Liquidité minimum côté vendeur en USD (0 = désactivé). |
| `--obi FLOAT` | −0,25 | Seuil de rejet OBI. Les entrées avec un OBI inférieur sont ignorées. |
| `--stake FLOAT` | 10,0 | Mise en USD par trade. |

### Modes d'affichage

| Flag | Description |
|------|-------------|
| `--detail` | Affiche une ligne par trade avec les horodatages d'entrée/sortie, les prix, les secondes restantes, l'issue et le PnL. Utile pour diagnostiquer des pertes spécifiques. |
| `--compare` | Tableau de comparaison trois colonnes : backtest avec les paramètres utilisateur \| backtest aligné sur les paramètres réels du bot \| résultats réels du bot depuis la table `trades`. |

### Recherche en grille (sweep)

| Flag | Description |
|------|-------------|
| `--sweep` | Recherche en grille standard : 135 combinaisons de `threshold × min_secs × min_ask` sur une DB. |
| `--sweep-all` | Recherche en grille étendue : 405 combinaisons (`threshold × min_secs × min_ask × obi × dsl`) sur toutes les DB disponibles. Résultats agrégés (somme PnL, pire MaxDD entre sessions). |
| `--sort METRIC` | Trier les résultats du sweep par `ratio` (PnL/MaxDD, défaut), `pnl` (PnL total), ou `wr` (taux de victoire). |
| `--top N` | Afficher uniquement les N meilleures configurations uniques dans le tableau, dédupliquées sur `(threshold, min_secs, obi)`. Supprime les variantes redondantes de `min_ask` et `dsl` qui produisent des résultats identiques. Défaut : 0 (tout afficher). |

---

## 5. Sortie standard expliquée

### Bloc par fichier

```
BACKTEST — paper3.db
signal=0.95  min_secs=30  min_ask=10  obi=-0.25
Snapshots: 764,399
==============================================================
Trades   : 2856
Wins     : 2808
Losses   : 30
Open     : 18  (unresolved at end of data)
Stake    : $10.00  (capital start: $100.00)
Win rate : 98.9%
Total PnL: $+89.13  (+89.1%)
Max DD   : $80.69
Capital  : $189.13
```

| Champ | Signification |
|-------|--------------|
| `signal=…` | Paramètres utilisés pour ce run (threshold, min_secs, min_ask, obi). |
| `Snapshots` | Nombre de lignes lues dans la table `snapshots`. |
| `Trades` | Total des trades ouverts (résolus + ouverts). |
| `Open` | Trades encore ouverts en fin de données — le dernier snapshot du marché a été traité mais ni le seuil WIN ni LOSS n'a été atteint. Exclus du taux de victoire. |
| `Stake` / `capital start` | Mise par trade et capital de départ pour le calcul de PnL%. |
| `Win rate` | `wins / (wins + losses) × 100`. |
| `Total PnL` | Profit/perte net en USD. Le `+89,1 %` est `PnL / capital_start × 100`. |
| `Max DD` | Pire perte cumulée sur une journée (UTC). |
| `Capital` | `capital_start + total_pnl` — capital final si vous aviez commencé avec `capital_start`. |

### Bloc agrégé (plusieurs fichiers)

Quand plusieurs fichiers sont traités, un bloc AGGREGATE résume toutes les sessions :

```
AGGREGATE — 5 file(s)  912,777 snapshots
(capital reset per file — independent sessions)
Trades   : 3202
Win rate : 99.0%
Total PnL: $+108.09
PnL%     : +21.6%  (sur capital total $500.00)
Worst DD : $80.69  (worst single session)
```

Notez que `PnL%` divise ici par le capital total de départ sur tous les fichiers (`5 × 100 $ = 500 $`).

### Tableau de trades avec `--detail`

```
  ts_entry             market_id        dir   bid_in  secs  outcome  bid_out   fee   pnl_net
  2026-01-15 10:23:05  0x1a2b…          YES   0.9612    52  WIN      0.9902  $0.20  $+0.22
```

| Colonne | Signification |
|---------|--------------|
| `ts_entry` | Horodatage UTC de l'entrée du trade. |
| `market_id` | Identifiant du token Polymarket (tronqué). |
| `dir` | Direction — toujours `YES` sur les marchés BTC Up/Down. |
| `bid_in` | `best_bid` à l'entrée (a déclenché le signal). |
| `secs` | Secondes restantes à l'entrée. |
| `outcome` | WIN / LOSS / OPEN. |
| `bid_out` | `best_bid` à la sortie (WIN ≈ 0,99, LOSS ≈ 0,01, OPEN = dernière valeur connue). |
| `fee` | Frais de preneur payés. |
| `pnl_net` | Profit/perte net de ce trade. |

---

## 6. Recherche en grille — `--sweep` et `--sweep-all`

### Ce que ça fait

La recherche en grille lance le backtest pour chaque combinaison de valeurs de paramètres, puis classe les résultats selon la métrique choisie. C'est l'outil principal pour trouver les paramètres optimaux.

### Grilles de paramètres

**`--sweep`** (135 combos, une seule DB) :

| Paramètre | Valeurs testées |
|-----------|----------------|
| `threshold` | 0,94 ; 0,95 ; 0,96 ; 0,97 ; 0,98 |
| `min_secs` | 30 ; 45 ; 60 |
| `min_ask` | 5 ; 10 ; 20 |

**`--sweep-all`** (405 combos, toutes les DB, remise à zéro du capital par fichier) :

| Paramètre | Valeurs testées |
|-----------|----------------|
| `threshold` | 0,94 ; 0,95 ; 0,96 ; 0,97 ; 0,98 |
| `min_secs` | 30 ; 45 ; 60 |
| `min_ask` | 5 ; 10 ; 20 |
| `obi` | −0,75 ; −0,50 ; −0,25 |
| `dsl` | 30 ; 100 ; 500 |

### Colonnes du tableau de sweep

```
threshold | min_secs | min_ask |    obi |    dsl | trades |  wins |    WR% |       PnL |    PnL% |   MaxDD |  PnL/DD
```

| Colonne | Signification |
|---------|--------------|
| `threshold` | Seuil du signal d'entrée. |
| `min_secs` | Secondes minimum restantes à l'entrée. |
| `min_ask` | Volume minimum côté vendeur. |
| `obi` | Seuil de rejet OBI. |
| `dsl` | Stop-loss journalier en USD. |
| `trades` | Total des trades sur toutes les DB pour cette config. |
| `wins` | Total des trades gagnants. |
| `WR%` | Taux de victoire (wins / résolus × 100). |
| `PnL` | PnL net total en USD (somme sur tous les fichiers). |
| `PnL%` | `PnL / capital_total_départ × 100`. Capital total = `capital_start × nombre_de_fichiers`. |
| `MaxDD` | Pire perte journalière sur tous les fichiers (exposition au pire cas). |
| `PnL/DD` | Ratio de type Calmar, ajusté au risque. `∞` = zéro drawdown (suspect). |

### Section Recommandations

Après le tableau, le script affiche les 5 meilleures configs selon trois critères :
- **Par ratio PnL/MaxDD** — recommandé pour la sélection ajustée au risque.
- **Par PnL total** — configs avec le profit absolu le plus élevé.
- **Par taux de victoire** — le plus haut % de trades gagnants (attention : WR très élevé signifie souvent très peu de trades avec un seuil élevé — vérifier le nombre de trades).

La dernière ligne donne la commande CLI exacte pour reproduire la meilleure config globale.

### Note sur la colonne `min_ask`

`min_ask` a un effet négligeable sur la plupart des marchés — la déduplication du top-N (`--top`) supprime les variantes `min_ask` pour que vous ne voyiez que les configs qui diffèrent sur les axes significatifs (`threshold`, `min_secs`, `obi`).

### AVERTISSEMENT — contamination de `--sweep-all` par les bases non-Polymarket

`--sweep-all` inclut **toutes les bases ayant une table `snapshots`**, pas seulement les bases Polymarket. Les bases des stratégies CEX (`grid_cex_*.db`, `swing_cex_*.db`) ont aussi une table `snapshots` et sont silencieusement incluses dans le sweep.

Cela contamine l'optimisation OBI : les snapshots CEX ont des distributions OBI différentes, et le sweep agrégé recommandera un seuil OBI beaucoup plus permissif (ex. `obi=−0,75`) qui semble bon globalement mais dégrade les performances Polymarket testé en isolation.

**Utilisation sûre de `--sweep-all`** : validez toujours la meilleure recommandation en la ré-exécutant sur une seule base Polymarket (`--db data/polymarket_5M_c2_*.db`) avant de l'adopter. Cas confirmé du 2026-06-08 : sweep-all recommandait `obi=−0,75` (Sharpe 6,4) mais `obi=−0,25` (actuel) a obtenu Sharpe 10,1 sur c2 seul.

---

## 7. Comparaison trois colonnes — `--compare`

### Objectif

`--compare` lance trois backtests côte à côte pour chaque DB et affiche un tableau de comparaison :

| Colonne | Ce que c'est |
|---------|-------------|
| **BACKTEST (paramètres)** | Une **simulation** : rejeu des `snapshots` avec les paramètres que vous avez spécifiés (ou les valeurs par défaut du JSON de stratégie). Aucun trade réel. |
| **BACKTEST (aligné)** | Une **simulation** : rejeu des mêmes `snapshots`, mais avec les paramètres que le bot avait *réellement* à l'exécution (inférés depuis la table `trades`). Toujours pas de trades réels, mais les paramètres sont corrigés — c'est la prédiction côté simulation la plus fidèle de ce qui aurait dû se passer. |
| **BOT RÉEL** | **Trades réels** : ce que le bot live a réellement exécuté sur Polymarket, stocké dans la table `trades`. Argent réel, fills aux prix du marché, latence réelle. Pas une simulation. Peut contenir des issues STOP et GHOST que les simulations ne peuvent pas modéliser. |

### Comment les paramètres réels sont détectés

Le moteur lit la table `trades` et infère :

| Paramètre | Méthode de détection |
|-----------|---------------------|
| `stake` | Valeur modale (la plus fréquente). |
| `threshold` | 5e percentile de `signal_best_bid` arrondi à 0,01 inférieur. |
| `min_secs` | 5e percentile de `signal_secs_remaining` arrondi à 5s inférieur. |
| `capital_start` | `capital_before` du premier trade résolu chronologiquement. |
| `daily_stop_loss` | Pire perte journalière + 20 % de marge. **Abandonnée** quand le résultat > 5× la mise (une seule perte ≥ limite journalière → heuristique non fiable). Retombe sur le DSL de l'utilisateur. |

### Lignes du tableau de comparaison

```
                               BACKTEST         BACKTEST              BOT
                           (paramètres)         (aligné)             RÉEL
  Threshold                        0.95             0.96          ≈0.9600
  Min secs                          30s              45s             ≈47s
  Stake                             $10              $10      $10 (modal)
  Capital start                    $100             $100             $100
  Daily stop-loss                   $30              $50             ≈$50
  Trades                            299              199              301
  Wins                              297              197              293
  Losses                              2                2                8
  Win rate                        99.3%            99.0%            97.3%
  Total PnL                     $+14.73           $+4.97          $-15.18
  PnL%                           +14.7%            +5.0%           -15.2%
  Max DD                         $13.90           $16.24                —
```

### Lire les avertissements de divergence

Quand le bot a tourné avec des paramètres différents des valeurs par défaut du backtest, le script affiche :

```
⚠  Paramètres divergents :
   stake: backtest=$10, actual=$150 (×15)
   threshold: backtest=0.95, actual≈0.93
```

Ces divergences expliquent pourquoi la colonne BACKTEST simple n'est pas directement comparable au BOT RÉEL. La colonne **alignée** corrige cela.

### Divergences courantes expliquées

| Divergence | Ce que ça signifie |
|------------|-------------------|
| **Stake ×N** | Le bot a tourné avec une mise plus élevée (ex : après modification manuelle de la config). Les valeurs PnL$ s'ajustent proportionnellement ; PnL% est comparable si capital_start est aussi aligné. |
| **DSL affiché comme `—`** | Le stop-loss journalier n'a pas pu être détecté de manière fiable (mise > DSL : une seule perte dépasse déjà la limite journalière, rendant l'heuristique du pire jour non fiable). Le backtest aligné retombe sur le DSL que vous avez spécifié. La comparaison PnL pour cette DB est moins fiable. |
| **Capital start >> 100 $** | Le bot a démarré cette session avec du capital accumulé lors de trades précédents. Les colonnes PnL% utilisent des bases différentes. |
| **Écart STOP/GHOST** | Le bot a eu des issues STOP ou GHOST non modélisées en backtest. Un écart positif (PnL% backtest > PnL% bot réel) est attendu et partiellement expliqué par ces issues. |

### Quand s'inquiéter

- **PnL% bot réel négatif alors que le backtest aligné est positif** : investiguer la précision du taux de frais, la divergence de seuil, ou une période de données atypique.
- **Écart > 2× entre PnL% backtest aligné et PnL% bot réel** : vérifier le nombre de STOP/GHOST. S'ils sont faibles, investiguer les données d'exécution.
- **8+ pertes là où le backtest en montre 2** : la période de données a peut-être inclus une rupture structurelle du marché non capturée dans les snapshots.

---

## 8. Le problème de modélisation STOP/GHOST

Cette section explique la différence systématique la plus importante entre les résultats
de backtest et les résultats réels du bot, et comment en tenir compte lors de l'interprétation
de la sortie `--compare`.

### Ce que signifie STOP

Un résultat **STOP** est enregistré quand le **stop-loss journalier se déclenche alors qu'un
trade est ouvert**. Le bot live force la clôture de la position immédiatement au bid courant du
marché — qui est typiquement entre 0,01 et 0,99, pas à un seuil propre WIN ou LOSS.

```
paper3.db — 24 trades STOP :
  Bid de sortie moyen : 0,356  (le marché était à ~35 % de probabilité à la sortie)
  PnL moyen          : −79 $   (sur une mise moyenne de 127 $)
  Perte moyenne      : −62 % de la mise
  Impact PnL total   : −1 895 $
```

Mécaniquement : si mise = 150 $ et ask d'entrée ≈ 0,953, le bot a acheté ≈ 157 tokens.
Sortie à bid = 0,356 rapporte 157 × 0,356 ≈ 56 $. Perte nette = 56 $ − 150 $ = **−94 $**
(proche de la moyenne −79 $, la variation reflétant les différents bids de sortie).

Le stop-loss journalier ne garantit pas une perte fixe — il sort au bid du marché au
moment du déclenchement. Un stop à bid=0,1 perd 90 % de la mise ; un stop à bid=0,5
perd 50 %.

### Ce que signifie GHOST

Un résultat **GHOST** est enregistré quand un trade a été ouvert mais **aucune résolution
n'a jamais été détectée** par le bot. Le marché a expiré (ou a été résolu on-chain) sans
que le bot ne capte l'événement WIN ou LOSS — typiquement à cause d'une déconnexion
WebSocket, d'un timeout API, ou d'un marché qui s'est résolu entre deux reconnexions.

```
paper3.db — 19 trades GHOST :
  pnl_net          : 0,00 $ pour les 19 trades
  resolution_bid   : 0,000 (aucun prix de sortie enregistré)
  mise             : 150 $ chacun
```

`pnl_net = 0,0` ne signifie **pas** que la mise a été récupérée. Cela signifie que le bot
a écrit zéro car il ne pouvait pas déterminer l'issue. L'impact économique réel dépend
de ce qui s'est passé on-chain : si le marché a résolu YES la position était profitable ;
si NO la mise entière a été perdue. Sans réconciliation oracle, l'issue est inconnue et
traitée de façon conservatrice comme un PnL nul.

### Pourquoi le backtest ne peut modéliser ni l'un ni l'autre

| Issue | Ce que fait le backtest à la place | Pourquoi ça diffère |
|-------|----------------------------------|---------------------|
| **STOP** | Bloque les nouvelles entrées quand `daily_pnl < −DSL`. Les trades ouverts existants continuent jusqu'à atteindre bid ≥ 0,99 (WIN) ou bid ≤ 0,01 (LOSS). | Le backtest ne force jamais la sortie à un prix intermédiaire. Un trade qui aurait été STOPpé en live à bid=0,35 est finalement simulé en WIN ou LOSS — souvent WIN, car la plupart des marchés résolvent YES. Cela rend le backtest **systématiquement optimiste** pour les sessions avec des stops. |
| **GHOST** | Pas de concept de résolution manquante. Si les snapshots se terminent avant la résolution, le trade est enregistré comme `OPEN`. | La table `snapshots` contient chaque message reçu par le bot. Un marché qui s'est résolu pendant une déconnexion n'a laissé aucun snapshot — donc le backtest n'entre jamais ce trade, ou l'entre et le laisse OPEN. Les mises déployées dans les trades GHOST sont invisibles pour le backtest. |

### Quantifier l'écart (exemple paper3.db)

```
PnL% backtest aligné   : +168,2 %  (mise=150 $, capital=1 000 $)
PnL% bot réel          :  +31,4 %
Écart                  : −136,8 pp

Contributeurs connus :
  STOP  : 24 trades × moy. −79 $  = −1 895 $  (backtest les comptait en WIN/LOSS, surtout WIN)
  GHOST : 19 trades × inconnu     =      0 $  (mise déployée, issue inconnue)
  Pertes supplémentaires : 78 réelles vs 28 backtest aligné = 50 LOSS de plus × −137 $ moy. = −6 850 $
```

Les 50 pertes supplémentaires (au-delà de ce que prédit le backtest aligné) expliquent
la majorité de l'écart. Leur origine est un biais de résolution temporelle fondamental
expliqué dans la section ci-dessous. Les pertes STOP ajoutent −1 895 $ que le backtest
traite comme des victoires.

### Comment lire l'écart dans la sortie `--compare`

Quand le tableau de comparaison affiche :

```
  Stops (daily SL)   : —     —     24
  Ghosts (no exit)   : —     —     19
  PnL%               : +267 %  +168 %  +31 %
```

Les deux colonnes de backtest sont optimistes par construction pour cette session.
L'interprétation correcte est :

1. Les chiffres **+267 % / +168 %** représentent le plafond — ce que la stratégie
   *atteindrait* si tous les trades se résolvaient proprement (pas de stops forcés, pas
   de sorties manquantes).
2. Le **+31,4 %** bot réel est le plancher — la performance réelle incluant tous les frictions.
3. L'écart résiduel après soustraction de l'impact STOP/GHOST s'explique par des pertes
   réelles supplémentaires, le slippage d'exécution, et les lacunes de couverture des snapshots.

### L'angle mort des 5 secondes — origine des pertes supplémentaires

Le moteur de backtest lit une ligne par marché toutes les ~5 secondes (l'intervalle de snapshot).
Le bot live traite **chaque tick WebSocket** — qui peut arriver plusieurs fois par seconde.

Quand le best_bid d'un marché plonge brièvement à ≤ 0,01 (le seuil LOSS) entre deux
snapshots puis remonte, voici ce qui se passe :

```
Timeline d'un trade LOSS invisible pour le backtest :

  t= 0s  snapshot → bid=0,96  ← backtest entre en trade
  t= 5s  snapshot → bid=0,82
  t=10s  snapshot → bid=0,06  ← backtest voit ça (pas encore 0,01)
  t=12,3s tick WebSocket → bid=0,01  ← bot live : LOSS enregistré
  t=12,5s tick WebSocket → bid=0,04  (le marché remonte)
  t=15s  snapshot → bid=0,04  ← backtest voit la remontée, continue
  ...
  t=240s snapshot → bid=0,98  ← backtest résout en WIN
```

Preuves issues de paper3.db : chaque trade LOSS contient de nombreux snapshots
(14 à 88 pendant le trade), mais le bid minimum jamais capturé dans ces snapshots
est 0,01–0,06 — le plongeon à ≤0,01 qui a déclenché le LOSS live n'apparaît nulle part
dans les données de snapshots. La durée moyenne d'un trade LOSS est 24 minutes
(1 477 secondes), ce qui laisse largement le temps de capturer la chute — mais celle-ci
est trop brève (quelques centaines de millisecondes) pour être enregistrée.

**Cet écart est structurel et non corrigeable** sans stocker chaque tick WebSocket
(~100× plus de données). Il ne dépend pas des paramètres de stratégie — chaque
configuration subit le même angle mort de manière égale. Les classements du sweep
restent donc valides pour comparer les configurations entre elles.

### Ce que cela implique pour les décisions de stratégie

- Ne **pas** utiliser directement le PnL% du backtest aligné comme prédiction de la
  performance live sur des sessions avec beaucoup de trades STOP ou GHOST.
- Un ratio `(PnL% bot réel) / (PnL% backtest aligné)` bien inférieur à 1,0 est attendu et
  **normal** quand les comptages STOP+GHOST sont significatifs par rapport au total des trades.
- Le sweep / grid search reste valide pour classer les configurations les unes par rapport
  aux autres — l'écart STOP/GHOST affecte toutes les configurations également (il dépend
  des conditions de la session, pas des paramètres de stratégie).
- Pour réduire l'écart : abaisser le stop-loss journalier par rapport à la mise pour que
  moins d'événements STOP se produisent, ou accepter que la performance live sera
  structurellement inférieure au backtest de cette marge.

---

## 9. Workflow complet — `strategy_compare.sh`

Le script shell `scripts/strategy_compare.sh` automatise le workflow d'optimisation complet :

```bash
bash scripts/strategy_compare.sh              # défaut : top 10, tri par ratio
bash scripts/strategy_compare.sh --top 20     # afficher les 20 meilleures configs uniques
bash scripts/strategy_compare.sh --sort pnl   # trier par PnL total
bash scripts/strategy_compare.sh --db data/liveweek.db  # une seule DB
bash scripts/strategy_compare.sh --no-save    # afficher seulement, pas de fichier rapport
```

Il s'exécute en deux sections :

1. **Section 1 — Recherche en grille** (`--sweep-all`) : 405 combinaisons sur toutes les DB.
2. **Section 2 — Comparaison par DB** (`--compare`) : tableau trois colonnes pour chaque DB.

La sortie est sauvegardée dans `reports/strategy_compare_AAAAMMJJ_HHMMSS.txt` et affichée simultanément sur stdout.

### Options

| Flag | Défaut | Description |
|------|--------|-------------|
| `--top N` | 10 | Nombre de configs uniques dans le tableau de sweep. |
| `--sort METRIC` | `ratio` | Tri : `ratio`, `pnl`, ou `wr`. |
| `--db PATH` | toutes les DB | Restreindre à un seul fichier DB. |
| `--out FILE` | horodatage auto | Remplacer le chemin du fichier rapport. |
| `--no-save` | désactivé | Afficher seulement, ne pas écrire de fichier. |

---

## 10. Fichiers de stratégie JSON

Les paramètres de stratégie sont stockés dans `strategies/` :

| Fichier | Statut | Description |
|---------|--------|-------------|
| `polymarket_BTC5M.json` | v1 — référence | Paramètres originaux (threshold=0,96, ratio=3,61). |
| `polymarket_BTC5M_v2.json` | **v2 — actif** | Optimisé sweep-all 2026-05-08 (threshold=0,95, ratio=4,42). |

### Paramètres v2

```json
{
    "signal_threshold":   0.95,
    "min_secs_remaining": 45,
    "obi_reject_thresh":  -0.75,
    "daily_stop_loss":    30.0,
    "stake":              10.0,
    "capital_start":      100.0
}
```

Le bot charge la stratégie au démarrage. Pour activer une nouvelle version après avoir mis à jour le JSON :

```bash
bash scripts/start_bot.sh   # redémarre et recharge la stratégie
```

---

## 11. Interpréter les résultats et prendre des décisions

### Mon résultat de backtest est-il bon ?

| Métrique | Cible | Alerte |
|----------|-------|--------|
| WR% | 97–99 % | < 97 % avec seuil élevé = quelque chose ne va pas |
| Ratio PnL/DD | ≥ 3,5 | < 2,0 = risque non justifié |
| MaxDD | < 80 $ | ≥ 100 $ = exposition journalière dangereuse |
| Trades | ≥ 500 au total | < 500 = données insuffisantes pour des statistiques fiables |

### Dois-je mettre à jour la stratégie ?

| Verdict | Condition | Action |
|---------|-----------|--------|
| **KEEP** | Meilleur ratio trouvé ≤ ratio actuel + 0,3 | Aucun changement nécessaire. La config actuelle est quasi-optimale. |
| **MONITOR** | Meilleur ratio trouvé > ratio actuel + 0,3 mais total trades < 500, ou bot réel sous-performe le backtest aligné de > 3× en PnL% | Collecter plus de données live avant de décider. |
| **UPDATE** | Meilleur ratio trouvé > ratio actuel + 0,3 ET nombre de trades dans ±30 % de la config actuelle | Créer un nouveau fichier de stratégie versionné et mettre à jour le bot. |

### Créer une nouvelle version de stratégie

Quand une meilleure configuration est trouvée :

1. Copier le fichier de stratégie actuel :
   ```bash
   cp strategies/polymarket_BTC5M_v2.json strategies/polymarket_BTC5M_v3.json
   ```
2. Modifier les paramètres dans le nouveau fichier.
3. Mettre à jour le champ `_description` avec la date et le nouveau ratio.
4. Mettre à jour le chemin par défaut dans `tradinebotte-polymarket/live_bot.py` (rechercher `polymarket_BTC5M_piste3.json`).
5. Lancer la suite de tests pour confirmer que tout passe :
   ```bash
   bash scripts/run_tests.sh
   ```
6. Redémarrer le bot :
   ```bash
   bash scripts/start_bot.sh
   ```

### Paramètres à ne pas modifier sans backtest complet

`WIN_THRESHOLD` (0,99), `LOSS_THRESHOLD` (0,01), `FEE_RATE` (2 %) et `STAKE` ne sont pas balayés. Les modifier invalide toutes les comparaisons de backtest existantes. Les tests de régression (`TestParamConsistency`) garantissent que `live_bot.py` et `backtest.py` sont toujours en accord sur ces valeurs.

---

## 12. Autres scripts de backtest

Les sections précédentes documentent `analysis/backtest.py` (Polymarket). Les scripts suivants couvrent les stratégies CEX et d'accumulation et partagent la même philosophie : rejeu historique → stats → sweep optionnel.

### `scripts/backtest_accumulation.py` — Accumulation BTC long terme

Récupère les klines 1h Binance (BTCUSDT) en live et rejoue la stratégie d'accumulation depuis `tradinebotte-cex/strategies/accumulation/btc_accumulation.json`.

```bash
# Défaut : historique depuis 2024-09-01 avec proxy dip
python3 scripts/backtest_accumulation.py

# Plage de dates spécifique
python3 scripts/backtest_accumulation.py --start 2026-01-01 --end 2026-06-30

# Proxy OBI (EMA taker-buy) à la place du proxy prix
python3 scripts/backtest_accumulation.py --proxy obi

# Afficher le journal complet des trades
python3 scripts/backtest_accumulation.py --trades
```

**Flags principaux :**

| Flag | Défaut | Description |
|------|--------|-------------|
| `--start` | 2024-09-01 | Date de début (YYYY-MM-DD) |
| `--end` | aujourd'hui | Date de fin |
| `--proxy` | dip | Signal de scale-in : `dip` (chute de prix depuis le haut N bougies) ou `obi` (EMA taker-buy) |
| `--dip-pct` | 4,0 | Proxy dip : % de chute depuis le haut récent pour déclencher |
| `--dip-lookback` | 72 | Proxy dip : fenêtre glissante du haut en bougies |
| `--capital` | depuis JSON | Override capital_usdt |

**Note :** `--dip-pct` et `--dip-lookback` sont des paramètres de simulation uniquement — ils n'ont pas d'équivalent dans le bot live.

**Validation :** Utilisez toujours un split train/test lors du réglage des paramètres. Utilisez 2024-09 → 2025-12 en train et 2026-01 → aujourd'hui en test hors-échantillon.

### `analysis/backtest_orderbook.py` — Scalping orderbook

Rejoue la stratégie orderbook_bot sur des données `ob_snapshots` d'un fichier `live_ob_*.db`.

```bash
python3 analysis/backtest_orderbook.py
python3 analysis/backtest_orderbook.py --db data/live_ob_2026-05-26.db
python3 analysis/backtest_orderbook.py --sweep                      # grille 576 combos
python3 analysis/backtest_orderbook.py --sweep --csv results/ob_sweep.csv
```

**Note :** Les bases créées avant le correctif M-1 (2026-05-23) n'ont pas la colonne `tfi` et ne peuvent pas être utilisées.

### `analysis/backtest_grid.py` — Stratégie grid CEX

Rejoue une stratégie de grille statique ou suivante sur des bases OHLCV (klines 1 minute).

```bash
python3 analysis/backtest_grid.py --all                     # toutes les BTCUSDT_1m*.db
python3 analysis/backtest_grid.py --all --trail bear        # grille suivante baissière
python3 analysis/backtest_grid.py --all --sweep             # sweep 15 combos
python3 analysis/backtest_grid.py --all --compare           # statique vs suivante
```

**Flags principaux :** `--range` (±% autour du centre), `--levels` (nombre d'ordres), `--trail {off,bear,bull,both}`, `--sort {calmar,pnl}`.

### `analysis/backtest_swing_dca.py` — Swing / DCA CEX

Rejoue les stratégies DCA, Swing ou SwingHold sur des bases OHLCV.

```bash
python3 analysis/backtest_swing_dca.py --all-dbs --compare   # 3 stratégies × 3 régimes
python3 analysis/backtest_swing_dca.py --strategy dca --sweep
```

**Flags principaux :** `--all-dbs` (bull 2024 + bear 2022 + range 2026), `--compare` (3 stratégies côte à côte), `--sweep` (grille tp_pct × sl_pct).

### `analysis/backtest_volfilter.py` — Filtre de volatilité Polymarket

Teste un filtre bid-volatilité / range / OBI-volatilité sur une base snapshots Polymarket. Lance automatiquement une comparaison de 3 scénarios.

```bash
python3 analysis/backtest_volfilter.py
python3 analysis/backtest_volfilter.py --db data/polymarket_5M_c2_*.db --sweep
```

**Note :** Ce script ne supporte pas `--help` ; l'appeler directement lance la comparaison.

### `analysis/backtest_stake_secs.py` — Optimisation de la mise

Optimise la mise par trade en fonction des secondes restantes et de la confiance bid. Teste trois formes de courbe (A = continue, B = step function, C = Kelly par bucket).

```bash
python3 analysis/backtest_stake_secs.py --db data/polymarket_5M_c2_*.db --curve all --top 15
```

**Note :** La courbe C (Kelly) produit de gros gains en-échantillon. Validez avec walk-forward avant tout usage en live.

### `analysis/backtest_scalping.py` — Scalping CEX

Rejoue une stratégie de scalping court terme sur des klines 1 minute.

```bash
python3 analysis/backtest_scalping.py
python3 analysis/backtest_scalping.py --compare   # 3 configs côte à côte
```

### `analysis/backtest_cycle_strategy.py` — Stratégie cycle BTC

Backtest d'une stratégie de régime basée sur le Mayer Multiple sur le dataset journalier long terme.

```bash
python3 analysis/backtest_cycle_strategy.py
python3 analysis/backtest_cycle_strategy.py --compare
python3 analysis/backtest_cycle_strategy.py --top-mm 2.2 --bot-mm 0.8
```
