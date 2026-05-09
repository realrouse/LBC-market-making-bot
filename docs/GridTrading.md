# Grid Trading — Mode de fonctionnement et setup

## Qu'est-ce que le grid trading ?

Le grid trading est une stratégie qui divise une plage de prix en niveaux
régulièrement espacés (une « grille ») et place des ordres LIMIT BUY sous le
prix courant et des ordres LIMIT SELL au-dessus. Chaque fois que le prix
oscille entre deux niveaux, un cycle achat/vente se complète et génère un
profit égal à l'espacement de la grille multiplié par la quantité échangée.

La stratégie est **market-neutral** : elle ne parie pas sur une direction.
Elle profite de la volatilité dans les deux sens, tant que le prix reste à
l'intérieur de la plage `[grid_lower, grid_upper]`.

---

## Différences avec la stratégie threshold (Polymarket)

| | Threshold | Grid |
|---|---|---|
| Marché | Polymarket binaire (UP/DOWN) | CEX spot continu (BTC/USDT) |
| Échelle de prix | 0–1 (probabilité) | USDT absolu (~90 000–110 000) |
| Positions | Une à la fois, résolution binaire | Plusieurs niveaux simultanés |
| Durée | ~45–300 secondes | Indéfinie (tourne en continu) |
| Signal d'entrée | `best_bid >= 0.96` | Prix croise un niveau de grille |
| Profit | Valeur pari × (1 − frais) | Espacement × quantité − frais |
| Source de données | WebSocket Polymarket | WebSocket Binance / MEXC |

---

## Architecture

### Fichiers concernés

```
bot/
  strategies/
    __init__.py      ← factory load("grid", config) → GridStrategy
    base.py          ← protocole Strategy (interface commune)
    grid.py          ← GridStrategy, GridLevel, GridState
  connectors/
    __init__.py      ← factory load("binance"|"mexc") → api_* module
  api_binance.py     ← connecteur Binance (REST + WebSocket)
  api_mexc.py        ← connecteur MEXC (REST + WebSocket)
  live_bot.py        ← orchestrateur (charge stratégie + connecteur)
strategies/
  grid_BTCUSDT.json  ← exemple de config grid
```

### Flux d'exécution

```
main()
 ├─ make_config()          lit grid_BTCUSDT.json
 ├─ _load_connector("binance")   remplace api_polymarket par api_binance
 ├─ BotState(conn, config)
 ├─ load_strategy("grid", config) → GridStrategy(config)
 │    └─ calcule les niveaux de prix + valide les bornes
 └─ ws_loop()
      └─ handle_book_update()
           └─ state.strategy.on_book_update(state, ts)
                └─ GridStrategy.on_book_update() [voir algorithme]
```

---

## Algorithme

### Initialisation

```
grid_step = (grid_upper - grid_lower) / (grid_levels - 1)
levels    = [grid_lower + i × grid_step  for i in 0..grid_levels-1]
```

Exemple : `grid_lower=90000`, `grid_upper=110000`, `grid_levels=21`
→ `grid_step = 1000` USDT
→ niveaux = [90 000, 91 000, 92 000, …, 110 000]

### Placement initial (au premier tick)

- Prix courant = 100 000 USDT
- Ordres BUY placés à : 90 000, 91 000, …, 99 000
- Ordres SELL placés à : 101 000, 102 000, …, 110 000

### Cycle complet (profit unitaire)

```
1. BUY fill à 99 000 USDT
   → placer SELL à 100 000 USDT (= 99 000 + grid_step)

2. SELL fill à 100 000 USDT
   → profit brut = grid_step × qty_btc = 1000 × (50 / 99 000) ≈ 0.505 USDT
   → placer BUY à 99 000 USDT (cycle recommence)
```

### Stop-loss de grille

Si `best_bid < grid_lower` ou `best_bid > grid_upper` : annuler tous les
ordres ouverts et arrêter la grille. Le bot loggue l'événement et s'arrête.

### État par niveau (`GridLevel`)

