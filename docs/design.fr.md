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
                │  Binance kline WebSocket + API REST                  │
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
             ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐
             │ account_bot  │  │ account_bot  │  │    indicators.py       │
             │ SUB :5557    │  │ SUB :5557    │  │    SUB :5557           │
             │ REQ :5561 ───┼──┼──────────────┼─▶│    REP bind :5561     │
             │ ~/compte-a   │  │ REQ :5561 ───┼─▶│    PUB bind :5559     │
             └──────────────┘  └──────────────┘  └───────────┬────────────┘
                                                              │ indicators
                                                              ▼
                                                   ┌─────────────────────┐
                                                   │   tout consumer     │
                                                   │   SUB :5559         │
                                                   └─────────────────────┘
```

**Flux d'enregistrement dynamique** — au démarrage, chaque `account_bot`
envoie un REQ à `:5561` décrivant les flux dont il a besoin. `indicators.py`
démarre la tâche asyncio correspondante si elle ne tourne pas déjà et répond
`{"status":"ok","stream_id":"..."}`. Toute la sortie est diffusée sur le
PUB `:5559` ; les consommateurs filtrent par `stream_id`.

```
démarrage account_bot :
  REQ → {"cmd":"subscribe","asset":"BTCUSDT","timeframe":"4h",
         "source":"binance_ws","indicators":[{"type":"rsi","period":14}]}
  REP ← {"status":"ok","stream_id":"btc_4h"}
  SUB → :5559  (filtre : stream_id == "btc_4h")
```

### Patrons de socket utilisés

| Patron | Direction | Utilisé par |
|---|---|---|
| `zmq.PUB` bind | 1 → N diffusion | `feed.py`, `indicators.py` |
| `zmq.SUB` connect | N → 1 réception | `account_bot.py`, `indicators.py` |
| `zmq.REP` bind | serveur requête/réponse | `indicators.py` |
| `zmq.REQ` connect | client requête/réponse | `account_bot.py` (au démarrage) |

Tous les messages sont des objets JSON mono-frame. ZeroMQ garantit la
livraison atomique de chaque frame — jamais de message partiel.

### Adresses par défaut

| Variable | Défaut | Bindé par | Connecté par |
|---|---|---|---|
| `TRADINEBOTTE_FEED_ADDR` | `tcp://127.0.0.1:5557` | `feed.py` | `account_bot.py`, `indicators.py` |
| `TRADINEBOTTE_INDICATORS_ADDR` | `tcp://127.0.0.1:5559` | `indicators.py` PUB | tout consumer |
| `TRADINEBOTTE_INDICATORS_REG_ADDR` | `tcp://127.0.0.1:5561` | `indicators.py` REP | `account_bot.py` (REQ au démarrage) |

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

**Stream Binance kline** — `source="binance_ws"`, les champs incluent `asset`,
`timeframe` et `stream_id` ; pas de `token_id`.

---

### `indicators` — taux de financement perpétuel Binance (`source="binance_funding"`)

Interrogé depuis `https://fapi.binance.com/fapi/v1/premiumIndex` toutes les
15 min (par défaut). Aucun calcul d'indicateur ; le taux brut est publié tel
quel.

```json
{
  "t":               "indicators",
  "stream_id":       "btc_funding",
  "funding_rate":    0.0001,
  "next_funding_ms": 1746000000000,
  "ts":              1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `stream_id` | string | Tel que déclaré dans la config (ex. `"btc_funding"`) |
| `funding_rate` | float | Taux de financement perpétuel Binance actuel (typiquement ±0,01 %) |
| `next_funding_ms` | int | Horodatage du prochain règlement de financement (Unix ms) |
| `ts` | int | Horodatage de publication (Unix ms) |

---

### `indicators` — volatilité implicite Deribit DVOL (`source="deribit_iv"`)

Interrogé depuis `https://www.deribit.com/api/v2/public/get_index_price` toutes
les 5 min (par défaut). Fournit l'indice de volatilité implicite annualisé BTC.

