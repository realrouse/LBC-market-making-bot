# Grid Trading Adapté — Stratégies Bear et Bull

> 🇬🇧 [English version](AdaptedGridTrading.md)

Ce document décrit trois stratégies de grid trading développées et backtestées sur
trois bases de données historiques réelles BTC/USDT couvrant des régimes de marché
distincts.

Voir aussi : [`docs/GridTrading.fr.md`](GridTrading.fr.md) pour l'algorithme de base.

---

## Contexte — Pourquoi la grille statique échoue en marché directionnel

Une grille statique standard place des ordres d'achat et de vente dans une plage de
prix fixe `[grid_lower, grid_upper]`. Elle s'arrête immédiatement quand le prix sort
de cette plage.

Backtestée sur trois régimes BTC de 90 jours (±15%, 30 niveaux, $50/ordre, $1 500) :

| Régime | Période | Mouvement | Résultat statique | Temps dans la grille |
|---|---|---|---|---|
| Latéral 2026 | Fév–Mai 2026 | $63K–$83K | **+5,0%** (+20%/an) | 96% |
| Bear (crash LUNA) | Mai–Août 2022 | $38K → $17K (−54%) | **−3,3%** | 9% |
| Bull run | Oct 2024–Jan 2025 | $66K → $108K (+64%) | **+0,1%** | 25% |

La grille statique est optimale en marché latéral. En marché directionnel elle s'arrête
dans 9–25% de la période, soit avec une perte (bear) soit avec un profit quasi nul (bull).

---

## Stratégie 1 — Grille statique (référence)

**À utiliser quand :** le marché devrait consolider dans une plage connue.

### Paramètres

| Paramètre | Recommandé | Description |
|---|---|---|
| `--range` | 15 | Grille à ±15% du prix de départ |
| `--levels` | 30 | 30 niveaux régulièrement espacés |
| `--size` | 50 | $50 USDT par ordre |
| `--trail` | off | Pas de trailing (défaut) |

**Capital requis :** `niveaux × taille = 30 × 50 $ = 1 500 $`
**Pas de grille :** ~$729 à BTC $80 705 (≈ 0,9% par niveau)

### Fonctionnement

1. Au démarrage, ordres BUY placés à tous les niveaux sous le prix courant.
2. Quand un BUY se remplit → SELL placé un step au-dessus.
3. Quand un SELL se remplit → BUY replacé un step en dessous. Profit enregistré.
4. Si le prix sort de `[grid_lower, grid_upper]` → tous les ordres annulés, BTC restant
   liquidé au prix de clôture, bot arrêté.

### Résultats backtest (±15%, 30L)

```
Latéral 2026 : +74 $  (+5,0%)  168 cycles  96% dans la grille  MaxDD 2,1%
Bear 2022    : −50 $  (−3,3%)   18 cycles   9% dans la grille  exit_low
Bull 2024    :  +2 $  (+0,1%)    5 cycles  25% dans la grille  exit_high
```

### Lancement

```bash
python3 scripts/backtest_grid.py --all
python3 scripts/backtest_grid.py --all --sweep          # recherche de paramètres
```

---

## Stratégie 2 — Grille trailing bear-adaptée

**À utiliser quand :** une tendance baissière est attendue (ou en cours), avec des
oscillations à chaque niveau.

### Concept fondamental

Au lieu de s'arrêter quand le prix passe sous `grid_lower`, la grille **se recentre
vers le bas** sur le prix de clôture courant et reprend. Le bot suit le prix vers le
bas en capturant les profits des oscillations à chaque niveau de prix sur la descente.

Quand le prix rebondit finalement au-dessus du `grid_upper` recentré, la grille sort
avec un profit — la plage ±15% serrée garantit que le rebond franchit rapidement la
borne supérieure.

```
Grille initiale : BTC $37 631  →  [$31 986 – $43 275]
  Jour 9 : prix tombe à $31 981  → recentrage à $31 981  →  [$27 184 – $36 778]
  Jour 25 : prix tombe à $27 437 → recentrage à $27 437  →  [$23 321 – $31 553]
  Jour 43 : BTC rebondit à $31 538 → SORTIE par le haut (rentable)
```

