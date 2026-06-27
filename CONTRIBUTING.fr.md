# Contribuer

> 🇬🇧 [English version](CONTRIBUTING.md)

## Table des matières

- [Environnement de développement](#environnement-de-développement)
- [Structure du projet](#structure-du-projet)
- [Exécution des tests](#exécution-des-tests)
- [Qualité du code](#qualité-du-code)
- [Workflow git](#workflow-git)
- [Style des messages de commit](#style-des-messages-de-commit)
- [Processus de release](#processus-de-release)
- [Politique de langue](#politique-de-langue)
- [Règle de documentation bilingue](#règle-de-documentation-bilingue)
- [Règles de sécurité](#règles-de-sécurité)
- [Ajouter un adaptateur d'exchange](#ajouter-un-adaptateur-dexchange)
- [Ajouter un moteur de stratégie](#ajouter-un-moteur-de-stratégie)
- [Modifier du code partagé : la règle de symétrie](#modifier-du-code-partagé--la-règle-de-symétrie)

---

## Environnement de développement

**Prérequis** : Python 3.8+, Linux ou macOS.

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte

# Installer la bibliothèque partagée comme package éditable (requis par tous les sous-systèmes)
pip install -e tradinetools/

# Installer les dépendances de développement (pylint, mypy, pip-audit)
pip install -r requirements-dev.txt
```

Recommandé : utiliser `uv` pour un virtualenv isolé plus rapide :

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements-dev.txt
pip install -e tradinetools/
```

Le lanceur de tests (`scripts/run_tests.sh`) détecte automatiquement `.venv` à la racine du projet.

**Activer le hook pre-commit** (une seule fois par clone — bloque les commits exposant des données d'infrastructure) :

```bash
git config core.hooksPath .git-hooks
```

---

## Structure du projet

```
tradinebotte/
├── tradinebotte-cex/            # Bots CEX et moteurs de stratégie
│   ├── accumulation_bot.py      # Bot d'accumulation OBI (v1.5)
│   ├── orderbook_bot.py         # Bot de scalping OBI (v2.12)
│   ├── api_binance.py           # Adaptateur Binance spot
│   ├── api_mexc.py              # Adaptateur MEXC spot
│   ├── api_mexc_futures.py      # Adaptateur MEXC Futures perpétuel
│   ├── api_bitstamp.py          # Adaptateur Bitstamp spot
│   ├── api_common.py            # Helpers partagés : parse_levels(), book_snapshot(), hmac_sign()
│   ├── earn_manager.py          # Gestionnaire Binance Simple Earn Flexible
│   ├── connectors/__init__.py   # validate() — vérification de compatibilité connecteur/stratégie
│   ├── strategy_engines/        # Moteurs de stratégie modulaires
│   │   ├── base.py              # Interface BaseStrategy
│   │   ├── grid.py              # Grid (static / trail=bear / trail=bull)
│   │   ├── swing.py             # Swing avec filtres EMA200 + ATR + RSI
│   │   ├── swinghold.py         # SwingHold — ventes fractionnées, accumulation long terme
│   │   └── dca.py               # DCA cadencé avec TP/SL
│   ├── strategies/              # Fichiers de config JSON par stratégie
│   └── tests/                   # Tests unitaires spécifiques CEX
│
├── tradinebotte-indicators/     # Pipeline de signaux ZMQ
│   ├── indicators.py            # Pipeline principal (RSI, EMA, OBI, TFI, liquidations)
│   ├── strategies/              # Fichiers de config de flux (indicators_all.json, ...)
│   └── tests/
│
├── tradinebotte-polymarket/     # Connecteur marchés de prédiction Polymarket
│   ├── live_bot.py              # Machine d'état async
│   ├── feed.py                  # Feed WebSocket (ZMQ PUB)
│   ├── account_bot.py           # Bot par compte (ZMQ SUB)
│   ├── api_polymarket.py        # Adaptateur CLOB Polymarket
│   ├── strategies/              # Fichiers de stratégie JSON
│   └── tests/
│
├── tradinebotte-status/         # Monitoring de santé
│   ├── status_collector.py      # Collecteur de heartbeats (ZMQ → SQLite)
│   └── generate_status.py       # Générateur de tableau de bord HTML
│
├── tradinetools/                # Bibliothèque partagée (pip install -e tradinetools/)
│   └── tradinetools/
│       ├── math.py              # sma_last, ema_last, atr_last, bollinger_last, ...
│       ├── zmq.py               # Fabriques de sockets ZMQ
│       ├── logging.py           # setup_root_logger(), setup_logger()
│       └── schemas.py           # Dataclasses de messages versionnés
│
├── analysis/                    # Scripts de backtest et d'analyse
├── scripts/                     # Scripts d'installation, déploiement, test, release
├── tests/                       # Suite de tests principale (stratégies CEX, adaptateurs API, ...)
├── docs/                        # Documentation (voir référence docs/ ci-dessous)
├── requirements.txt             # Dépendances runtime
├── requirements-dev.txt         # Dépendances dev (pylint, mypy, pip-audit)
└── version.py                   # Source unique de vérité pour le numéro de version
```

### Référence docs/

| Fichier | Contenu |
|---|---|
| [`docs/design.fr.md`](docs/design.fr.md) | Architecture multi-processus et flux de messages ZMQ |
| [`docs/accumulation.md`](docs/accumulation.md) | Conception de la stratégie d'accumulation |
| [`docs/indicators.fr.md`](docs/indicators.fr.md) | Référence du pipeline d'indicateurs |
| [`docs/GridTrading.fr.md`](docs/GridTrading.fr.md) | Fonctionnement et configuration du grid trading |
| [`docs/AdaptedGridTrading.fr.md`](docs/AdaptedGridTrading.fr.md) | Résultats de backtest grid et guide de sélection |
| [`docs/snapshots.md`](docs/snapshots.md) | Schéma de la table snapshots et requêtes de référence |
| [`docs/logging.md`](docs/logging.md) | Vocabulaire canonique des tags de log |
| [`docs/KellySizing.md`](docs/KellySizing.md) | Conception du sizing Kelly fractionnel |
| [`docs/multi.fr.md`](docs/multi.fr.md) | Architecture multi-bot WebSocket |
| [`docs/HOWTO_tests_and_backtests.fr.md`](docs/HOWTO_tests_and_backtests.fr.md) | Guide pratique des tests et backtests |

---

## Exécution des tests

```bash
bash scripts/run_tests.sh
```

1163 tests répartis en 6 suites. Aucun accès réseau ni credentials requis — base SQLite en mémoire pour chaque test.

Le script exécute également pylint sur tous les fichiers `.py` suivis par git. Pour lancer une suite individuelle :

```bash
# Suite principale (stratégies CEX, adaptateurs API, moteur de backtest)
python3 -m unittest discover -s tests/ -p "test_*.py" -v

# Sous-système spécifique
python3 -m unittest discover -s tradinebotte-cex/tests/ -p "test_*.py" -v
python3 -m unittest discover -s tradinebotte-indicators/tests/ -p "test_*.py" -v
python3 -m unittest discover -s tradinebotte-polymarket/tests/ -p "test_*.py" -v
python3 -m unittest discover -s tradinebotte-status/tests/ -p "test_*.py" -v
python3 -m unittest discover -s tradinetools/tests/ -p "test_*.py" -v
```

Tout nouveau code doit être accompagné de tests. Aucune exception pour la logique de stratégie, les adaptateurs API ou les nouvelles fonctions utilitaires.

---

## Qualité du code

```bash
# Linter — cible : ≥ 9,90/10 ; en dessous, la release est bloquée
pylint tradinebotte-cex tradinebotte-indicators tradinebotte-polymarket tradinebotte-status tradinetools

# Vérificateur de types — doit retourner 0 erreur
mypy tradinebotte-polymarket tradinebotte-cex tradinebotte-indicators tradinetools --ignore-missing-imports

# Scripts shell — doivent passer shellcheck au niveau warning
shellcheck -S warning scripts/*.sh tradinebotte-*/scripts/*.sh
```

Les trois vérifications sont exécutées automatiquement par `scripts/prepare_release.sh`. Un score pylint inférieur à 9,90 bloque la release.

---

## Workflow git

- `main` — releases stables uniquement ; ne jamais pousser directement
- `dev` — développement actif ; tout le travail cible `dev`
- Branches de fonctionnalités : partir de `dev`, ouvrir une PR ciblant `dev`
- Merger `dev → main` uniquement après avoir exécuté `scripts/prepare_release.sh` (voir [Processus de release](#processus-de-release))

---

## Style des messages de commit

```
type: description courte à l'impératif

Corps optionnel si le pourquoi n'est pas évident.
```

Types utilisés dans ce projet :

| Préfixe | Quand l'utiliser |
|---|---|
| `fix:` | Correction de bug |
| `feat:` | Nouvelle fonctionnalité |
| `refactor:` | Restructuration du code sans changement de comportement |
| `docs:` | Modifications de documentation uniquement |
| `test:` | Tests nouveaux ou mis à jour |
| `scripts:` | Scripts de build, déploiement ou outillage |
| `logging:` | Modifications du logging uniquement |
| `release(vX.Y):` | Commit de préparation de release |

Garder la ligne de sujet sous 72 caractères. Référencer les issues ou PRs dans le corps, pas dans le sujet.

---

## Processus de release

Avant chaque merge de `dev` vers `main` :

```bash
bash scripts/prepare_release.sh
```

Le script exécute 7 vérifications dans l'ordre :

| Étape | Vérification | Bloquante ? |
|---|---|---|
| 1 | Tests unitaires (6 suites) | Oui |
| 2 | Score pylint ≥ 9,90 | Oui |
| 3 | Shellcheck propre sur tous les fichiers `.sh` | Oui |
| 4 | Les 10 fichiers de documentation bilingue présents | Oui |
| 5 | Fraîcheur du CHANGELOG (dernière entrée = aujourd'hui) | Avertissement |
| 6 | Scan qualité des données sur `data/*.db` | Avertissement |
| 7 | Tests d'intégration (si config présente) | Avertissement |

Options disponibles :
- `--skip-integration` — ignorer l'étape 7 si l'environnement de test est indisponible
- `--tag v0.XX` — créer un tag git après un run réussi

**Ne jamais merger `dev → main` sans un run entièrement vert.**

---

## Politique de langue

Le projet est **bilingue au niveau de la documentation** et **exclusivement en anglais au niveau du code** :

| Artefact | Langue |
|---|---|
| `README.md`, `CHANGELOG.md`, `INSTALL.md`, `QUICKSTART.md`, `UPDATE.md`, `CONTRIBUTING.md` | Anglais |
| `README.fr.md`, `CHANGELOG.fr.md`, `INSTALL.fr.md`, `QUICKSTART.fr.md`, `UPDATE.fr.md`, `CONTRIBUTING.fr.md` | Français |
| Code source (`.py`, `.sh`, `.json`) | **Anglais uniquement** |
| Commentaires de code | **Anglais uniquement** |
| Messages de log | **Anglais uniquement** |
| Docstrings | **Anglais uniquement** |

Ne jamais écrire en français dans le code source, les commentaires, les messages de log ou les docstrings.

---

## Règle de documentation bilingue

La documentation est maintenue sous forme de paires EN/FR. **Les deux fichiers d'une paire doivent être mis à jour dans le même commit** — ne jamais modifier l'un sans mettre à jour son équivalent :

| Anglais | Français |
|---|---|
| `README.md` | `README.fr.md` |
| `CHANGELOG.md` | `CHANGELOG.fr.md` |
| `INSTALL.md` | `INSTALL.fr.md` |
| `QUICKSTART.md` | `QUICKSTART.fr.md` |
| `UPDATE.md` | `UPDATE.fr.md` |
| `CONTRIBUTING.md` | `CONTRIBUTING.fr.md` |

L'étape 4 de `prepare_release.sh` vérifie la présence des 10 fichiers et bloque la release si l'un est manquant.

---

## Règles de sécurité

Ne jamais inclure les éléments suivants dans un fichier public (README, CHANGELOG, INSTALL, messages de commit, etc.) :

- Noms d'hôtes de serveurs ou noms de domaine
- Adresses IP
- Noms d'utilisateurs de déploiement
- Mots de passe ou tokens API

Utiliser des descriptions génériques à la place : "les comptes de déploiement", "le VPS de production", "le serveur de test".

Le hook pre-commit dans `.git-hooks/pre-commit` bloque les commits contenant des patterns sensibles connus. L'activer une fois par clone :

```bash
git config core.hooksPath .git-hooks
```

---

## Ajouter un adaptateur d'exchange

1. Créer `tradinebotte-cex/api_<exchange>.py` implémentant la même interface que `api_binance.py` :
   - `get_markets(session, symbol)` → snapshot du carnet d'ordres
   - `post_order(session, symbol, side, quantity, price)` → identifiant d'ordre
   - `post_market_order(session, symbol, side, quantity)` → identifiant d'ordre
   - Utilitaires partagés depuis `api_common.py` : `parse_levels()`, `book_snapshot()`, `hmac_sign()`

2. Ajouter des tests dans `tests/test_api_cex.py` couvrant le cas nominal, les codes d'erreur HTTP et les pannes réseau.

3. La vérification de compatibilité dans `tradinebotte-cex/connectors/__init__.py` (`validate()`) vérifie automatiquement que le nouvel adaptateur expose toutes les méthodes requises par la stratégie choisie. Aucun câblage manuel nécessaire.

---

## Ajouter un moteur de stratégie

1. Créer `tradinebotte-cex/strategy_engines/<nom>.py` en sous-classant `BaseStrategy` depuis `base.py`.
   Implémenter au minimum : `on_tick()`, `on_fill()`, `restore_state()`.

2. Créer un squelette de config JSON dans `tradinebotte-cex/strategies/<nom>/` documentant chaque paramètre.

3. Ajouter des tests dans `tradinebotte-cex/tests/test_strategy_engines.py` couvrant la logique d'entrée, de sortie, le SL/TP et la restauration d'état au redémarrage.

4. Si la stratégie consomme des données d'indicateurs, souscrire au ZMQ PUB de `indicators.py` — voir `docs/design.fr.md` pour le format des messages.

## Modifier du code partagé : la règle de symétrie

Aucune famille de stratégie n'est « principale ». Polymarket (threshold/grid), les
grid/swing CEX et l'accumulation sont des **pairs**. Une conséquence n'est pas évidente :
`live_bot.py` vit sous `tradinebotte-polymarket/` mais c'est l'entrypoint **universel** —
il exécute aussi les stratégies grid/swing CEX (sélectionnées par `strategy_type` /
`connector`). Plusieurs effets de bord sont couplés à ses fonctions partagées ; par
exemple, la persistance des snapshots vivait *à l'intérieur* de `handle_book_update`.

C'est exactement ainsi qu'est apparu le bug d'enregistrement silencieux du 2026-06-16 :
une nouvelle boucle consommatrice CEX (`cex_feed_consumer_loop`) a contourné
`handle_book_update` pour de bonnes raisons (elle n'a pas besoin du bookkeeping de tokens
Polymarket) et a **silencieusement supprimé l'effet de bord de persistance des
snapshots** qui s'y trouvait — et aucun test ne l'a attrapé, parce que cet effet de bord
n'était exercé que sur le chemin Polymarket. Les bots continuaient à battre ; seule une
table de fond a cessé de grossir, pendant ~10 jours.

**La règle :** avant de merger une modification de code partagé entre familles, énumérer
*chaque* effet de bord de la fonction modifiée ou contournée, et le vérifier **pour
toutes les familles** — pas seulement celle sur laquelle on travaille. Quand on contourne
une fonction partagée, revérifier ce qu'elle faisait *d'autre* et recréer ou tester les
parties encore nécessaires.

Checklist pour tout chemin consommateur de données nouveau ou modifié / fonction
partagée du hot-path :

- [ ] **Snapshots persistés ?** Faire passer l'écriture par l'étape partagée
      (`_persist_snapshot` dans `live_bot.py`, `_record_accum_snapshot` dans
      `accumulation_bot.py`) — ne pas inliner un `INSERT` nu.
- [ ] **Horloge de fraîcheur avancée ?** Mettre à jour `last_write_ts` à chaque ligne
      persistée. Le badge `⚠data` de la status page en dépend — un bot qui enregistre
      mais ne la met jamais à jour (ou qui n'enregistre rien) doit apparaître stale, pas
      silencieux.
- [ ] **Trades / état de stratégie** écrits dans le ledger de cette famille (`trades`,
      `grid_levels`, `swing_orders`, `accum_trades`, …) ?
- [ ] **PnL cumulé** exporté sur le heartbeat (`pnl_total`) ?
- [ ] **Un test pour chacun des points ci-dessus, pour CETTE famille.** Voir
      `docs/test_coverage_matrix.md` pour la grille actuelle et les trous. Le garde
      structurel `tradinebotte-polymarket/tests/test_bot.py::TestDataPathCoverage` fait
      échouer toute boucle consommatrice `live_bot` qui pilote une stratégie sans
      persister de snapshot — l'étendre (ou ajouter un équivalent) en ajoutant un
      consommateur ailleurs.
