# Indicateurs — Guide de référence

> 🇬🇧 [English version](indicators.md)

`tradinebotte-indicators/indicators.py` est un étage de pipeline optionnel qui souscrit aux données
de marché (feed ZeroMQ et/ou WebSocket Binance) et publie des messages enrichis
sur un socket PUB dédié. Il supporte trois catégories de données :

- **Indicateurs calculés** — appliqués à une série de prix glissante (RSI, SMA,
  EMA, volatilité). Requièrent au moins `min_ticks` points de données avant de
  publier.
- **Sources WebSocket** — maintiennent une connexion persistante aux streams
  Binance et publient par lot à chaque événement. Aucun polling REST requis.
- **Sources par polling** — récupèrent une valeur brute depuis une API REST
  externe à intervalle configurable. Aucun historique de prix requis.

Toute la sortie est diffusée sur un seul socket PUB ; les consommateurs filtrent
par `stream_id`. Voir [design.fr.md](design.fr.md) pour la topologie ZeroMQ et
le protocole d'enregistrement dynamique.

---

## 1. Format de configuration

Chaque flux est déclaré dans un fichier JSON :

```json
{
  "zmq_feed_addr": "tcp://127.0.0.1:5557",
  "zmq_out_addr":  "tcp://127.0.0.1:5559",
  "zmq_reg_addr":  "tcp://127.0.0.1:5561",
  "min_ticks": 25,
  "streams": [
    {
      "id":              "btc_4h",
      "asset":           "BTCUSDT",
      "source":          "binance_ws",
      "timeframe":       "4h",
      "indicators":      [
        {"type": "rsi",        "period": 14},
        {"type": "volatility", "period": 20}
      ],
      "seed_periods":    50,
      "poll_interval_s": 0
    }
  ]
}
```

### Champs de premier niveau

| Champ | Défaut | Description |
|---|---|---|
| `zmq_feed_addr` | `tcp://127.0.0.1:5557` | Adresse du socket PUB de `feed.py` (source feed uniquement) |
| `zmq_out_addr` | `tcp://127.0.0.1:5559` | Adresse PUB bindée par cette instance |
| `zmq_reg_addr` | `tcp://127.0.0.1:5561` | Adresse REP pour l'enregistrement dynamique de flux |
| `min_ticks` | 25 | Ticks minimum dans le buffer avant publication (indicateurs calculés uniquement) |

### Champs de flux

| Champ | Requis | Défaut | Description |
|---|---|---|---|
| `id` | oui | — | Identifiant unique du flux ; utilisé comme `stream_id` dans les messages publiés |
| `asset` | oui¹ | `""` | Paire de trading (ex. `"BTCUSDT"`) |
| `source` | oui | `"feed"` | Source de données — voir section 2 |
| `timeframe` | oui¹ | `"tick"` | Timeframe de la bougie pour `binance_ws` ; `"n/a"` pour les sources poll |
| `indicators` | oui² | `[]` | Liste d'objets `{"type": "...", "period": N}` |
| `seed_periods` | non | `50` | Bougies REST historiques à pré-charger au démarrage (`binance_ws` uniquement) |
| `poll_interval_s` | non | `0` | Intervalle de poll en secondes ; `0` = utiliser le défaut de la source |

¹ Optionnel pour les sources poll (`binance_funding`, `deribit_iv`, `fear_greed`,
`binance_oi`, `binance_ls_ratio`, `binance_liquidations`).  
² `[]` vide autorisé pour les sources poll uniquement ; obligatoire pour `feed`
et `binance_ws`.

---

## 2. Sources de données

| `source` | Catégorie | Dépendance externe | Intervalle par défaut |
|---|---|---|---|
| `feed` | calculé | ZeroMQ feed.py (local) | événementiel |
| `binance_ws` | calculé | Binance kline WebSocket | événementiel (bougie fermée) |
| `binance_scalping` | WebSocket | Binance depth20@100ms + aggTrade | événementiel (tous les N events) |
| `cex_scalping` | ZeroMQ | `cex_feed` partagé (local, tout exchange) | événementiel (tous les N updates de carnet) |
| `binance_full_depth` | WebSocket | Binance depth@100ms + snapshot REST | événementiel (tous les N events) |
| `binance_funding` | poll | REST `fapi.binance.com` | 900 s (15 min) |
| `deribit_iv` | poll | REST `www.deribit.com` | 300 s (5 min) |
| `fear_greed` | poll | REST `api.alternative.me` | 3600 s (1 h) |
| `binance_oi` | poll | REST `fapi.binance.com` | 300 s (5 min) |
| `binance_ls_ratio` | poll | REST `fapi.binance.com` | 300 s (5 min) |
| `binance_liquidations` | poll | REST `fapi.binance.com` | 300 s (5 min) |
| `binance_vwap_context` | poll | REST `api.binance.com` | 3600 s (1 h) |
| `binance_volume_profile` | poll | REST `api.binance.com` | 3600 s (1 h) |
| `binance_macro_obi` | poll | REST `api.binance.com` | 60 s (1 min) |

