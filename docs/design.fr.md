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
| `indicators` | `bot/indicators.py` | Pipeline d'indicateurs partagé | Aucune | SUB connect `:5557`, PUB bind `:5559`, REP bind `:5561` |
| `orderbook_bot` | `bot/orderbook_bot.py` | Scalping OBI sur BTCUSDT spot + perp ; connexion directe au WebSocket Binance | Clé API Binance (optionnelle en mode paper) | Aucun (pas de ZMQ — WS Binance direct) |
| `accumulation_bot` | `bot/accumulation_bot.py` | Accumulation BTC spot long terme : achat initial + renforcement OBI + ladder de profits | Clé API Binance | Aucun (pas de ZMQ — WS Binance direct) |

`orderbook_bot` et `accumulation_bot` sont des bots Binance autonomes. Ils ne
participent pas à la topologie ZeroMQ feed/account-bot Polymarket et ne
consomment pas le service `indicators` — chacun calcule l'OBI en interne depuis
sa propre connexion WebSocket Binance `depth20@100ms`. Fichiers d'état :
`live_ob.db` / `orderbook_bot.pid` / `orderbook_bot.log` et `live_accum.db` /
`accumulation_bot.pid` / `accumulation_bot.log`. Configs de stratégie :
`strategies/scalping/orderbook_btc.json` et
`strategies/accumulation/btc_accumulation.json`.

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
| `rsi_N` | float | RSI(N) — formule de Cutler (sum/n sur fenêtre fixe), 0–100 |
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

### `indicators` — OBI et flux de trades Binance (`source="binance_scalping"`)

Piloté par le stream Binance combiné `depth20@100ms` + `aggTrade`. Publié
tous les `publish_every_n` events de profondeur (défaut 10). Calcule en temps
réel l'OBI et le trade flow imbalance.

