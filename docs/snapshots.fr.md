# Snapshots — Référence technique

La table `snapshots` est un journal en série temporelle de l'état du carnet
d'ordres de chaque token actif, écrit à intervalle fixe pendant l'exécution
du bot. C'est la principale source de données pour le backtest et l'analyse
de stratégie.

---

## Schéma de la table

```sql
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms          INTEGER,   -- Horodatage Unix en millisecondes (horloge murale)
    market_id      TEXT,      -- Identifiant CLOB du marché Polymarket
    token_id       TEXT,      -- Identifiant du token de résultat (côté YES ou NO)
    direction      TEXT,      -- "UP" ou "DOWN" (dérivé du titre du marché)
    secs_remaining REAL,      -- Secondes avant fermeture du marché au moment du snapshot
    best_bid       REAL,      -- Meilleure offre d'achat dans le carnet (échelle 0–1)
    best_ask       REAL,      -- Meilleure offre de vente dans le carnet (échelle 0–1)
    spread         REAL,      -- best_ask − best_bid (≥ 0)
    ask_vol        REAL,      -- Volume total offert au meilleur niveau ask
    obi            REAL,      -- Déséquilibre du carnet d'ordres (voir ci-dessous, plage −1 à +1)
    has_open_trade INTEGER DEFAULT 0  -- 1 si le bot avait un trade ouvert sur ce
                                      -- marché au moment du snapshot, 0 sinon
);
```

### Index

```sql
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_resolved ON trades(resolved);
```

Aucun index n'est créé sur `snapshots` par défaut. Pour de grandes requêtes
analytiques, en créer un manuellement sur `(ts_ms)` ou `(market_id, ts_ms)` :

```sql
CREATE INDEX idx_snap_ts ON snapshots(ts_ms);
```

---

## Définition des colonnes

| Colonne | Type | Source | Notes |
|---|---|---|---|
| `id` | INTEGER | Auto-incrémentation SQLite | Croissant monotone ; des écarts peuvent exister après une rotation |
| `ts_ms` | INTEGER | `int(time.time() * 1000)` à l'écriture | Heure murale, UTC, précision milliseconde |
| `market_id` | TEXT | API Gamma → `TokenState.market_id` | Stable pour toute la durée du marché |
| `token_id` | TEXT | API Gamma → `TokenState.token_id` | Identifie le token de résultat YES/NO |
| `direction` | TEXT | Dérivé du titre du marché | `"UP"` ou `"DOWN"` |
| `secs_remaining` | REAL | Calculé depuis `end_date_ts` du marché | Décroît vers 0 ; négatif après la fermeture |
| `best_bid` | REAL | Carnet d'ordres WebSocket | Le signal d'entrée se déclenche quand `best_bid >= 0.96` |
| `best_ask` | REAL | Carnet d'ordres WebSocket | Garde : si `best_ask >= 1.0`, le marché est résolu |
| `spread` | REAL | `max(0, best_ask − best_bid)` | Spread élevé → carnet mince, risque de glissement |
| `ask_vol` | REAL | Quantité au sommet du carnet côté ask | Utilisé dans le calcul de l'OBI (voir ci-dessous) |
| `obi` | REAL | Calculé depuis le carnet d'ordres | Déséquilibre du carnet ; voir la formule ci-dessous |
| `has_open_trade` | INTEGER | `1 if market_id in state.open_trades` | Signale les lignes où un trade était actif |

**Ce qui n'est PAS stocké :**

- `bid_vol` — la composante numérateur de l'OBI n'est pas persistée directement ;
  on peut la dériver depuis l'OBI et `ask_vol` (voir formule ci-dessous).
- Les niveaux individuels du carnet d'ordres au-delà du sommet.
- Les payloads bruts des messages WebSocket.
- La configuration du bot au moment du snapshot (capital, seuils, etc.).

---

## Formule OBI

L'OBI (Order Book Imbalance — Déséquilibre du Carnet d'Ordres) est calculé dans
`api_polymarket.py:parse_book_message()` :