**`cex_scalping`** consomme le service de plan de données partagé `cex_feed` (qui récupère chaque carnet d'ordres CEX une seule fois et le diffuse via ZeroMQ) au lieu d'ouvrir son propre WebSocket d'exchange, et rediffuse un flux de scalping (`mid` / `obi` / `obi_ema` / `spread_bps`) au même format que `binance_scalping`. Paramètres : `exchange` + `symbol` (les tags cex_feed, p. ex. `mexc` / `BTCUSDT`), `cex_feed_addr`, `obi_ema_alpha`, `publish_every_n`. Utilisé p. ex. pour `btc_scalping_mexc` (MEXC spot, décodé depuis le WS protobuf de MEXC par cex_feed).

**Enregistrement à la demande :** au-delà de la config statique, un bot peut déclarer les flux dont il a besoin (`indicators_streams`) et les enregistrer auprès de la socket REP (`zmq_reg_addr`), en se ré-enregistrant périodiquement : un flux s'auto-répare si le service indicators redémarre — sans édition de config statique.

---

## 3. Indicateurs calculés

Ces indicateurs sont appliqués à un buffer ring `PriceSeries`. Ils se
configurent via la liste `indicators` dans une spec de flux.

### 3.1 Indicateurs basés sur les prix (`feed` et `binance_ws`)

Ces indicateurs fonctionnent avec toute source fournissant une série de prix.

#### RSI — Relative Strength Index

**Clé :** `rsi_N` (ex. `rsi_14`)  
**Config :** `{"type": "rsi", "period": 14}`  
**Prix minimum requis :** N + 1  
**Plage de sortie :** 0 – 100 (float)

RSI de Wilder sur les N derniers deltas de prix :

```
avg_gain = moyenne(deltas positifs sur les N derniers)
avg_loss = moyenne(|deltas négatifs| sur les N derniers)
RSI = 100 − 100 / (1 + avg_gain / avg_loss)
```

Retourne `None` quand moins de N + 1 prix sont dans le buffer. Retourne 100.0
quand `avg_loss == 0` (uniquement des gains).

**Interprétation :** >70 = suracheté, <30 = survendu. Pour les marchés binaires
à 5 minutes, un RSI élevé associé à `best_bid ≥ 0,96` peut indiquer une
surextension — la résolution UP est déjà pricée. Un RSI bas avec un bid élevé
est plus rare et peut constituer un signal d'entrée plus fort.

---

#### SMA — Moyenne Mobile Simple

**Clé :** `sma_N` (ex. `sma_20`)  
**Config :** `{"type": "sma", "period": 20}`  
**Prix minimum requis :** N  
**Plage de sortie :** même échelle que les prix d'entrée (0 – 1 pour les bids Polymarket)

```
SMA(N) = moyenne(prices[-N:])
```

Retourne `None` quand moins de N prix sont dans le buffer.

**Interprétation :** Prix au-dessus de la SMA = tendance haussière ; prix
croisant la SMA par le bas = support potentiel. Pour les flux feed (Polymarket
`best_bid`), la SMA lisse le bruit tick-by-tick. Utile comme filtre de
confirmation de tendance en complément du seuil 0,96.

---

#### EMA — Moyenne Mobile Exponentielle

**Clé :** `ema_N` (ex. `ema_9`)  
**Config :** `{"type": "ema", "period": 9}`  
**Prix minimum requis :** N  
**Plage de sortie :** même échelle que les prix d'entrée

```
k = 2 / (N + 1)
EMA₀ = SMA(prices[:N])          # amorçage avec SMA
EMAᵢ = prixᵢ × k + EMAᵢ₋₁ × (1 − k)
```

Retourne `None` quand moins de N prix sont dans le buffer.

**Interprétation :** Plus réactive que la SMA (davantage de poids aux prix
récents). Paires typiques : EMA 9 + EMA 20 pour les signaux de croisement, ou
EMA 12 + EMA 26 pour le MACD (voir TODO). Un croisement de l'EMA courte au-
dessus de l'EMA longue = changement de momentum.

---

#### Volatilité — Volatilité glissante

**Clé :** `vol_N` (ex. `vol_20`)  
**Config :** `{"type": "volatility", "period": 20}`  
**Prix minimum requis :** N + 1  
**Plage de sortie :** 0 – ∞ (écart-type des log-rendements, sans dimension)

Écart-type population des log-rendements sur les N + 1 derniers prix :

```
log_returns = [log(p[i] / p[i-1]) pour i dans 1..N]
vol = sqrt( moyenne( (r - moyenne(log_returns))² ) )
```

Retourne `None` quand moins de N + 1 prix sont dans le buffer, ou quand un
prix ≤ 0. Retourne 0.0 pour une série constante.

**Interprétation :** Faible volatilité = marché en tendance stable ; haute
volatilité = incertitude ou retournements rapides. Un pic de volatilité avant
la fermeture d'un marché binaire indique que le prix actuel du bid est
instable.

---

### 3.2 Indicateurs OHLCV (`binance_ws` uniquement)

Ces indicateurs nécessitent les données High, Low et Volume des klines Binance.
Ils ne sont **pas disponibles** pour les flux source `feed` (qui ne fournissent
que `best_bid`).

#### ATR — Average True Range

**Clé :** `atr_N` (ex. `atr_14`)  
**Config :** `{"type": "atr", "period": 14}`  
**Source requise :** `binance_ws`

True range moyen sur les N dernières barres. True range = max(H−L, |H−C_prev|,
|L−C_prev|). Mesure la volatilité brute en unités de prix.

---

#### Bollinger Bands

**Clés :** `bb_upper_N`, `bb_mid_N`, `bb_lower_N` (ex. `bb_upper_20`)  
**Config (trois entrées séparées) :**
```json
{"type": "bollinger_upper", "period": 20},
{"type": "bollinger_mid",   "period": 20},
{"type": "bollinger_lower", "period": 20}
```
**Source requise :** `binance_ws`  
**Multiplicateur de bande :** fixé à k = 2.0

`bb_mid_N` = SMA(close, N) ; `bb_upper_N` = mid + 2σ ; `bb_lower_N` = mid − 2σ.
Largeur = (upper − lower) / mid — mesure le régime de volatilité.

---

#### VWAP — Prix Moyen Pondéré par le Volume

**Clé :** `vwap_N` (ex. `vwap_50`)  
**Config :** `{"type": "vwap", "period": 50}`  
**Source requise :** `binance_ws`

VWAP des N dernières bougies fermées en utilisant le prix de clôture × le
volume de base. Glissant (non réinitialisé en intraday). Utile comme référence
d'ancrage de tendance.

---

#### vol_zscore — Z-score du volume

**Clé :** `vol_z_N` (ex. `vol_z_20`)  
**Config :** `{"type": "vol_zscore", "period": 20}`  
**Source requise :** `binance_ws`

Z-score du volume de la barre courante par rapport à la moyenne et à l'écart-
type glissants sur N barres : `(vol_courant − moyenne) / écart-type`. Positif =
volume élevé ; négatif = volume sous la moyenne.

---

#### rolling_max — Maximum glissant des hauts

**Clé :** `rmax_N` (ex. `rmax_20`)  
**Config :** `{"type": "rolling_max", "period": 20}`  
**Source requise :** `binance_ws`

Prix le plus haut parmi les N dernières barres. Utile pour la détection de
cassures (breakout).

---

#### ichimoku — Ichimoku Kinko Hyo

**Clés :** `ichi_tenkan`, `ichi_kijun`, `ichi_cloud_top`, `ichi_cloud_bottom`, `ichi_chikou`  
**Config :** `{"type": "ichimoku"}` — pas de `period` ; périodes conventionnelles fixes 9 / 26 / 52 / 26.  
**Source requise :** `binance_ws`

Un groupe multi-lignes émettant cinq valeurs (une entrée de config → cinq clés) :

| Clé | Signification |
| --- | --- |
| `ichi_tenkan` | Ligne de conversion, **courante** : `(max(high,9) + min(low,9)) / 2` |
| `ichi_kijun` | Ligne de base, **courante** : `(max(high,26) + min(low,26)) / 2` |
| `ichi_cloud_top` / `ichi_cloud_bottom` | Les Senkou Span A/B qui **s'appliquent à la barre courante** — les spans leading sont calculés il y a 26 barres (displacement), donc c'est le nuage contre lequel le prix live évolue réellement. `top = max(A,B)`, `bottom = min(A,B)`. **Comparer le prix live à ces valeurs, jamais à un span forming courant.** |
| `ichi_chikou` | `close[t − 26]` — le prix auquel la lagging span est comparée ; un consommateur détenant le close courant dérive le signal chikou via `sign(close_now − ichi_chikou)`. |

**Historique requis :** le nuage applicable nécessite 52 (Senkou B) + 26 (displacement)
= **78 barres**. Comme le publisher supprime le message entier d'un stream tant que
*l'un* de ses indicateurs vaut `None`, mettre `seed_periods >= 78` et de préférence
faire tourner l'ichimoku dans **son propre stream** pour qu'un nuage en préchauffage
ne bloque pas les indicateurs plus rapides. Voir `strategies/indicators_ichimoku_btc.json`.

---

## 4. Sources WebSocket

Ces sources maintiennent une connexion Binance WebSocket persistante et
publient par lot sans polling REST. `indicators: []` est obligatoire.

### `binance_scalping` — OBI et flux de trades en temps réel

**Streams :** Binance combinés `depth20@100ms` + `aggTrade`  
**Déclencheur de publication :** tous les `publish_every_n` mises à jour de profondeur (défaut 10)  
**Authentification :** non requise

```json
{
  "t":                 "indicators",
  "stream_id":         "btc_scalping_spot",
  "asset":             "BTCUSDT",
  "market":            "spot",
  "obi":               0.12,
  "obi_ema":           0.10,
  "obi_decel":         -0.003,
  "spread_bps":        1.8,
  "tfi":               0.23,
  "realized_vol_bps":  4.7,
  "ts":                1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `obi` | float | Déséquilibre brut du carnet à `obi_levels` niveaux : `(bid_vol − ask_vol) / (bid_vol + ask_vol)` ∈ [−1, +1] |
| `obi_ema` | float | OBI lissé par EMA (`obi_ema_alpha`, défaut 0,05) — filtre anti-spoofing |
| `obi_decel` | float | Première différence de `obi_ema` — signal d'accélération/décélération de l'OBI |
| `spread_bps` | float | `(best_ask − best_bid) / mid × 10000` en points de base |
| `tfi` | float | Trade flow imbalance sur `tfi_window_s` : `(buy_vol − sell_vol) / total_vol` ∈ [−1, +1] |
| `realized_vol_bps` | float | Écart-type population des log-rendements du prix mid en points de base (absent si données insuffisantes) |

**Paramètres du flux :**

| Paramètre | Défaut | Description |
|---|---|---|
| `market` | `"spot"` | `"spot"` ou `"perp"` — sélectionne le endpoint WebSocket Binance |
| `obi_levels` | 10 | Nombre de niveaux top-of-book sommés pour l'OBI |
| `obi_ema_alpha` | 0.05 | Coefficient de lissage EMA pour `obi_ema` |
| `tfi_window_s` | 60.0 | Fenêtre glissante en secondes pour l'agrégation TFI |
| `vol_window_n` | 200 | Nombre d'échantillons de prix mid pour la vol réalisée |
| `publish_every_n` | 10 | Limitation : publier une fois tous les N events de profondeur |

---

### `binance_full_depth` — Reconstruction complète du carnet d'ordres

**Streams :** Binance `btcusdt@depth@100ms` (diffs incrémentiels) + snapshot REST  
**Déclencheur de publication :** tous les `publish_every_n` mises à jour de profondeur (défaut 10)  
**Authentification :** non requise

Maintient le carnet d'ordres spot complet (jusqu'à 5 000 niveaux) en suivant
l'algorithme de reconnexion+resynchronisation Binance documenté : à la
connexion, bufferiser les events WebSocket pendant la récupération d'un
snapshot REST (`GET /api/v3/depth?limit=5000`) ; appliquer le snapshot ;
rejouer les events bufférisés (éliminer les périmés, valider la séquence
`U == lastUpdateId + 1`) ; puis diffuser en direct. En cas de gap ou d'erreur :
resynchronisation complète.

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
| `best_bid` | float | Meilleur prix acheteur |
| `best_ask` | float | Meilleur prix vendeur |
| `mid` | float | `(best_bid + best_ask) / 2` |
| `spread_bps` | float | Spread en points de base |
| `obi_N` | float | OBI à N niveaux pour chaque N dans `obi_levels_list` (ex. `obi_10`, `obi_100`, `obi_500`) |
| `cum_bid_vol_Xpct` | float | Quantité acheteur cumulée dans X% sous le mid |
| `cum_ask_vol_Xpct` | float | Quantité vendeur cumulée dans X% au-dessus du mid |
| `wall_bid_price` | float | Prix du plus grand niveau acheteur unique dans `wall_range_pct` du mid |
| `wall_bid_qty` | float | Quantité au `wall_bid_price` |
| `wall_ask_price` | float | Prix du plus grand niveau vendeur unique dans `wall_range_pct` du mid |
| `wall_ask_qty` | float | Quantité au `wall_ask_price` |
| `book_levels_bid` | int | Nombre de niveaux de prix actuellement suivis côté achat |
| `book_levels_ask` | int | Nombre de niveaux de prix actuellement suivis côté vente |

**Paramètres du flux :**

| Paramètre | Défaut | Description |
|---|---|---|
| `obi_levels_list` | `[10, 100, 500]` | Liste des profondeurs pour lesquelles l'OBI est calculé |
| `cum_vol_range_pct` | 1.0 | Plage en pourcentage autour du mid pour les champs de volume cumulé |
| `wall_range_pct` | 2.0 | Plage en pourcentage autour du mid pour la recherche de niveaux murailles |
| `publish_every_n` | 10 | Limitation : publier une fois tous les N events de profondeur |

---

## 5. Sources par polling

Ces sources récupèrent une valeur depuis une API REST externe à intervalle
configurable. Elles ne nécessitent pas de série de prix. `indicators: []` est
obligatoire dans la config JSON.

### `binance_funding` — Taux de financement perpétuel

**Endpoint :** `https://fapi.binance.com/fapi/v1/premiumIndex`  
**Intervalle par défaut :** 900 s (15 min)  
**Authentification :** non requise

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
| `funding_rate` | float | Taux de financement perp Binance actuel. Positif = les longs paient les shorts. Plage typique : ±0,03 % par 8 h. |
| `next_funding_ms` | int | Prochain règlement de financement (Unix ms) |

**Interprétation :** Financement fortement positif (> 0,01 %) = côté long
encombré, légère inclinaison baissière. Financement négatif = côté short
encombré, légère inclinaison haussière. Contexte macro, pas un signal
trade-par-trade.

---

### `deribit_iv` — Volatilité implicite (DVOL)

**Endpoint :** `https://www.deribit.com/api/v2/public/get_index_price?index_name=dvol_btc`  
**Intervalle par défaut :** 300 s (5 min)  
**Authentification :** non requise

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
| `dvol` | float | Volatilité implicite annualisée BTC Deribit (ex. 62,5 ≈ 62,5 %) |

**Interprétation :** DVOL élevé (> 80 %) = le marché des options anticipe de
grands mouvements ; DVOL faible (< 40 %) = marché calme. Un pic de DVOL alors
que `best_bid` est proche de 0,96 suggère que la "certitude" est fragile.
Utile pour le sizing (réduire la taille en régime de IV haute).

---

### `fear_greed` — Indice Fear & Greed

**Endpoint :** `https://api.alternative.me/fng/?limit=1`  
**Intervalle par défaut :** 3600 s (1 h)  
**Authentification :** non requise

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
| `fear_greed` | int | Valeur de l'indice 0–100 |
| `fear_greed_label` | string | `"Extreme Fear"` / `"Fear"` / `"Neutral"` / `"Greed"` / `"Extreme Greed"` |

**Interprétation :** Avidité Extrême (> 80) précède historiquement des
corrections ; Peur Extrême (< 20) précède des rebonds. Contexte macro
uniquement — beaucoup trop lent pour les marchés prédictifs à 5 minutes.

---

### `binance_oi` — Open interest futures

**Endpoint :** `https://fapi.binance.com/futures/data/openInterestHist`  
**Paramètres :** `period=5m&limit=2`  
**Intervalle par défaut :** 300 s (5 min)  
**Authentification :** non requise

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
| `oi_btc` | float | Open interest total en contrats BTC |
| `oi_usd` | float | Open interest total en USD |
| `oi_change_btc` | float | Variation depuis le poll précédent (+ = positions ouvertes, − = positions fermées) |
| `oi_change_usd` | float | Même variation en USD |

`oi_change_*` vaut 0.0 au premier poll (pas de référence précédente).

**Interprétation :** OI montant + prix montant = vraie tendance (nouveaux longs
qui entrent). OI montant + prix baissant = nouveaux shorts (bearish). OI
baissant = débouclage de positions indépendamment de la direction (risque de
retournement). Une chute importante de l'OI juste avant la fermeture d'un
marché binaire peut indiquer un exit des mains fortes.

---

### `binance_ls_ratio` — Ratio long/short des comptes

**Endpoint :** `https://fapi.binance.com/futures/data/topLongShortAccountRatio`  
**Paramètres :** `period=5m&limit=1`  
**Intervalle par défaut :** 300 s (5 min)  
**Authentification :** non requise

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

**Note :** Mesure le *nombre de comptes*, pas la *taille des positions*. Une
grosse position short d'un seul compte n'est pas reflétée ici.

**Interprétation :** Signal contrarian. Quand les top-traders sont massivement
longs (ratio > 1,5), le prix se retourne souvent à la baisse (trade encombré).
Ratio < 0,7 (majoritairement short) précède des short squeezes. Plus prédictif
en régime de tendance avec OI élevé.

---

### `binance_liquidations` — Ordres de liquidation forcée

**Endpoint :** `https://fapi.binance.com/fapi/v1/forceOrders`  
**Paramètres :** `startTime = maintenant − intervalle`  
**Intervalle par défaut :** 300 s (5 min)  
**Authentification :** non requise

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
| `liq_long_usd` | float | Valeur USD des positions longues liquidées sur l'intervalle (ordres `SELL` forcés) |
| `liq_short_usd` | float | Valeur USD des positions courtes liquidées sur l'intervalle (ordres `BUY` forcés) |
| `liq_net_usd` | float | `liq_short_usd − liq_long_usd`. Négatif = cascade de liquidations longues (baissier). |
| `liq_count` | int | Nombre total d'ordres forcés sur l'intervalle |

**Interprétation :** Un pic élevé de `liq_long_usd` signifie que des longs sont
vendus de force — cela fait baisser le prix et peut déclencher des cascades.
`liq_short_usd` élevé = short squeeze en cours. Ces deux événements augmentent
la volatilité réalisée dans la prochaine fenêtre de 5 minutes, ce qui est
directement pertinent pour prédire la certitude d'un résultat binaire.

---

### `binance_vwap_context` — Contexte de prix VWAP

**Endpoints :** `https://api.binance.com/api/v3/klines` + `https://api.binance.com/api/v3/ticker/price`  
**Intervalle par défaut :** 3600 s (1 h)  
**Authentification :** non requise

Récupère les `vwap_period` dernières bougies fermées au `timeframe` configuré
(défaut : 24 bougies 4h = 4 jours de données), calcule le VWAP en utilisant
le prix typique `(H + L + C) / 3 × volume de base`, puis récupère le prix
spot courant.

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
| `dip_score` | float | `(vwap − price) / vwap`. Positif = prix sous le VWAP (dip) ; négatif = prix au-dessus |
| `dip_zone` | string | `"below_vwap"` ou `"above_vwap"` |

**Paramètres du flux :**

| Paramètre | Défaut | Description |
|---|---|---|
| `vwap_period` | 24 | Nombre de bougies fermées pour le VWAP (24 × 4h = 4 jours) |
| `timeframe` | `"4h"` | Timeframe des klines |

---

### `binance_volume_profile` — Profil de volume taker

**Endpoints :** `https://api.binance.com/api/v3/klines` + `https://api.binance.com/api/v3/ticker/price`  
**Intervalle par défaut :** 3600 s (1 h)  
**Authentification :** non requise

Récupère les `kline_limit` dernières bougies fermées, agrège le volume taker
achat/vente dans des tranches de prix larges de `bucket_size_usd` (en utilisant
le point médian de la bougie), et identifie les `hvn_top_n` nœuds à volume
élevé (HVN) par volume total.

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
| `bucket_buy_vol` | float | Volume taker achat accumulé dans la tranche courante |
| `bucket_sell_vol` | float | Volume taker vente accumulé dans la tranche courante |
| `bucket_net_vol` | float | `bucket_buy_vol − bucket_sell_vol` |
| `price_zone` | string | `"buy_hvn"` / `"sell_hvn"` / `"neutral"` — si la tranche courante est un HVN et son côté dominant |
| `zone_score` | float | `bucket_net_vol / (bucket_buy_vol + bucket_sell_vol)` ∈ [−1, +1] |
| `hvn_buckets` | list[float] | Liste triée des `hvn_top_n` bornes inférieures des HVN |

**Paramètres du flux :**

| Paramètre | Défaut | Description |
|---|---|---|
| `bucket_size_usd` | 500.0 | Largeur de chaque tranche de prix en USD |
| `hvn_top_n` | 5 | Nombre de HVN à identifier |
| `kline_limit` | 288 | Nombre de bougies fermées à récupérer (288 × 5m = 24 h) |
| `timeframe` | `"5m"` | Timeframe des klines |

---

### `binance_macro_obi` — OBI macro depuis les klines

**Endpoint :** `https://api.binance.com/api/v3/klines`  
**Intervalle par défaut :** 60 s (1 min)  
**Authentification :** non requise

Récupère les `kline_limit` dernières bougies 1m fermées et calcule pour chaque
bougie un déséquilibre de flux taker : `(taker_buy_vol / total_vol − 0,5) × 2`,
plage [−1, +1]. La série est lissée par EMA avec `ema_alpha` pour produire
`macro_obi`.

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
| `macro_obi` | float | Déséquilibre de flux taker lissé par EMA ∈ [−1, +1]. +1 = pression acheteuse pure ; −1 = pression vendeuse pure. |
| `macro_obi_raw` | float | Déséquilibre brut de la bougie 1m la plus récente |
| `macro_obi_direction` | string | `"bullish"` / `"neutral"` / `"bearish"` selon le `neutral_threshold` |

**Paramètres du flux :**

| Paramètre | Défaut | Description |
|---|---|---|
| `kline_limit` | 60 | Nombre de bougies 1m fermées à récupérer |
| `ema_alpha` | 0.20 | Coefficient de lissage EMA |
| `neutral_threshold` | 0.10 | `|macro_obi|` en dessous de ce seuil → `"neutral"` |
| `timeframe` | `"1m"` | Timeframe des klines |

---

## 6. Fichiers de configuration prêts à l'emploi

### Config unifiée de production

`tradinebotte-indicators/strategies/indicators_all.json` est la configuration de production.
Elle fait tourner les 14 flux dans un seul processus `indicators.py` sur les
ports 5559/5561 (service systemd `tradinebotte-indicators`). Les fichiers
individuels existent uniquement pour les tests en standalone.

| `stream_id` | Source | Catégorie |
|---|---|---|
| `btc_4h` | `binance_ws` | WebSocket (bougie fermée) |
| `btc_1d` | `binance_ws` | WebSocket (bougie fermée) |
| `btc_funding` | `binance_funding` | Poll REST (15 min) |
| `btc_dvol` | `deribit_iv` | Poll REST (5 min) |
| `fear_greed` | `fear_greed` | Poll REST (1 h) |
| `btc_oi` | `binance_oi` | Poll REST (5 min) |
| `btc_ls_ratio` | `binance_ls_ratio` | Poll REST (5 min) |
| `btc_scalping_spot` | `binance_scalping` | WebSocket (depth20 + aggTrade, spot) |
| `btc_scalping_perp` | `binance_scalping` | WebSocket (depth20 + aggTrade, perp) |
| `btc_full_depth` | `binance_full_depth` | WebSocket (reconstruction carnet complet) |
| `btc_vwap_context` | `binance_vwap_context` | Poll REST (1 h) |
| `btc_volume_profile` | `binance_volume_profile` | Poll REST (1 h) |
| `btc_macro_obi` | `binance_macro_obi` | Poll REST (1 min) |

### Configs standalone par flux (tests)

Chaque fichier déclare un flux sur une paire de ports PUB + REP dédiés.

| Fichier config | Source | `stream_id` | Port PUB | Port REP | Poll par défaut |
|---|---|---|---|---|---|
| `indicators_4h_bitcoin.json` | `binance_ws` | `btc_4h` | 5559 | 5561 | événementiel |
| `indicators_1d_bitcoin.json` | `binance_ws` | `btc_1d` | 5560 | 5562 | événementiel |
| `indicators_funding_bitcoin.json` | `binance_funding` | `btc_funding` | 5563 | 5564 | 900 s |
| `indicators_deribit_iv_bitcoin.json` | `deribit_iv` | `btc_dvol` | 5565 | 5566 | 300 s |
| `indicators_fear_greed.json` | `fear_greed` | `fear_greed` | 5567 | 5568 | 3600 s |
| `indicators_oi_bitcoin.json` | `binance_oi` | `btc_oi` | 5569 | 5570 | 300 s |
| `indicators_ls_ratio_bitcoin.json` | `binance_ls_ratio` | `btc_ls_ratio` | 5571 | 5572 | 300 s |
| `indicators_liquidations_bitcoin.json` | `binance_liquidations` | `btc_liquidations` | 5573 | 5574 | 300 s |

### Démarrer l'instance de production unifiée

```bash
# Production : 14 flux (géré par le service systemd tradinebotte-indicators)
TRADINEBOTTE_INDICATORS_CONFIG=tradinebotte-indicators/strategies/indicators_all.json \
  bash tradinebotte-indicators/scripts/start_indicators.sh

# Test d'un flux unique en isolation
TRADINEBOTTE_INDICATORS_CONFIG=tradinebotte-indicators/strategies/indicators_oi_bitcoin.json \
  bash tradinebotte-indicators/scripts/start_indicators.sh
```

---

## 7. Enregistrement dynamique

Tout bot peut demander un nouveau flux à l'exécution en envoyant un REQ au
socket REP (`:5561` par défaut). Le serveur démarre la tâche si elle ne tourne
pas déjà et répond immédiatement.

```python
import zmq, json
ctx = zmq.Context()
req = ctx.socket(zmq.REQ)
req.connect("tcp://127.0.0.1:5561")

req.send_json({
    "cmd":        "subscribe",
    "source":     "binance_ws",
    "asset":      "BTCUSDT",
    "timeframe":  "4h",
    "indicators": [{"type": "rsi", "period": 14}],
})
resp = req.recv_json()
# {"status": "ok", "stream_id": "btc_4h"}
```

Pour les sources poll et WebSocket, `asset` et `timeframe` sont optionnels :

```python
req.send_json({"cmd": "subscribe", "source": "binance_oi", "asset": "BTCUSDT"})
resp = req.recv_json()
# {"status": "ok", "stream_id": "binance_oi"}
```

**Limitation :** Les flux `source="feed"` ne peuvent pas être enregistrés
dynamiquement — les déclarer dans le fichier de config JSON. Toutes les autres
sources supportent l'enregistrement dynamique : `"binance_ws"`,
`"binance_scalping"`, `"binance_full_depth"`, `"binance_funding"`,
`"deribit_iv"`, `"fear_greed"`, `"binance_oi"`, `"binance_ls_ratio"`,
`"binance_liquidations"`, `"binance_vwap_context"`, `"binance_volume_profile"`
et `"binance_macro_obi"`.

---

## 8. Configuration des ports

Toutes les adresses de port sont calculées depuis `TRADINEBOTTE_PORT_BASE`
(défaut : 5557). Cette variable décale l'ensemble de la plage de ports par
défaut de façon uniforme, permettant de faire tourner deux piles indépendantes
sur la même machine sans modifier aucun fichier JSON.

| Variable | Défaut | Description |
|---|---|---|
| `TRADINEBOTTE_PORT_BASE` | `5557` | Port de base. Tous les ports par défaut se décalent de `PORT_BASE − 5557`. |
| `TRADINEBOTTE_FEED_ADDR` | `tcp://127.0.0.1:{PORT_BASE}` | Adresse PUB du feed. Remplace `PORT_BASE` pour le feed uniquement. |
| `TRADINEBOTTE_INDICATORS_ADDR` | `tcp://127.0.0.1:{PORT_BASE+2}` | Adresse PUB du service indicators. |
| `TRADINEBOTTE_INDICATORS_REG_ADDR` | `tcp://127.0.0.1:{PORT_BASE+4}` | Adresse REP pour l'enregistrement dynamique. |

Quand `PORT_BASE` est défini, les adresses déclarées dans les fichiers JSON
(`zmq_out_addr`, `zmq_reg_addr`, `zmq_feed_addr`) sont décalées du même offset.
Les variables par service restent prioritaires sans décalage.

```bash
# Pile par défaut — ports 5557 / 5559 / 5561 …
bash tradinebotte-indicators/scripts/start_indicators.sh

# Deuxième pile indépendante — tous les ports décalés de +1000
TRADINEBOTTE_PORT_BASE=6557 \
TRADINEBOTTE_INDICATORS_CONFIG=tradinebotte-indicators/strategies/indicators_4h_bitcoin.json \
  bash tradinebotte-indicators/scripts/start_indicators.sh
```

---

## 9. Indicateurs planifiés (TODO)

Ces indicateurs seront ajoutés comme nouvelles valeurs de `type` dans
`_VALID_INDICATOR_TYPES` et implémentés dans `PriceSeries.compute_indicators`.
Ils réutilisent les données klines Binance existantes — aucune nouvelle source
REST ou WebSocket requise.

| Indicateur | Clés | Notes |
|---|---|---|
| **MACD** (12/26/9) | `macd`, `macd_signal`, `macd_hist` | `macd = EMA12 − EMA26` ; `signal = EMA9(macd)` ; `hist = macd − signal` |
| **Stochastic RSI** | `stoch_rsi_k`, `stoch_rsi_d` | `k = (RSI − min_RSI) / (max_RSI − min_RSI)` lissé ×3 ; `d = SMA3(k)` |

---

## 10. Fichiers associés

| Fichier | Rôle |
|---|---|
| `tradinebotte-indicators/indicators.py` | Implémentation : toutes les sources, `PriceSeries`, chargeur de config, tâches ZMQ |
| `tradinebotte-indicators/strategies/indicators_all.json` | Config unifiée de production (14 flux) |
| `tradinebotte-indicators/strategies/indicators_*.json` | Fichiers de config par flux pour les tests standalone |
| `tradinebotte-indicators/tests/test_indicators.py` | Tests unitaires et d'intégration (117 tests) |
| `docs/design.fr.md` | Topologie ZeroMQ, catalogue de messages, analyse ZeroMQ vs MQTT |
