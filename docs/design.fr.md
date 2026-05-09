# Architecture multi-processus et flux de messages ZeroMQ

> 🇬🇧 [English version](design.md)

Ce document décrit l'architecture multi-processus de tradinebotte : comment
les processus sont organisés, ce que chacun fait, et comment ils communiquent
via ZeroMQ.

---

## 1. Modes de déploiement

Deux modes de déploiement existent. Ils partagent le même code et les mêmes
fichiers de stratégie.

### Option A — Autonome (processus unique)

```
Polymarket WebSocket
        │
        ▼
  ┌───────────────┐
  │  live_bot.py  │  ← WebSocket + évaluation signal + ordres + BD
  └───────────────┘
```

Un seul processus fait tout : maintient la connexion WebSocket, évalue les
signaux, place les ordres, écrit la base de données. Utilisé pour les
configurations mono-compte.

```bash
python3 bot/live_bot.py
```

### Option B — Multi-bot (feed + N bots de compte)

```
Polymarket WebSocket
        │
        ▼
  ┌──────────┐
  │ feed.py  │  ← WebSocket uniquement, pas de clés, pas de trading
  └────┬─────┘
       │ ZeroMQ PUB  tcp://127.0.0.1:5557
       │
  ┌────┴──────────────────────┐
  ▼                           ▼
┌──────────────────┐  ┌──────────────────┐
│  account_bot.py  │  │  account_bot.py  │  (N instances, isolées)
│  ~/compte-a      │  │  ~/compte-b      │
└──────────────────┘  └──────────────────┘
```

Le feed détient la connexion WebSocket unique et diffuse tous les événements.
Chaque bot de compte exécute la pile de trading complète de façon indépendante
avec ses propres clés, sa propre base de données et ses propres paramètres de
stratégie.

```bash
python3 bot/feed.py &
TRADINEBOTTE_DIR=~/compte-a python3 bot/account_bot.py &
TRADINEBOTTE_DIR=~/compte-b python3 bot/account_bot.py &
```

**Démarrage automatique du feed :** le premier `account_bot.py` à démarrer
lance `feed.py` automatiquement. Un verrou fichier POSIX
(`/tmp/tradinebotte-feed/feed-<hash>.lock`) garantit qu'un seul bot de compte
démarre le feed ; les autres attendent que le feed soit joignable avant de
se connecter.

---

## 2. Inventaire des processus

| Processus | Fichier | Rôle | Credentials | Socket ZMQ |
|---|---|---|---|---|
| `live_bot` | `bot/live_bot.py` | Bot autonome : WebSocket + signal + ordres + BD | Clé privée requise | Aucun (pas de ZMQ) |
| `feed` | `bot/feed.py` | Relais WebSocket diffusion seule | **Aucune** | PUB bind `:5557` |
| `account_bot` | `bot/account_bot.py` | Logique de trading par compte | Clé privée requise | SUB connect `:5557` |
| `indicators` | `bot/indicators.py` | Étage pipeline d'indicateurs techniques | Aucune | SUB connect `:5557`, PUB bind `:5559` |

---

## 3. Topologie ZeroMQ

```
                ┌──────────────────────────────────────────────────────┐
                │               SYSTÈMES EXTERNES                     │
                │  Polymarket WebSocket (wss://ws-*.clob...)           │
                │  API REST Gamma (https://gamma-api.poly...)          │
                └─────────────────────┬────────────────────────────────┘
                                      │ connexion WS unique
                                      ▼
                            ┌──────────────────┐
                            │    feed.py        │
                            │  PUB bind :5557   │
                            └─────────┬─────────┘
                                      │ diffuse : market / book / ping
                        ┌─────────────┼─────────────────┐
                        ▼             ▼                  ▼
             ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
             │ account_bot  │  │ account_bot  │  │  indicators.py   │
             │ SUB :5557    │  │ SUB :5557    │  │  SUB :5557       │
             │ ~/compte-a   │  │ ~/compte-b   │  │  PUB bind :5559  │
             └──────────────┘  └──────────────┘  └────────┬─────────┘
                                                           │ indicators
                                                           ▼
                                                  ┌─────────────────┐
                                                  │  tout consumer  │
                                                  │  SUB :5559      │
                                                  └─────────────────┘
```