```json
{
  "t":                "indicators",
  "stream_id":        "btc_scalping_spot",
  "asset":            "BTCUSDT",
  "market":           "spot",
  "obi":              0.12,
  "obi_ema":          0.10,
  "obi_decel":        -0.003,
  "spread_bps":       1.8,
  "tfi":              0.23,
  "realized_vol_bps": 4.7,
  "ts":               1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `obi` | float | OBI brut : `(bid_vol − ask_vol) / (bid_vol + ask_vol)` ∈ [−1, +1] |
| `obi_ema` | float | OBI lissé par EMA (filtre anti-spoofing) |
| `obi_decel` | float | Première différence de `obi_ema` (signal d'accélération) |
| `spread_bps` | float | Spread bid/ask en points de base |
| `tfi` | float | Trade flow imbalance sur `tfi_window_s` : `(buy_vol − sell_vol) / total_vol` ∈ [−1, +1] |
| `realized_vol_bps` | float | Écart-type des log-rendements du prix mid en points de base (absent si données insuffisantes) |
| `ts` | int | Horodatage de publication (Unix ms) |

---

### `indicators` — Carnet d'ordres complet Binance (`source="binance_full_depth"`)

Maintient le carnet d'ordres spot Binance complet (jusqu'à 5 000 niveaux) via
snapshot REST + diffs WebSocket incrémentiels avec resynchronisation complète
en cas de gap. Publié tous les `publish_every_n` events de profondeur (défaut 10).

```json
{
  "t":                  "indicators",
  "stream_id":          "btc_full_depth",
  "asset":              "BTCUSDT",
  "best_bid":           67420.10,
  "best_ask":           67421.50,
  "mid":                67420.80,
  "spread_bps":         2.08,
  "obi_10":             0.15,
  "obi_100":            0.08,
  "obi_500":            0.03,
  "cum_bid_vol_1.0pct": 12.45,
  "cum_ask_vol_1.0pct": 9.87,
  "wall_bid_price":     67300.00,
  "wall_bid_qty":       4.2,
  "wall_ask_price":     67500.00,
  "wall_ask_qty":       3.8,
  "book_levels_bid":    4872,
  "book_levels_ask":    4651,
  "ts":                 1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `best_bid`, `best_ask`, `mid` | float | Prix top-of-book |
| `spread_bps` | float | Spread en points de base |
| `obi_N` | float | OBI à N niveaux pour chaque N dans `obi_levels_list` (ex. `obi_10`, `obi_100`, `obi_500`) |
| `cum_bid_vol_Xpct` / `cum_ask_vol_Xpct` | float | Quantité cumulée dans X% du mid côté achat/vente |
| `wall_bid_price`, `wall_bid_qty` | float | Plus grand niveau acheteur unique dans `wall_range_pct` du mid |
| `wall_ask_price`, `wall_ask_qty` | float | Plus grand niveau vendeur unique dans `wall_range_pct` du mid |
| `book_levels_bid`, `book_levels_ask` | int | Nombre de niveaux de prix actuellement suivis |
| `ts` | int | Horodatage de publication (Unix ms) |

---

### `indicators` — Contexte de prix VWAP (`source="binance_vwap_context"`)

Interrogé toutes les heures (par défaut). Récupère les 24 dernières bougies
4h fermées depuis Binance REST, calcule le VWAP via le prix typique × volume,
puis récupère le prix spot courant pour dériver un score de dip.

```json
{
  "t":         "indicators",
  "stream_id": "btc_vwap_context",
  "vwap":      67150.40,
  "price":     67420.10,
  "dip_score": -0.00402,
  "dip_zone":  "above_vwap",
  "ts":        1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `vwap` | float | VWAP des `vwap_period` dernières bougies fermées |
| `price` | float | Prix spot courant |
| `dip_score` | float | `(vwap − price) / vwap`. Positif = sous le VWAP (dip) ; négatif = au-dessus |
| `dip_zone` | string | `"below_vwap"` ou `"above_vwap"` |
| `ts` | int | Horodatage de publication (Unix ms) |

---

### `indicators` — Profil de volume taker (`source="binance_volume_profile"`)

Interrogé toutes les heures (par défaut). Récupère les 288 dernières bougies
5m fermées, agrège le volume taker achat/vente dans des tranches de 500 $ et
identifie les 5 nœuds à volume élevé (HVN) principaux.

```json
{
  "t":               "indicators",
  "stream_id":       "btc_volume_profile",
  "price":           67420.10,
  "price_bucket":    67000.0,
  "bucket_buy_vol":  145.3,
  "bucket_sell_vol": 98.7,
  "bucket_net_vol":  46.6,
  "price_zone":      "buy_hvn",
  "zone_score":      0.32,
  "hvn_buckets":     [65000.0, 66500.0, 67000.0, 68000.0, 69500.0],
  "ts":              1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `price` | float | Prix spot courant |
| `price_bucket` | float | Borne inférieure de la tranche contenant `price` |
| `bucket_buy_vol` / `bucket_sell_vol` | float | Volume taker achat/vente dans la tranche courante |
| `bucket_net_vol` | float | `bucket_buy_vol − bucket_sell_vol` |
| `price_zone` | string | `"buy_hvn"` / `"sell_hvn"` / `"neutral"` |
| `zone_score` | float | `bucket_net_vol / total_bucket_vol` ∈ [−1, +1] |
| `hvn_buckets` | list[float] | Liste triée des `hvn_top_n` bornes inférieures des HVN |
| `ts` | int | Horodatage de publication (Unix ms) |

---

### `indicators` — OBI macro depuis les klines (`source="binance_macro_obi"`)

Interrogé toutes les minutes (par défaut). Récupère les 60 dernières bougies
1m fermées, calcule pour chaque bougie le déséquilibre de flux taker
`(taker_buy / total − 0,5) × 2`, et lisse la série par EMA.

```json
{
  "t":                   "indicators",
  "stream_id":           "btc_macro_obi",
  "macro_obi":           0.18,
  "macro_obi_raw":       0.24,
  "macro_obi_direction": "bullish",
  "ts":                  1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `macro_obi` | float | Déséquilibre de flux taker lissé par EMA ∈ [−1, +1] |
| `macro_obi_raw` | float | Valeur brute de la bougie 1m la plus récente |
| `macro_obi_direction` | string | `"bullish"` / `"neutral"` / `"bearish"` |
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

### `feed_auto_start = false` — feed géré par systemd

Quand `feed.py` est géré par un processus externe (ex. `tradinebotte-feed.service`),
définir `"feed_auto_start": false` dans le `config.json` du compte. Le chemin
verrou/Popen est entièrement ignoré. `account_bot` sonde l'adresse du feed en
boucle de tentatives (6 × 5 s = 30 s max) et quitte avec une erreur si le feed
est inaccessible. C'est le mode recommandé pour les déploiements cross-user où
le feed tourne en service systemd sous un autre utilisateur.

```
feed_auto_start = false :

account_bot démarre
    │
    ├─── sonde le feed (TCP connect, réception sous 5 s) ?
    │       OUI ──▶ connecte SUB, enregistre les indicateurs, commence le trading
    │
    │       NON (jusqu'à 6 tentatives)
    │        │
    │        ├─── toutes les tentatives épuisées ?
    │        │       OUI ──▶ log ERROR + sys.exit(1)
    │        │       NON ──▶ log WARNING + attente 5 s + nouvelle tentative
    │        │
    └─────────────────────────────────────────────────────────
```

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
En production, il tourne en tant que service systemd `tradinebotte-indicators`
avec `strategies/indicators/indicators_all.json`, qui regroupe les 14 flux en
un seul processus sur les ports 5559/5561.

Le pipeline comporte trois catégories de flux :

**1. WebSocket** (tâches asyncio événementielles) :
- `binance_ws` — WebSocket klines Binance ; publie à chaque bougie fermée. Alimente le buffer ring `PriceSeries` pour RSI/SMA/EMA/Vol et les indicateurs OHLCV (ATR, Bollinger, VWAP, vol_zscore, rolling_max).
- `binance_scalping` — Binance combiné `depth20@100ms` + `aggTrade` ; publie l'OBI, l'EMA-OBI, le TFI et la vol réalisée à 100 ms.
- `binance_full_depth` — Binance `depth@100ms` + snapshot REST ; maintient un carnet complet de 5 000 niveaux et publie l'OBI multi-profondeur, le volume cumulé et les niveaux murailles.

**2. REST-pollés** (boucles asyncio sleep) :
- `binance_funding` (15 min), `binance_oi` (5 min), `binance_ls_ratio` (5 min), `binance_liquidations` (5 min)
- `deribit_iv` (5 min), `fear_greed` (1 h)
- `binance_vwap_context` (1 h), `binance_volume_profile` (1 h), `binance_macro_obi` (1 min)

**3. Feed** (SUB à `feed.py`) :
- Source `feed` — ticks Polymarket `best_bid` par token. Alimente le buffer ring `PriceSeries` pour les indicateurs calculés. Doit être déclaré statiquement dans la config JSON (non disponible via l'enregistrement dynamique).

```
Ring-buffer par token (deque, maxlen=200)
    │
    │  push(best_bid) à chaque message "book"  [source feed]
    │  push(close, high, low, volume) à la bougie fermée  [source binance_ws]
    │
    ├── RSI(N)         Cutler : sum(gains)/n ÷ sum(losses)/n sur N deltas
    ├── SMA(N)         moyenne(prices[-N:])
    ├── EMA(N)         itératif : ema = prix*k + ema*(1−k),  k = 2/(N+1)
    ├── Vol(N)         écart-type des log-rendements sur N+1 derniers prix
    ├── ATR(N)         average true range  [binance_ws uniquement]
    ├── Bollinger(N)   SMA ± 2σ  [binance_ws uniquement]
    ├── VWAP(N)        moyenne pondérée par le volume  [binance_ws uniquement]
    ├── vol_zscore(N)  z-score du volume vs moyenne/écart-type glissants  [binance_ws uniquement]
    └── rolling_max(N) max(highs[-N:])  [binance_ws uniquement]
         │
         └── publie "indicators" quand tous les indicateurs configurés ont une valeur valide
```

Les indicateurs retournent `None` tant que le buffer n'a pas assez d'historique.
Aucun message n'est publié tant qu'un seul indicateur n'est pas calculable :
les consommateurs ne reçoivent jamais de données partielles.

**Partage multi-bots** — `indicators.py` exécute une tâche asyncio par flux. Toute la sortie est diffusée sur un seul socket PUB ; les consommateurs filtrent par `stream_id`.

**Enregistrement dynamique** — les flux peuvent être ajoutés à l'exécution sans redémarrer `indicators.py`. Chaque bot envoie un REQ au socket REP (`:5561`) avec ses besoins ; le serveur démarre la tâche si elle est nouvelle et répond avec le `stream_id` à écouter. Les flux déclarés dans la config JSON sont pré-chargés au démarrage ; les flux demandés par les bots s'ajoutent par-dessus.

**Sources supportées pour l'enregistrement dynamique :** `"binance_ws"`,
`"binance_scalping"`, `"binance_full_depth"`, `"binance_funding"`,
`"deribit_iv"`, `"fear_greed"`, `"binance_oi"`, `"binance_ls_ratio"`,
`"binance_liquidations"`, `"binance_vwap_context"`, `"binance_volume_profile"`
et `"binance_macro_obi"`. Les flux feed-source (ticks Polymarket) doivent être
déclarés statiquement dans le fichier de config JSON.

**Démarrage du pipeline :**

```bash
# Production : service unifié (14 flux)
TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators/indicators_all.json \
  bash scripts/start_indicators.sh

# Ou démarrer le feed puis les indicateurs manuellement
python3 bot/feed.py &
python3 bot/indicators.py &
# Un consommateur souscrit à :5559
# (tout script Python avec zmq.SUB connectant tcp://127.0.0.1:5559)
```

---

## 8. Ordre de démarrage

Pour l'Option B avec indicateurs :

```
1. feed.py           bind  :5557   (service systemd, ou démarrage auto si feed_auto_start=true)
2. indicators.py     SUB→  :5557   / bind :5559 + :5561  (service systemd, optionnel)
3. account_bot(s)    SUB→  :5557, REQ→ :5561 (enregistrement des flux d'indicateurs au démarrage)
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
| `TRADINEBOTTE_INDICATORS_ADDR` | `tcp://127.0.0.1:$(PORT_BASE+2)` | indicators.py, account_bot.py | Adresse ZMQ PUB du service indicators. `indicators.py` la bind ; `account_bot.py` s'y abonne quand `indicators_streams` est défini. |
| `TRADINEBOTTE_INDICATORS_REG_ADDR` | `tcp://127.0.0.1:$(PORT_BASE+4)` | indicators.py, account_bot.py | Adresse ZMQ REP pour l'enregistrement dynamique de flux. `indicators.py` la bind ; `account_bot.py` y envoie des REQ subscribe au démarrage. |
| `TRADINEBOTTE_DIR` | `~/tradinebotte` | account_bot.py, live_bot.py | Répertoire de données par compte (BD, log, config, stratégies) |

### Faire tourner deux piles indépendantes sur la même machine

```bash
# Pile A — ports par défaut (5557, 5559, 5561 …)
TRADINEBOTTE_DIR=~/compte-a python3 bot/account_bot.py &

# Pile B — tous les ports décalés de +1000
TRADINEBOTTE_PORT_BASE=6557 TRADINEBOTTE_DIR=~/compte-b python3 bot/account_bot.py &
TRADINEBOTTE_PORT_BASE=6557 TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators/indicators_4h_bitcoin.json \
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
| `bot/orderbook_bot.py` | Bot de scalping OBI Binance autonome (sans ZMQ) |
| `bot/accumulation_bot.py` | Bot d'accumulation BTC autonome (sans ZMQ) |
| `strategies/indicators/indicators_all.json` | Config indicators unifiée de production (14 flux) |
| `strategies/scalping/orderbook_btc.json` | Config de stratégie pour `orderbook_bot` |
| `strategies/accumulation/btc_accumulation.json` | Config de stratégie pour `accumulation_bot` |
| `docs/multi.fr.md` | Guide de configuration Option B et stratégies par compte |
| `docs/GridTrading.fr.md` | Architecture de la stratégie grid et config JSON |
| `tests/test_multibot.py` | Tests d'intégration ZMQ pour feed + account_bot |
| `tests/test_indicators.py` | Tests unitaires pour les maths d'indicateurs et PriceSeries |