**Insight clé :** la plage ±15% est suffisamment serrée pour que les rebonds en marché
bear (fréquents même lors des crashs sévères) franchissent `grid_upper` et forcent
une sortie profitable. Une plage ±30% reste dans la grille plus longtemps mais
accumule de plus grandes pertes latentes sur le BTC.

### Paramètres

| Paramètre | Recommandé | Description |
|---|---|---|
| `--trail` | bear | Recentrage vers le bas uniquement |
| `--range` | 15 | ±15% (serré → sortie rentable sur rebond) |
| `--levels` | 30 | 30 niveaux |
| `--size` | 50 | $50/ordre |
| `--max-recenters` | 10 | Recentrages max avant stop-loss |

### Asymétrie

- Prix passe sous `grid_lower` → **recentrage vers le bas** (suit la tendance bear)
- Prix passe au-dessus de `grid_upper` → **STOP** (sort avec le profit accumulé)

Cela signifie que la stratégie bear n'est **pas** pénalisée par un bull run — si le
prix remonte soudainement, la grille sort proprement avec le profit accumulé.

### Gestion du capital au recentrage

À chaque recentrage, les nouveaux ordres BUY sont placés depuis le **budget USDT
restant**, en commençant par le niveau le plus proche du prix courant vers le bas.
Après plusieurs recentrages dans un bear market sévère, l'USDT peut être partiellement
épuisé (dépensé en BTC non encore revendu), donc la grille recentrée peut avoir moins
d'ordres BUY actifs qu'à l'origine.

### Résultats backtest (±15%, 30L, `--trail bear`)

```
Latéral 2026 : +74 $  (+5,0%)  168 cycles  96% dans la grille  0 recentrages  identique au statique
Bear 2022    : +31 $  (+2,0%)  102 cycles  33% dans la grille  2 recentrages  EXIT_HIGH sur rebond
Bull 2024    :  +2 $  (+0,1%)    5 cycles  25% dans la grille  0 recentrages  identique au statique
```

**Amélioration vs statique sur le bear market : −3,3% → +2,0% (+5,3 points)**

Détail sur le crash 2022 :
```
PnL réalisé   :  +43,90 $  (cycles complétés, nets de frais)
PnL non réalisé:  −19,83 $  (BTC détenu à $31 538 vs coût moyen ~$32K)
Frais          :  −11,76 $
PnL net        :  +30,67 $
```

### Avertissement — éviter `--trail both` en bear market

`trail=both` recentre dans les deux directions. En bear market c'est catastrophique :
il recentre vers le bas (accumule du BTC), puis vers le haut (place de nouveaux ordres
BUY), puis vers le bas à nouveau — en composant les pertes sur BTC à chaque oscillation.

```
trail=both sur bear 2022 :  −359 $  (−23,9%)  9 recentrages  409 $ de perte latente BTC
trail=bear sur bear 2022 :   +31 $  (+2,0%)   2 recentrages   20 $ de perte latente BTC
```

### Lancement

```bash
python3 scripts/backtest_grid.py --all --trail bear
python3 scripts/backtest_grid.py --all --trail bear --compare   # vs statique
python3 scripts/backtest_grid.py --all --trail bear --sweep     # recherche de paramètres
```

### Fichier de stratégie

`strategies/grid_BTCUSDT_bear_trailing.json` — calibré à BTC=$80 705 (2026-05-09) :
grille `[$68 599 – $92 811]`, step $829, 30 niveaux, $50/ordre, $1 500 de capital.
Recalibrer `grid_lower` / `grid_upper` à ±15% du prix BTC courant lors du déploiement.

---

## Stratégie 3 — Grille trailing bull-adaptée

**À utiliser quand :** une tendance haussière est attendue (ou en cours), avec des
oscillations à chaque niveau.

### Concept fondamental

Au lieu de s'arrêter quand le prix dépasse `grid_upper`, la grille **se recentre vers
le haut** sur le prix de clôture courant et reprend. Le bot suit le prix vers le haut
en capturant les profits des oscillations à chaque niveau de prix successif.