### Patrons de socket utilisés

| Patron | Direction | Utilisé par |
|---|---|---|
| `zmq.PUB` bind | 1 → N diffusion | `feed.py`, `indicators.py` |
| `zmq.SUB` connect | N → 1 réception | `account_bot.py`, `indicators.py` |

Tous les messages sont des objets JSON mono-frame. ZeroMQ garantit la
livraison atomique de chaque frame — jamais de message partiel.

### Adresses par défaut

| Variable | Défaut | Bindé par | Connecté par |
|---|---|---|---|
| `TRADINEBOTTE_FEED_ADDR` | `tcp://127.0.0.1:5557` | `feed.py` | `account_bot.py`, `indicators.py` |
| `TRADINEBOTTE_INDICATORS_ADDR` | `tcp://127.0.0.1:5559` | `indicators.py` | tout consumer |

Les deux peuvent être surchargées via variables d'environnement pour faire
tourner plusieurs stacks feed+compte indépendants sur la même machine (ex.
port 5557 pour le stack A, 5558 pour le stack B).

---

## 4. Catalogue des messages

Tous les messages partagent un champ discriminant `"t"`.

### `market` — nouveau marché découvert

Publié par `feed.py` quand un marché entre dans la fenêtre ±6 minutes.
Re-publié aussi après chaque reconnexion WebSocket (les consommateurs traitent
les doublons comme des no-ops).

```json
{
  "t":           "market",
  "market_id":   "0xabc…",
  "question":    "Bitcoin Up or Down — 5 minutes (13:00 UTC)",
  "up_token_id": "1234…",
  "dn_token_id": "5678…",
  "start_ms":    1745664000000,
  "end_ms":      1745664300000
}
```

| Champ | Type | Description |
|---|---|---|
| `market_id` | string | ID de condition Polymarket |
| `question` | string | Titre du marché (≤80 caractères) |
| `up_token_id` | string | ID du token UP/YES |
| `dn_token_id` | string | ID du token DOWN/NO |
| `start_ms` | int | Timestamp d'ouverture du marché (Unix ms) |
| `end_ms` | int | Timestamp de fermeture du marché (Unix ms) |

Consommateurs : `account_bot.py` (enregistre le marché dans `BotState`).

---

### `book` — mise à jour du carnet d'ordres

Publié par `feed.py` à chaque événement WebSocket `book`, `price_change` ou
`last_trade_price`. Haute fréquence ; pilote l'évaluation des signaux.

```json
{
  "t":        "book",
  "token_id": "1234…",
  "best_bid": 0.97,
  "best_ask": 0.975,
  "spread":   0.005,
  "bid_vol":  120.50,
  "ask_vol":  80.00,
  "obi":      0.20
}
```

| Champ | Type | Description |
|---|---|---|
| `token_id` | string | Token dont le carnet a changé |
| `best_bid` | float | Meilleur prix acheteur (0–1) |
| `best_ask` | float | Meilleur prix vendeur (0–1) |
| `spread` | float | `best_ask − best_bid` |
| `bid_vol` | float | Profondeur agrégée top-5 côté achat (USD) |
| `ask_vol` | float | Profondeur agrégée top-5 côté vente (USD) |
| `obi` | float | Déséquilibre du carnet : `(bid_vol − ask_vol) / (bid_vol + ask_vol)` ∈ [−1, +1] |

Consommateurs : `account_bot.py` (évaluation signal), `indicators.py`
(accumulation de prix).

---

### `ping` — keepalive

Publié par `feed.py` toutes les 10 secondes. L'absence de pings indique un
crash du feed ou un problème réseau.

```json
{"t": "ping", "ts": 1745664123456}
```

Consommateurs : ignoré par `account_bot.py` ; utile pour le monitoring externe.