```json
{
  "t":         "indicators",
  "stream_id": "btc_dvol",
  "dvol":      62.5,
  "ts":        1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `stream_id` | string | Tel que déclaré dans la config (ex. `"btc_dvol"`) |
| `dvol` | float | Volatilité implicite annualisée Deribit DVOL (ex. 62,5 ≈ 62,5 %) |
| `ts` | int | Horodatage de publication (Unix ms) |

---

### `indicators` — Indice Fear & Greed (`source="fear_greed"`)

Interrogé depuis `https://api.alternative.me/fng/` toutes les 1 heure (par
défaut). L'indice va de 0 (Peur Extrême) à 100 (Avidité Extrême).

```json
{
  "t":                "indicators",
  "stream_id":        "fear_greed",
  "fear_greed":       72,
  "fear_greed_label": "Greed",
  "ts":               1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `stream_id` | string | Toujours `"fear_greed"` (indépendant de l'actif) |
| `fear_greed` | int | Valeur de l'indice 0–100 |
| `fear_greed_label` | string | Libellé textuel : `"Extreme Fear"`, `"Fear"`, `"Neutral"`, `"Greed"`, `"Extreme Greed"` |
| `ts` | int | Horodatage de publication (Unix ms) |

Consommateurs : tout processus souscrivant au port PUB des indicateurs.

---

### `indicators` — open interest futures Binance (`source="binance_oi"`)

Interrogé depuis `https://fapi.binance.com/futures/data/openInterestHist` toutes
les 5 min (par défaut). Fournit l'OI absolu et la variation signée depuis le poll
précédent.

```json
{
  "t":             "indicators",
  "stream_id":     "btc_oi",
  "oi_btc":        45690.57,
  "oi_usd":        4569056780.0,
  "oi_change_btc": 12.44,
  "oi_change_usd": 1244440.0,
  "ts":            1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `oi_btc` | float | Open interest en contrats BTC |
| `oi_usd` | float | Open interest en USD |
| `oi_change_btc` | float | Delta OI depuis le poll précédent (+ = nouveaux longs/shorts ouverts) |
| `oi_change_usd` | float | Delta OI en USD depuis le poll précédent |
| `ts` | int | Horodatage de publication (Unix ms) |

OI montant avec le prix = tendance ; OI qui chute = débouclage de positions / risque de retournement.

---

### `indicators` — ratio long/short Binance (`source="binance_ls_ratio"`)

Interrogé depuis `https://fapi.binance.com/futures/data/topLongShortAccountRatio`
toutes les 5 min (par défaut). Reflète le positionnement des comptes top-traders
(pas la taille des positions).

```json
{
  "t":                "indicators",
  "stream_id":        "btc_ls_ratio",
  "long_short_ratio": 1.2345,
  "long_pct":         0.5523,
  "short_pct":        0.4477,
  "ts":               1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `long_short_ratio` | float | `long_pct / short_pct` |
| `long_pct` | float | Fraction des comptes top-traders nets long (0–1) |
| `short_pct` | float | Fraction des comptes top-traders nets short (0–1) |
| `ts` | int | Horodatage de publication (Unix ms) |

Signal contrarian : ratio > 1,5 ou < 0,7 précède souvent un retournement.

---

### `indicators` — liquidations forcées Binance (`source="binance_liquidations"`)

Agrège `https://fapi.binance.com/fapi/v1/forceOrders` sur le dernier intervalle
de poll (5 min par défaut). Ordres `SELL` = positions longues liquidées ; ordres
`BUY` = positions courtes liquidées.