```
Grille initiale : BTC $66 084  →  [$56 171 – $75 997]
  Jour 23 : prix atteint $75 982  → recentrage à $75 982  →  [$64 585 – $87 379]
  Jour 48 : prix atteint $87 348  → recentrage à $87 348  →  [$74 246 – $100 450]
  Jour 67 : prix atteint $100 761 → recentrage à $100 761 →  [$85 647 – $115 875]
  Jour 92 : période se termine à $96 600 — bot complète la période entière
```

Dans le bull run 2024, la grille statique s'est arrêtée après 23 jours avec 5 cycles.
La grille trailing bull a tourné 92 jours entiers avec 134 cycles et 3 recentrages.

### Paramètres

| Paramètre | Recommandé | Description |
|---|---|---|
| `--trail` | bull | Recentrage vers le haut uniquement |
| `--range` | 15 | ±15% (serré → plus de cycles par segment) |
| `--levels` | 30 | 30 niveaux |
| `--size` | 50 | $50/ordre |
| `--max-recenters` | 10 | Recentrages max |

### Asymétrie

- Prix dépasse `grid_upper` → **recentrage vers le haut** (suit la tendance bull)
- Prix passe sous `grid_lower` → **STOP** (limite le downside)

Si un bull run se retourne en bear market, le bot s'arrête à `grid_lower` — même
comportement que la grille statique, limitant la perte à la largeur initiale de la grille.

### Résultats backtest (±15%, 30L, `--trail bull`)

```
Latéral 2026 : +75 $  (+5,0%)  170 cycles  100% dans la grille  1 recentrage   ✓ terminé
Bear 2022    : −50 $  (−3,3%)   18 cycles    9% dans la grille  0 recentrages  identique au statique
Bull 2024    : +55 $  (+3,7%)  134 cycles  100% dans la grille  3 recentrages  ✓ terminé
```

**Amélioration vs statique sur le bull run : +0,1% → +3,7% (+3,6 points, 26× plus de profit)**

Détail sur le bull run 2024 :
```
PnL réalisé    :  +58,86 $  (cycles complétés, nets de frais)
PnL non réalisé:  −10,85 $  (petite position BTC en fin de période)
Frais          :  −13,72 $
PnL net        :  +54,95 $
```

Sur la période latérale 2026, la stratégie bull performe légèrement mieux que la
statique (100% dans la grille vs 96%, 1 recentrage résout le bref pic au-dessus de
$81 863).

### Lancement

```bash
python3 scripts/backtest_grid.py --all --trail bull
python3 scripts/backtest_grid.py --all --trail bull --compare   # vs statique
python3 scripts/backtest_grid.py --all --trail bull --sweep     # recherche de paramètres
```

### Fichier de stratégie

`strategies/grid_BTCUSDT_bull_trailing.json` — mêmes bornes de grille que le bear
trailing. Recalibrer `grid_lower` / `grid_upper` à ±15% du prix BTC courant.

---

## Guide de sélection de stratégie

```
Analyse du marché
├── Consolidation / range attendu ?
│     └── Statique ou bull trailing (quasi identiques)
│           python3 scripts/backtest_grid.py --all
│           python3 scripts/backtest_grid.py --all --trail bull
│
├── Tendance baissière / bear market attendu ?
│     └── Bear trailing
│           python3 scripts/backtest_grid.py --all --trail bear
│
├── Tendance haussière / bull run attendu ?
│     └── Bull trailing
│           python3 scripts/backtest_grid.py --all --trail bull
│
└── Incertain ?
      └── Bear trailing (asymétrique : profite des oscillations à la baisse,
            sort proprement si le marché remonte)
```

**Ne jamais utiliser `--trail both` en marché directionnel.** Approprié uniquement
en range confirmé où l'on veut que la grille suive le prix dans les deux directions
sans s'arrêter.

---

## Tableau de comparaison complet

Trois régimes, trois stratégies, ±15%, 30 niveaux, $50/ordre, $1 500 de capital :