```python
bv = sum(float(e["size"]) for e in bids)  # volume total côté bid
av = sum(float(e["size"]) for e in asks)  # volume total côté ask
tv = bv + av
obi = (bv - av) / tv if tv > 0 else 0.0
```

Plage : **−1,0** (tout le volume est côté ask, pression baissière) à **+1,0**
(tout le volume est côté bid, pression haussière). Une valeur proche de 0
indique un carnet équilibré.

**Reconstruction de `bid_vol` depuis les colonnes stockées** (approximation,
sommet du carnet uniquement) :

```
bid_vol = ask_vol * (1 + obi) / (1 - obi)   [quand obi ≠ 1]
```

Cette reconstruction n'est qu'approximative car `bid_vol` couvre tous les
niveaux bid alors qu'`ask_vol` stocké ici ne concerne que le sommet du carnet.

---

## Timing d'écriture

Un snapshot est écrit une fois par token actif toutes les `SNAPSHOT_INTERVAL`
secondes.

La vérification s'effectue dans `handle_book_update()` après chaque message
WebSocket :

```python
now = time.time()
if now - ts.last_snapshot_ts >= state.config.snapshot_interval:
    ts.bid_history.append(ts.best_bid)
    ts.obi_history.append(ts.obi)
    if state.config.enable_snapshots:
        save_snapshot(state, ts)
    ts.last_snapshot_ts = now
```

Points clés :
- L'intervalle est **piloté par les événements**, pas par une horloge. Un snapshot
  se déclenche sur la première mise à jour WebSocket arrivant au moins
  `snapshot_interval` secondes après le snapshot précédent pour ce token.
- Si le WebSocket est silencieux, les snapshots s'interrompent en conséquence.
- Chaque appel à `save_snapshot()` émet un `INSERT` et un `COMMIT`.

### Configurer l'intervalle

| Méthode | Comment |
|---|---|
| Défaut (1 s) | Constante de compilation `SNAPSHOT_INTERVAL = 1` dans `live_bot.py` |
| `config.json` | `"snapshot_interval": N` dans la section `[hour_filter]` |
| Flag CLI | `--snapshot-interval N` (prend le dessus sur tous les autres réglages) |
| Désactivation totale | Flag `--no-snapshots` |

Le compte de collecte de données utilise `--snapshot-interval 1` (déjà la
valeur par défaut). Pour alléger les I/O, passer `--snapshot-interval 5`.

---

## Estimations de stockage

À un instant donné, 2 à 4 marchés actifs sont typiquement suivis (paires
Bitcoin UP/DOWN dans la fenêtre ±6 min).

| Intervalle | Lignes/min | Lignes/jour | Lignes/semaine | Taille DB approx./semaine |
|---|---|---|---|---|
| 1 s | ~120 | ~172 800 | ~1 210 000 | ~200 Mo |
| 5 s | ~24 | ~34 560 | ~242 000 | ~40 Mo |

Ces estimations supposent 2 tokens actifs et une connectivité WebSocket continue.
Les périodes calmes (peu de marchés) produisent moins de lignes.

SQLite en mode WAL gère ce volume sans problème. Le workflow hebdomadaire
`collect_db.sh --rotate` archive la base avant qu'elle ne grossisse sans limite.

---

## Isolation des données

Chaque instance du bot écrit dans sa propre base de données :

| Compte | Chemin de la base |
|---|---|
| Bot de trading | `~/tradinebotte/live.db` |
| Bot de collecte | `~/tradinebotte-collector/live.db` |

Le compte de collecte tourne avec `--simulate --snapshot-interval 1` (aucun
ordre réel, densité de snapshots maximale).

---

## Utilisation par le backtest

Le moteur de backtest rejoue la table `snapshots` pour simuler la stratégie :