| Champ | Type | Description |
|---|---|---|
| `price` | float | Prix de ce niveau en USDT |
| `buy_order_id` | str \| None | ID de l'ordre BUY actif sur ce niveau |
| `sell_order_id` | str \| None | ID de l'ordre SELL actif sur ce niveau |
| `status` | str | `idle` \| `buy_placed` \| `sell_placed` \| `cycle_complete` |
| `filled_at_ts` | float \| None | Timestamp Unix du dernier fill |

---

## Configuration

### Fichier de stratégie JSON

```json
{
    "strategy_type": "grid",
    "connector":     "binance",

    "grid_symbol":          "BTCUSDT",
    "grid_lower":           90000.0,
    "grid_upper":           110000.0,
    "grid_levels":          20,
    "grid_order_size_usdt": 50.0,

    "capital_start":  1000.0,
    "stake":            50.0,
    "daily_stop_loss": 100.0,

    "hour_filter": { "enabled": false }
}
```

### Paramètres grid

| Clé | Type | Requis | Description |
|---|---|---|---|
| `strategy_type` | string | oui | Doit être `"grid"` |
| `connector` | string | oui | `"binance"` ou `"mexc"` |
| `grid_symbol` | string | oui | Paire, ex. `"BTCUSDT"` |
| `grid_lower` | float | oui | Borne basse de la grille (USDT, > 0) |
| `grid_upper` | float | oui | Borne haute de la grille (USDT, > `grid_lower`) |
| `grid_levels` | int | oui | Nombre de niveaux (≥ 2) |
| `grid_order_size_usdt` | float | oui | Montant USDT par ordre (> 0) |

### Paramètres communs utilisés par le grid

| Clé | Description |
|---|---|
| `capital_start` | Capital initial en USDT |
| `daily_stop_loss` | Perte journalière maximale avant arrêt |
| `hour_filter` | Filtre horaire (même format que threshold) |

### Paramètres ignorés par le grid

Les clés `signal_threshold`, `entry_max`, `min_secs_remaining`, `obi_reject_thresh`,
`win_threshold`, `loss_threshold`, `min_ask_vol` sont des paramètres propres
à la stratégie threshold sur Polymarket — elles n'ont pas de sens pour le grid.

---

## Variables d'environnement

### Binance

```bash
export BINANCE_API_KEY="votre_clé_api"
export BINANCE_API_SECRET="votre_secret_api"
```

### MEXC

```bash
export MEXC_API_KEY="votre_clé_api"
export MEXC_API_SECRET="votre_secret_api"
```

En l'absence de ces variables, les ordres sont **simulés** (aucun ordre réel
ne part, identique au comportement `--simulate` du bot Polymarket).

---

## Setup et lancement

### 1. Configurer l'exchange

Sur Binance ou MEXC, créer une clé API avec les permissions :
- **Lecture** (obligatoire)
- **Trading spot** (obligatoire pour les ordres réels)
- **Retrait** : désactiver impérativement

### 2. Créer le fichier de stratégie

Copier `strategies/grid_BTCUSDT.json` et ajuster les bornes selon le prix
actuel de BTC et la volatilité attendue :

```bash
cp strategies/grid_BTCUSDT.json strategies/grid_BTCUSDT_live.json
# éditer grid_lower, grid_upper, grid_levels, grid_order_size_usdt
```

Règle empirique pour le calibrage :
- `grid_lower` / `grid_upper` : couvrir 10–20 % de part et d'autre du prix
  courant (ex. BTC à 100k → [88k, 112k])
- `grid_levels` : 20–50 niveaux → `grid_step` de 500–2000 USDT
- `grid_order_size_usdt` : au moins 10 × `grid_step` de capital disponible
  pour ne pas se retrouver à court de fonds si tous les BUY se remplissent

### 3. Référencer la stratégie dans config.json

```json
{
    "strategy": "/chemin/vers/strategies/grid_BTCUSDT_live.json"
}
```

### 4. Lancer en simulation d'abord

```bash
BINANCE_API_KEY="" BINANCE_API_SECRET="" bash scripts/start_bot.sh --simulate
```

Vérifier les logs pour confirmer que `GridStrategy` charge correctement
et que les niveaux sont calculés.

### 5. Lancer en production