```json
{
  "t":             "indicators",
  "stream_id":     "btc_liquidations",
  "liq_long_usd":  1250000.0,
  "liq_short_usd": 80000.0,
  "liq_net_usd":   -1170000.0,
  "liq_count":     45,
  "ts":            1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `liq_long_usd` | float | Valeur USD totale des positions longues liquidées sur l'intervalle |
| `liq_short_usd` | float | Valeur USD totale des positions courtes liquidées sur l'intervalle |
| `liq_net_usd` | float | `liq_short_usd − liq_long_usd` (négatif = surtout des longs liquidés) |
| `liq_count` | int | Nombre d'ordres forcés sur l'intervalle |
| `ts` | int | Horodatage de publication (Unix ms) |

Consommateurs : tout processus souscrivant au port PUB des indicateurs.

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

**Partage multi-bots** — `indicators.py` exécute une tâche asyncio par flux. Toute la sortie est diffusée sur un seul socket PUB ; les consommateurs filtrent par `stream_id`.

**Enregistrement dynamique** — les flux peuvent être ajoutés à l'exécution sans redémarrer `indicators.py`. Chaque bot envoie un REQ au socket REP (`:5561`) avec ses besoins ; le serveur démarre la tâche si elle est nouvelle et répond avec le `stream_id` à écouter. Les flux déclarés dans la config JSON sont pré-chargés au démarrage ; les flux demandés par les bots s'ajoutent par-dessus.

**Limitation** — l'enregistrement dynamique est supporté pour toutes les sources non-feed : `"binance_ws"`, `"binance_funding"`, `"deribit_iv"`, `"fear_greed"`, `"binance_oi"`, `"binance_ls_ratio"` et `"binance_liquidations"`. Les flux feed-source (ticks Polymarket) doivent être déclarés statiquement dans le fichier de config JSON.

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
| `TRADINEBOTTE_PORT_BASE` | `5557` | feed.py, account_bot.py, indicators.py | Port de base de toute la pile. Tous les ports par défaut se décalent de `PORT_BASE − 5557`. Les variables par service restent prioritaires. |
| `TRADINEBOTTE_FEED_ADDR` | `tcp://127.0.0.1:$PORT_BASE` | feed.py, account_bot.py, indicators.py | Adresse ZMQ exacte du socket PUB feed. Remplace `PORT_BASE` pour le feed uniquement. |
| `TRADINEBOTTE_INDICATORS_ADDR` | `tcp://127.0.0.1:$(PORT_BASE+2)` | indicators.py | Adresse ZMQ PUB exacte du service indicators. |
| `TRADINEBOTTE_INDICATORS_REG_ADDR` | `tcp://127.0.0.1:$(PORT_BASE+4)` | indicators.py | Adresse ZMQ REP exacte pour l'enregistrement dynamique de flux. |
| `TRADINEBOTTE_DIR` | `~/tradinebotte` | account_bot.py, live_bot.py | Répertoire de données par compte (BD, log, config, stratégies) |

### Faire tourner deux piles indépendantes sur la même machine

```bash
# Pile A — ports par défaut (5557, 5559, 5561 …)
TRADINEBOTTE_DIR=~/compte-a python3 bot/account_bot.py &

# Pile B — tous les ports décalés de +1000
TRADINEBOTTE_PORT_BASE=6557 TRADINEBOTTE_DIR=~/compte-b python3 bot/account_bot.py &
TRADINEBOTTE_PORT_BASE=6557 TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_4h_bitcoin.json \
  bash scripts/start_indicators.sh &
```

`TRADINEBOTTE_PORT_BASE` décale également les adresses déclarées dans les
fichiers JSON de config du même offset — une seule variable déplace l'ensemble
de la plage de ports d'une pile.

---

## 10. ZeroMQ vs MQTT — analyse des compromis pour ce projet

ZeroMQ et MQTT implémentent tous deux du publish/subscribe, mais font des
choix architecturaux opposés. Cette section explique pourquoi ZeroMQ a été
retenu et quels sont les compromis concrets dans le contexte de tradinebotte.

### Différence clé : avec ou sans broker

MQTT est **basé sur un broker** : chaque message transite par un serveur
central (Mosquitto, EMQX, …). Publishers et subscribers ne communiquent jamais
directement — le broker sert d'intermédiaire, stocke les messages retenus et
applique les niveaux de QoS.

ZeroMQ est **sans broker** : le publisher bind un port TCP ; les subscribers
s'y connectent directement. Aucun processus intermédiaire n'existe.

```
Topologie MQTT              Topologie ZeroMQ (la nôtre)
──────────────────          ──────────────────────────────
feed.py                     feed.py
  │  publish                PUB bind :5557
  ▼                           │
[broker Mosquitto]            ├──▶ account_bot A (SUB connect)
  │  subscribe               ├──▶ account_bot B (SUB connect)
  ├──▶ account_bot A         └──▶ indicators.py (SUB connect)
  ├──▶ account_bot B
  └──▶ indicators.py
```

### Avantages de ZeroMQ dans notre cas

