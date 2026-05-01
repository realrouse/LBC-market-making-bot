# Kelly Fractionnel — Sizing dynamique des mises

> Contexte : remplacement du `STAKE = $10` fixe par une mise proportionnelle
> au signal et au capital courant. Statut : **non implémenté** (roadmap v0.3).

---

## Principe : Kelly complet

La formule de Kelly donne la fraction optimale du capital à miser pour
maximiser la croissance géométrique du portefeuille à long terme :

```
f* = (p · b - q) / b
```

| Variable | Signification |
|---|---|
| `p` | Probabilité estimée de gagner |
| `q` | `1 - p` (probabilité de perdre) |
| `b` | Ratio gain net / mise (ex. gagner 8 $ sur 10 $ misés → b = 0.8) |

---

## Application aux marchés BTC 5-min de Polymarket

Pour un signal à `best_bid = 0.97`, `best_ask = 0.975` :

```
p = 0.97
b = (1 - best_ask) / best_ask = (1 - 0.975) / 0.975 ≈ 0.0256
q = 0.03

f* = (0.97 × 0.0256 - 0.03) / 0.0256
   = (0.0248 - 0.03) / 0.0256
   ≈ -0.20   → Kelly négatif : ne pas entrer
```

À `best_bid = 0.99` :

```
b = (1 - 0.99) / 0.99 ≈ 0.0101

f* = (0.99 × 0.0101 - 0.01) / 0.0101 ≈ 0.0
```

Les marchés binaires à 96 %+ ont un `b` très faible (le gain potentiel est
petit par rapport au risque), ce qui donne des mises Kelly naturellement
petites. C'est cohérent avec le fait que le signal est rare et très fiable.

---

## Pourquoi « fractionnel » ?

Kelly complet est théoriquement optimal mais **dangereux en pratique** : il
suppose que `p` est connue avec certitude. Or `best_bid` est une estimation
de marché, pas la vraie probabilité de résolution. Une erreur sur `p` entraîne
une surmise catastrophique.

La convention universelle est d'utiliser **¼ Kelly ou ½ Kelly** :

```
mise = f* × capital × fraction_kelly   (recommandé : 0.25)
```

Effet : réduit la volatilité du capital d'un facteur 4, au prix d'une
croissance légèrement moins rapide. Standard dans le trading quantitatif.

---

## Ce que ça changerait dans le bot

**Situation actuelle :** `STAKE = $10` fixe, indépendant du signal.

**Avec Kelly fractionnel :**

| Signal (`best_bid`) | Mise estimée |
|---|---|
| 0.960 (seuil minimal) | ~$3–5 (signal faible → mise faible) |
| 0.975 | ~$7–9 |
| 0.985 (signal fort) | ~$12–15 |
| Capital réduit après pertes | Mises automatiquement réduites |

La mise serait bornée pour éviter les cas extrêmes :

```python
mise = min(STAKE_MAX, max(STAKE_MIN, kelly_stake))
```

Paramètres à ajouter dans le JSON de stratégie :

```json
"kelly": {
    "enabled": false,
    "fraction": 0.25,
    "stake_min": 2.0,
    "stake_max": 25.0
}
```

---

## Risque principal

`best_bid` reflète le **prix du marché**, pas la vraie probabilité de
résolution. Un marché à 0.97 peut l'être parce que les autres traders sont
confiants — ou parce que le marché est mal coté. Kelly amplifie les erreurs
sur `p` : une surestimation systématique de `p` produit des ruines plus
rapides qu'une mise fixe conservative.

**Pré-requis avant activation :**
- Dataset de backtest suffisamment large (≥ 500 trades simulés)
- Estimation empirique de `p` réelle vs `best_bid` (calibration)
- Validation walk-forward pour détecter l'overfitting

---

## Référence

- Kelly, J.L. (1956). "A New Interpretation of Information Rate". Bell System Technical Journal.
- En pratique : ½ Kelly est le compromis le plus courant ; ¼ Kelly pour les stratégies à haute fréquence où l'estimation de `p` est incertaine.