```bash
export BINANCE_API_KEY="..."
export BINANCE_API_SECRET="..."
bash scripts/start_bot.sh
```

---

## Calcul de rentabilité

### Profit par cycle

```
qty_btc      = grid_order_size_usdt / prix_achat
profit_brut  = grid_step × qty_btc
frais_achat  = prix_achat × qty_btc × fee_rate
frais_vente  = (prix_achat + grid_step) × qty_btc × fee_rate
profit_net   = profit_brut − frais_achat − frais_vente
```

Exemple (Binance, fee_rate = 0.1%) :
```
grid_order_size = 50 USDT
prix_achat      = 99 000 USDT
qty_btc         = 50 / 99 000 = 0.000505 BTC
grid_step       = 1 000 USDT

profit_brut = 1 000 × 0.000505 = 0.505 USDT
frais       ≈ 0.10 USDT  (0.1% × 2 × ~50 USDT)
profit_net  ≈ 0.40 USDT par cycle
```

### Fréquence des cycles

Dépend entièrement de la volatilité. Sur BTC/USDT avec un `grid_step` de
1 000 USDT et une volatilité journalière de ±3 000 USDT, on peut s'attendre
à 6–15 cycles par niveau actif par jour.

### Risque principal : sortie de grille

Si le prix sort de `[grid_lower, grid_upper]`, tous les ordres BUY sont
remplis (prix descend) ou tous les SELL (prix monte), et la grille doit
être reconfigurée. La perte maximale correspond au coût d'achat de tous
les niveaux BUY si le prix s'effondre.

```
perte_max_théorique = grid_levels × grid_order_size_usdt
```

Avec 20 niveaux à 50 USDT = 1 000 USDT de capital engagé maximum.

---

## État d'implémentation

| Composant | État |
|---|---|
| `GridStrategy.__init__()` — calcul des niveaux | ✅ Implémenté |
| `GridStrategy.level_at_price()` — lookup par prix | ✅ Implémenté |
| `GridState`, `GridLevel` — structures de données | ✅ Implémenté |
| Validation des paramètres (bornes, niveaux, taille) | ✅ Implémenté |
| `connectors.load("binance")` / `load("mexc")` | ✅ Implémenté |
| `strategies.load("grid", config)` — factory | ✅ Implémenté |
| Routage dans `handle_book_update()` | ✅ Implémenté |
| `_initialise_grid()` — placement initial des ordres | ✅ Implémenté |
| `_poll_fills()` — détection des fills par REST | ✅ Implémenté |
| `_on_buy_filled()` / `_on_sell_filled()` — counter-orders | ✅ Implémenté |
| `_check_stop_loss()` — annulation si hors grille | ✅ Implémenté |
| `_save_state()` — persistance SQLite (`grid_state` + `grid_levels`) | ✅ Implémenté |
| `restore_from_db()` — reprise au redémarrage + réconciliation exchange | ✅ Implémenté |
| WebSocket order stream (fills en temps réel) | 🔲 TODO |

---

## Ajouter un connecteur CEX

Pour brancher un exchange autre que Binance ou MEXC :

1. Créer `bot/api_monexchange.py` en copiant `api_binance.py`
2. Implémenter les 8 fonctions de l'interface (voir `bot/connectors/__init__.py`)
3. Ajouter l'entrée dans `bot/connectors/__init__.py` :
   ```python
   _REGISTRY["monexchange"] = "api_monexchange"
   ```
4. Utiliser `"connector": "monexchange"` dans le JSON de stratégie

---

## Fichiers liés

| Fichier | Rôle |
|---|---|
| `bot/strategies/grid.py` | Implémentation `GridStrategy` |
| `bot/strategies/__init__.py` | Factory de stratégies |
| `bot/strategies/base.py` | Protocole `Strategy` (interface) |
| `bot/connectors/__init__.py` | Factory de connecteurs |
| `bot/api_binance.py` | Connecteur Binance REST + WebSocket |
| `bot/api_mexc.py` | Connecteur MEXC REST + WebSocket |
| `bot/live_bot.py` | Orchestrateur — `_load_connector()`, routage |
| `strategies/grid_BTCUSDT.json` | Config d'exemple BTC/USDT Binance |