---

### `indicators` — indicateurs techniques

Publié par `indicators.py` une fois que le buffer d'historique de prix par
token atteint `--min-ticks` (défaut 25) et que toutes les périodes
d'indicateurs sont satisfaites.

```json
{
  "t":        "indicators",
  "token_id": "1234…",
  "ts":       1745664125000,
  "rsi_14":   72.3,
  "sma_20":   0.9612,
  "ema_9":    0.9634,
  "vol_20":   0.0021
}
```

| Champ | Type | Description |
|---|---|---|
| `token_id` | string | Token auquel les indicateurs se rapportent |
| `ts` | int | Horodatage de publication (Unix ms) |
| `rsi_N` | float | RSI(N) — formule de Wilder, 0–100 |
| `sma_N` | float | Moyenne mobile simple des N dernières valeurs `best_bid` |
| `ema_N` | float | Moyenne mobile exponentielle (k = 2/(N+1)), amorcée avec SMA |
| `vol_N` | float | Volatilité glissante : écart-type population des log-rendements sur N prix |

Le suffixe numérique encode la période configurée (ex. `rsi_14`, `sma_20`).
Aucun message n'est publié tant qu'au moins un indicateur n'est pas calculable.

Consommateurs : tout processus souscrivant au port 5559.

---

## 5. Mécanisme de démarrage automatique du feed

En mode multi-bot, la gestion manuelle du feed n'est pas nécessaire. Le
premier `account_bot.py` à démarrer lance `feed.py` automatiquement.

```
account_bot démarre
    │
    ├─── sonde le feed (TCP connect, réception sous 5 s) ?
    │       OUI ──▶ connecte SUB, commence le trading
    │
    │       NON
    │        │
    │        ├─── essaie LOCK_EX | LOCK_NB sur le fichier verrou
    │        │       ÉCHEC (un autre bot a le verrou) ──▶ attend LOCK_SH ──▶ connecte SUB
    │        │
    │        │       SUCCÈS (on détient le verrou exclusif)
    │        │            │
    │        │            ├─── subprocess.Popen(feed.py, env=env_minimal)
    │        │            ├─── sonde jusqu'à ce que le feed réponde (30 s max)
    │        │            └─── libère le verrou ──▶ connecte SUB
    │        │
    └─────────────────────────────────────────────────────────
```

**Fichier verrou :** `/tmp/tradinebotte-feed/feed-<hash(addr)>.lock`
Le hash est dérivé de `TRADINEBOTTE_FEED_ADDR` : chaque stack indépendant
(port différent) obtient son propre verrou.

**Environnement minimal :** `feed.py` hérite uniquement de `PATH`, `HOME`,
`LANG`, `VIRTUAL_ENV`, `PYTHONPATH`, `LC_ALL`, `LC_CTYPE` et
`TRADINEBOTTE_FEED_ADDR`. La clé privée `POLY_PRIVATE_KEY` du parent n'est
jamais transmise au sous-processus feed.

---

## 6. Isolation des processus

Chaque instance `account_bot.py` est un **processus OS distinct** avec sa
propre copie du module `live_bot`. Cela garantit :

| Ressource | Isolée ? | Notes |
|---|---|---|
| Base SQLite (`live.db`) | ✅ | `TRADINEBOTTE_DIR` distinct par compte |
| Fichier log (`account.log`) | ✅ | `TRADINEBOTTE_DIR` distinct par compte |
| Clé privée | ✅ | Lue depuis le `config.json` propre au compte |
| Paramètres de stratégie | ✅ | Dossier `strategies/` distinct par compte |
| État du capital | ✅ | `BotState` en mémoire, reconstruit depuis la BD locale |
| Compteur stop-loss journalier | ✅ | Cache `state.daily_pnl` par processus |
| Déduplication de signal | ✅ | Set `state.signalled` par processus |

Le feed est entièrement **agnostique au signal** : il publie chaque mise à
jour brute du carnet sans filtrage. L'évaluation du signal, le placement des
ordres et le suivi des trades se font indépendamment dans chaque bot de compte.