| Stratégie | Régime | Cycles | PnL net | PnL% | Ann% | MaxDD | Temps% | Recentrages |
|---|---|---|---|---|---|---|---|---|
| Statique | Latéral 2026 | 168 | +74 $ | +5,0% | +20% | 2,1% | 96% | — |
| Statique | Bear 2022 | 18 | −50 $ | −3,3% | −13% | 3,5% | 9% | — |
| Statique | Bull 2024 | 5 | +2 $ | +0,1% | +1% | 0,1% | 25% | — |
| Bear trailing | Latéral 2026 | 168 | +74 $ | +5,0% | +20% | 2,1% | 96% | 0 |
| **Bear trailing** | **Bear 2022** | **102** | **+31 $** | **+2,0%** | **+8%** | 13,1% | 33% | **2** |
| Bear trailing | Bull 2024 | 5 | +2 $ | +0,1% | +1% | 0,1% | 25% | 0 |
| Bull trailing | Latéral 2026 | 170 | +75 $ | +5,0% | +20% | 2,1% | 100% | 1 |
| Bull trailing | Bear 2022 | 18 | −50 $ | −3,3% | −13% | 3,5% | 9% | 0 |
| **Bull trailing** | **Bull 2024** | **134** | **+55 $** | **+3,7%** | **+15%** | 1,7% | 100% | **3** |

---

## Résultats des sweeps de paramètres

### Sweep bear trailing (meilleures configs par Calmar moyen sur 3 régimes)

```
±Plage  Niv    Latéral    Bear 2022   Bull 2024   CalmarMoy  PnLMoy
  30%    20   +2,7% 1%  −2,4%  28%  +0,2%  0%      2,26     +0,2%
  15%    20   +5,2% 2%  +2,0%  13%  +0,2%  0%      1,71     +2,5%  ← meilleur PnL
  15%    30   +5,0% 2%  +2,0%  13%  +0,1%  0%      1,53     +2,4%
```

### Sweep bull trailing (meilleures configs par Calmar moyen)

```
±Plage  Niv    Latéral    Bear 2022   Bull 2024   CalmarMoy  PnLMoy
  20%    20   +3,7% 2%  −4,7%   5%  +2,1%  0%      2,19     +0,4%
  15%    20   +5,2% 2%  −3,4%   4%  +3,6%  2%      1,17     +1,8%  ← meilleur PnL
  15%    30   +5,0% 2%  −3,3%   3%  +3,7%  2%      1,17     +1,8%  ← meilleur PnL
```

---

## Reproduire n'importe quel résultat

```bash
# Statique — défaut
python3 scripts/backtest_grid.py --all --range 15 --levels 30 --size 50

# Bear trailing recommandé
python3 scripts/backtest_grid.py --all --range 15 --levels 30 --trail bear --compare

# Bull trailing recommandé
python3 scripts/backtest_grid.py --all --range 15 --levels 30 --trail bull --compare

# Sweep complet — mode bear
python3 scripts/backtest_grid.py --all --trail bear --sweep --sort pnl

# Sweep complet — mode bull
python3 scripts/backtest_grid.py --all --trail bull --sweep --sort pnl
```

---

## Fichiers liés

| Fichier | Rôle |
|---|---|
| `scripts/backtest_grid.py` | Moteur de backtest (statique + trailing) |
| `scripts/download_btc_history.py` | Téléchargement OHLCV depuis Binance |
| `strategies/grid_BTCUSDT_tight.json` | Config statique ±15% |
| `strategies/grid_BTCUSDT_moderate.json` | Config statique ±20% |
| `strategies/grid_BTCUSDT_bear_trailing.json` | Config bear trailing |
| `strategies/grid_BTCUSDT_bull_trailing.json` | Config bull trailing |
| `bot/strategies/grid.py` | Implémentation live de GridStrategy |
| `docs/GridTrading.fr.md` | Documentation de l'algorithme de base |
| `data/BTCUSDT_1m*.db` | Bases OHLCV (exclues du git) |