| Critère | Détail |
|---|---|
| **Aucun processus broker** | Aucun daemon supplémentaire à déployer, configurer, surveiller ou redémarrer. 3 points de défaillance en moins par serveur dédié. |
| **Latence** | TCP loopback sans saut broker : ~10–50 µs contre ~1 ms via un broker MQTT local. Critique pour les messages `book` qui pilotent l'évaluation du signal au seuil 0,96. |
| **Pas de données périmées** | ZeroMQ PUB/SUB ne retient aucun message. Un SUB qui se connecte tardivement manque les anciens messages — exactement ce que l'on veut : un `account_bot` redémarré après un crash ne doit pas recevoir des centaines de prix en file d'attente. |
| **Simplicité** | `pip install pyzmq` suffit ; aucun paquet broker, aucun fichier de config, aucune règle ACL. Une ligne pour bind, une pour connect. |
| **High-water mark (HWM)** | Si un subscriber est lent, ZeroMQ abandonne silencieusement les messages à la HWM. Pour un flux de données de marché, abandonner est le bon comportement : un prix périmé est pire qu'aucun prix. |
| **Déploiement localhost** | Tous les processus tournent sur le même serveur dédié. `tcp://127.0.0.1:*` ne nécessite ni auth, ni TLS, ni ACL réseau. Le modèle de sécurité MQTT (users, TLS, ACL) n'apporte rien ici. |

### Inconvénients de ZeroMQ dans notre cas

| Critère | Détail |
|---|---|
| **Pas de persistance** | Un subscriber non connecté au moment de l'envoi ne recevra jamais le message. Ce n'est généralement pas un problème (`book` est continu ; `market` est re-publié toutes les 30 s) mais un `account_bot` fraîchement redémarré peut manquer le premier cycle d'annonces de marchés. |
| **Pas de filtrage par topic côté broker** | ZeroMQ PUB envoie chaque message à tous les SUB connectés. Le filtrage se fait côté application (`if msg["stream_id"] != "btc_4h": continue`). Avec MQTT, le filtrage par topic au niveau du broker économise du CPU sur les flux haute fréquence quand il y a beaucoup de subscribers. Ce n'est pas un problème aujourd'hui (N ≤ 3 subscribers). |
| **Pas de monitoring intégré** | Les brokers MQTT exposent un arbre de topics `$SYS` avec comptes de connexions, débit, profondeur de file. ZeroMQ n'a pas d'équivalent — le diagnostic nécessite une instrumentation applicative ou des outils externes. |
| **Perte de messages à la reconnexion** | Après un redémarrage du feed, les SUB se reconnectent automatiquement mais perdent les messages publiés pendant le gap. Le refresh `market` toutes les 30 s atténue ce problème ; les gaps sur `book` sont acceptables. |

### Pourquoi MQTT serait moins adapté ici

| Fonctionnalité MQTT | Notre situation |
|---|---|
| **Messages retenus** | On ne veut précisément *pas* qu'un `account_bot` fraîchement connecté reçoive le dernier prix en cache : il pourrait avoir plusieurs minutes d'ancienneté et corrompre la phase de warmup `min_ticks`. |
| **QoS 1 / QoS 2** | Ajoute des aller-retours d'acquittement et des garanties at-least-once / exactly-once. Pour les prix en streaming, une livraison dupliquée est néfaste (les ticks comptés deux fois faussent l'historique des indicateurs). |
| **HA broker / clustering** | Notre architecture tourne sur un serveur dédié unique. La HA broker MQTT ajoute de la complexité sans aucun bénéfice. |
| **Orienté WAN / IoT** | MQTT a été conçu pour des appareils contraints sur des réseaux peu fiables. Nos pipes sont du TCP loopback — il n'y a ni perte de paquets, ni limite de bande passante, ni problème de keep-alive à résoudre. |

### Verdict

ZeroMQ est le bon choix pour ce projet : il supprime le broker du chemin
critique, élimine une classe entière de défaillances de déploiement, et fournit
la sémantique sans-persistance, basse-latence que requiert un flux de carnets
d'ordres haute fréquence. Le seul scénario où MQTT deviendrait intéressant
serait une distribution des subscribers sur plusieurs hôtes (serveurs dédiés distincts) et
un filtrage par topic au niveau du broker pour gérer la bande passante — un
scénario hors périmètre actuellement.

---

## 11. Fichiers liés

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