---

## 7. Pipeline d'indicateurs

`indicators.py` est un étage pipeline optionnel et sans état. Il ne trade pas.

```
Ring-buffer par token (deque, maxlen=200)
    │
    │  push(best_bid) à chaque message "book"
    │
    ├── RSI(N)    Wilder : gain_moyen / perte_moyenne sur N derniers deltas
    ├── SMA(N)    moyenne(prices[-N:])
    ├── EMA(N)    itératif : ema = prix*k + ema*(1−k),  k = 2/(N+1)
    └── Vol(N)    écart-type des log-rendements sur N+1 derniers prix
         │
         └── publie "indicators" quand les quatre sont non-None
```

Les indicateurs retournent `None` tant que le buffer n'a pas assez d'historique.
Aucun message n'est publié tant qu'un seul indicateur n'est pas calculable :
les consommateurs ne reçoivent jamais de données partielles.

**Partage multi-bots** — `indicators.py` exécute une tâche asyncio par flux configuré (une pour `btc_4h`, une pour `btc_1d`). Tous les messages de sortie sont publiés sur le même socket PUB. Chaque `account_bot` abonné reçoit *tous* les messages et filtre côté client selon `stream_id`. Aucun processus ni port supplémentaire.

**Démarrage du pipeline :**

```bash
# Démarrer le feed en premier (ou laisser account_bot le lancer)
python3 bot/feed.py &

# Démarrer les indicateurs (souscrit au feed, publie sur :5559)
python3 bot/indicators.py &

# Un consommateur souscrit à :5559
# (tout script Python avec zmq.SUB connectant tcp://127.0.0.1:5559)
```

---

## 8. Ordre de démarrage

Pour l'Option B avec indicateurs :

```
1. feed.py           bind  :5557   (ou démarrage automatique par le 1er account_bot)
2. indicators.py     SUB→  :5557   / bind :5559
3. account_bot(s)    SUB→  :5557
4. consommateurs     SUB→  :5559
```

ZeroMQ PUB/SUB est **sans état de connexion côté publisher** : le socket PUB
continue de fonctionner qu'il y ait ou non des clients SUB connectés. Les
messages publiés avant qu'un SUB se connecte sont perdus (pas de buffering
côté publisher). Cela signifie que démarrer les indicateurs après le feed
n'entraîne aucune perte de données — les éventuels messages `market` manqués
sont re-publiés au prochain refresh de 30 secondes.

---

## 9. Récapitulatif des variables d'environnement

| Variable | Défaut | Portée | Description |
|---|---|---|---|
| `TRADINEBOTTE_FEED_ADDR` | `tcp://127.0.0.1:5557` | feed.py, account_bot.py, indicators.py | Adresse ZMQ bindée par feed.py et utilisée par les consommateurs |
| `TRADINEBOTTE_INDICATORS_ADDR` | `tcp://127.0.0.1:5559` | indicators.py | Adresse ZMQ bindée par indicators.py et utilisée par les consommateurs |
| `TRADINEBOTTE_DIR` | `~/tradinebotte` | account_bot.py, live_bot.py | Répertoire de données par compte (BD, log, config, stratégies) |

---

## 10. Fichiers liés

| Fichier | Rôle |
|---|---|
| `bot/live_bot.py` | Bot autonome (Option A) |
| `bot/feed.py` | Diffuseur ZMQ (Option B) |
| `bot/account_bot.py` | Abonné par compte + logique de trading |
| `bot/indicators.py` | Étage pipeline d'indicateurs techniques |
| `docs/multi.fr.md` | Guide de configuration Option B et stratégies par compte |
| `docs/GridTrading.fr.md` | Architecture de la stratégie grid et config JSON |
| `tests/test_multibot.py` | Tests d'intégration ZMQ pour feed + account_bot |
| `tests/test_indicators.py` | Tests unitaires pour les maths d'indicateurs et PriceSeries |
