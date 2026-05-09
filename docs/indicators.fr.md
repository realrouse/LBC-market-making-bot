# Indicateurs — Guide de référence

> 🇬🇧 [English version](indicators.md)

`bot/indicators.py` est un étage de pipeline optionnel qui souscrit aux données
de marché (feed ZeroMQ et/ou WebSocket Binance) et publie des messages enrichis
sur un socket PUB dédié. Il supporte deux catégories de données :

- **Indicateurs calculés** — appliqués à une série de prix glissante (RSI, SMA,
  EMA, volatilité). Requièrent au moins `min_ticks` points de données avant de
  publier.
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

| `source` | Catégorie | Dépendance externe | Poll par défaut |
|---|---|---|---|
| `feed` | calculé | ZeroMQ feed.py (local) | événementiel |
| `binance_ws` | calculé | Binance kline WebSocket | événementiel |
| `binance_funding` | poll | REST `fapi.binance.com` | 900 s (15 min) |
| `deribit_iv` | poll | REST `www.deribit.com` | 300 s (5 min) |
| `fear_greed` | poll | REST `api.alternative.me` | 3600 s (1 h) |
| `binance_oi` | poll | REST `fapi.binance.com` | 300 s (5 min) |
| `binance_ls_ratio` | poll | REST `fapi.binance.com` | 300 s (5 min) |
| `binance_liquidations` | poll | REST `fapi.binance.com` | 300 s (5 min) |

---

## 3. Indicateurs calculés

Ces indicateurs sont appliqués à un buffer ring `PriceSeries`. Ils se
configurent via la liste `indicators` dans une spec de flux et fonctionnent
avec les sources `feed` et `binance_ws`.

### RSI — Relative Strength Index

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

### SMA — Moyenne Mobile Simple

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

### EMA — Moyenne Mobile Exponentielle

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

### Volatilité — Volatilité glissante

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

## 4. Sources par polling

Ces sources récupèrent une valeur scalaire depuis une API REST externe. Elles
ne nécessitent pas de série de prix. `indicators: []` est obligatoire dans la
config JSON.

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

## 5. Fichiers de configuration prêts à l'emploi

Chaque fichier déclare un flux sur une paire de ports PUB + REP dédiés, prêt
à lancer comme instance indépendante de `indicators.py`.

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

`indicators.json` est une config combinée (`btc_4h` et `btc_1d`) pour les
setups mono-instance multi-flux.

### Démarrer une instance dédiée

```bash
# Klines 4h (compte-a)
TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_4h_bitcoin.json \
  bash scripts/start_indicators.sh

# Open interest (processus séparé, port séparé)
TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_oi_bitcoin.json \
  bash scripts/start_indicators.sh
```

---

## 6. Enregistrement dynamique

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

Pour les sources poll, `asset` et `timeframe` sont optionnels :

```python
req.send_json({"cmd": "subscribe", "source": "binance_oi", "asset": "BTCUSDT"})
resp = req.recv_json()
# {"status": "ok", "stream_id": "binance_oi"}
```

**Limitation :** Les flux `source="feed"` ne peuvent pas être enregistrés
dynamiquement — les déclarer dans le fichier de config JSON.

---

## 7. Configuration des ports

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
bash scripts/start_indicators.sh

# Deuxième pile indépendante — tous les ports décalés de +1000
TRADINEBOTTE_PORT_BASE=6557 \
TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_4h_bitcoin.json \
  bash scripts/start_indicators.sh
```

---

## 8. Indicateurs planifiés (TODO)

Ces indicateurs seront ajoutés comme nouvelles valeurs de `type` dans
`_VALID_INDICATOR_TYPES` et implémentés dans `PriceSeries.compute_indicators`.
Ils réutilisent les données klines Binance existantes — aucune nouvelle source
REST ou WebSocket requise.

| Indicateur | Clés | Notes |
|---|---|---|
| **MACD** (12/26/9) | `macd`, `macd_signal`, `macd_hist` | `macd = EMA12 − EMA26` ; `signal = EMA9(macd)` ; `hist = macd − signal` |
| **Bollinger Bands** (20, ±2σ) | `bb_upper`, `bb_lower`, `bb_width` | `width = (upper − lower) / middle` — mesure le régime de volatilité |
| **VWAP** | `vwap` | Nécessite le volume de la bougie (champ `v` Binance) ; reset intraday à minuit UTC |
| **Stochastic RSI** | `stoch_rsi_k`, `stoch_rsi_d` | `k = (RSI − min_RSI) / (max_RSI − min_RSI)` lissé ×3 ; `d = SMA3(k)` |

---

## 9. Fichiers associés

| Fichier | Rôle |
|---|---|
| `bot/indicators.py` | Implémentation : toutes les sources, `PriceSeries`, chargeur de config, tâches ZMQ |
| `strategies/indicators_*.json` | Fichiers de config par flux |
| `tests/test_indicators.py` | Tests unitaires et d'intégration (117 tests) |
| `docs/design.fr.md` | Topologie ZeroMQ, catalogue de messages, analyse ZeroMQ vs MQTT |