| Colonne | Usage dans le backtest |
|---|---|
| `ts_ms` | Ordonnancement chronologique ; calcul de la dérive de `secs_remaining` |
| `best_bid` | Signal d'entrée : `best_bid >= SIGNAL_THRESHOLD (0.96)` |
| `best_ask` | Garde marché résolu : `best_ask >= 1.0` → ignorer |
| `secs_remaining` | Porte d'entrée : doit être `>= MIN_SECS_REMAINING (45 s)` |
| `obi` | Filtre : OBI positif confirme la dynamique directionnelle |
| `ask_vol` | Contrôle de liquidité : carnet mince → ignorer |
| `has_open_trade` | Empêche une nouvelle entrée pendant un trade déjà ouvert |

---

## L'angle mort à 5 s (contexte historique)

Avant le commit `7a8b351`, `SNAPSHOT_INTERVAL` valait **5 secondes** par défaut.

Un écart de 5 s signifiait que le backtest ne voyait pas les plongées de
`best_bid` sous `0.01` durant moins de 5 s. Ces plongées représentent des
marchés se résolvant en PERTE (LOSS). Le WebSocket en direct détecte ces
événements à chaque changement de carnet et ferme les trades en LOSS. Le
backtest ne les voyait pas — il ne lisait que le snapshot suivant, souvent
après que le prix s'était rétabli.

Conséquence : le backtest sur-comptait les victoires, produisant un taux de
réussite apparent supérieur à ce que le bot en direct réalisait. Sur une session
de 3 mois, cela représentait environ **50 événements LOSS supplémentaires**
invisibles au backtest.

Correction : `SNAPSHOT_INTERVAL = 1` (valeur par défaut depuis ce commit). À
1 s, les plongées de prix de courte durée sont capturées et les résultats du
backtest s'alignent étroitement sur les performances en direct.

---

## Requêtes SQL utiles

### Nombre de lignes et plage de dates

```sql
SELECT count(*) as lignes,
       datetime(min(ts_ms)/1000, 'unixepoch') as premier,
       datetime(max(ts_ms)/1000, 'unixepoch') as dernier
FROM snapshots;
```

### Moyenne du best_bid par direction et par jour

```sql
SELECT date(ts_ms/1000, 'unixepoch') as jour,
       direction,
       round(avg(best_bid), 4) as bid_moyen,
       count(*) as lignes
FROM snapshots
GROUP BY jour, direction
ORDER BY jour DESC;
```

### Tous les snapshots pour un trade ouvert

```sql
SELECT datetime(s.ts_ms/1000, 'unixepoch') as ts,
       s.best_bid, s.best_ask, s.obi, s.secs_remaining
FROM snapshots s
JOIN trades t ON s.market_id = t.market_id
WHERE t.id = 42          -- remplacer par l'id du trade
  AND s.ts_ms BETWEEN t.entry_ts_ms AND coalesce(t.resolution_ts_ms, t.entry_ts_ms + 3600000)
ORDER BY s.ts_ms;
```

### Proportion du temps avec un trade ouvert

```sql
SELECT round(100.0 * sum(has_open_trade) / count(*), 2) as pct_en_trade
FROM snapshots;
```

### Distribution du bid (histogramme par paliers)

```sql
SELECT round(best_bid, 1) as palier, count(*) as n
FROM snapshots
GROUP BY palier
ORDER BY palier DESC;
```

---

## Fichiers liés

| Fichier | Rôle |
|---|---|
| `bot/live_bot.py` | `save_snapshot()`, `SNAPSHOT_INTERVAL`, flag `--snapshot-interval` |
| `bot/api_polymarket.py` | `parse_book_message()` — calcul de l'OBI et des volumes |
| `scripts/collect_db.sh` | Téléchargement / rotation du `live.db` distant |
| `scripts/start_collector.sh` | Déploiement du bot de collecte de données |
| `scripts/schedule_collect.sh` | Installation du cron hebdomadaire de rotation + téléchargement |
| `data/` | Archive locale des fichiers `live_YYYY_WNN.db` téléchargés |
