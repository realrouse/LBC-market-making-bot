# Passer en mode réel — de la simulation à l'argent réel

> 🇬🇧 [English version](going-live.md)

Tous les bots détectent les credentials au démarrage via des variables
d'environnement. En l'absence des variables concernées, le bot tourne en
**mode simulation** : les fonctions d'ordre renvoient des IDs `sim_...`,
aucun ordre réel n'est passé et aucun fond ne bouge. Passer en mode réel
se fait en trois étapes : définir les credentials sur le serveur distant →
les injecter dans le service systemd → mettre à jour la page de statut.

---

## 1. Credentials requis par bot

| Bot | Connecteur | Variables d'environnement |
|-----|------------|--------------------------|
| `live_bot` / `account_bot` (Polymarket) | `polymarket` | `POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET` |
| `grid_bot` / `swing_bot` (Binance) | `binance` | `BINANCE_API_KEY`, `BINANCE_API_SECRET` |
| `accumulation_bot` | `binance` | `BINANCE_API_KEY`, `BINANCE_API_SECRET` |
| `orderbook_bot` | `binance` | `BINANCE_API_KEY`, `BINANCE_API_SECRET` |
| `grid_bot` (MEXC Futures) | `mexc_futures` | `MEXC_FUTURES_API_KEY`, `MEXC_FUTURES_API_SECRET` |
| `grid_bot` / `swing_bot` (MEXC spot) | `mexc` | `MEXC_API_KEY`, `MEXC_API_SECRET` |

**Note Polymarket :** `POLY_API_KEY`, `POLY_API_SECRET` et `POLY_API_PASSPHRASE`
sont dérivés de la clé privée du wallet. Exécuter `python3 scripts/setup.py`
une seule fois sur le compte pour les générer ; le script les écrit dans
`~/.polymarket_creds`, qu'on peut ensuite sourcer dans l'environnement.

---

## 2. Créer un fichier de credentials sur le compte distant

Se connecter en SSH sur le compte cible et créer `~/.tradinebotte-creds` :

```bash
cat > ~/.tradinebotte-creds << 'EOF'
# Binance — utilisé par accumulation_bot, orderbook_bot, grid_bot (binance), swing_bot
BINANCE_API_KEY=votre_cle_ici
BINANCE_API_SECRET=votre_secret_ici

# MEXC Futures — utilisé par grid_bot (connecteur mexc_futures)
# MEXC_FUTURES_API_KEY=votre_cle_ici
# MEXC_FUTURES_API_SECRET=votre_secret_ici

# Polymarket — utilisé par live_bot / account_bot
# POLY_PRIVATE_KEY=0x...
# POLY_API_KEY=...
# POLY_API_SECRET=...
# POLY_API_PASSPHRASE=...
EOF
chmod 600 ~/.tradinebotte-creds
```

Ce fichier n'est **jamais rsynced** par aucun script de déploiement — il vit
uniquement sur le compte distant et doit être créé manuellement.

---

## 3. Injecter les credentials dans le service systemd (drop-in override)

Les templates de service ne contiennent pas de directive `EnvironmentFile=` par
défaut. Utiliser un **drop-in override** pour qu'il survive aux recharges et
aux déploiements futurs :

```bash
# Sur le compte distant — à faire une seule fois par service concerné
export XDG_RUNTIME_DIR=/run/user/$(id -u)

systemctl --user edit tradinebotte-live.service
```

Un éditeur ouvre `~/.config/systemd/user/tradinebotte-live.service.d/override.conf`.
Ajouter :

```ini
[Service]
EnvironmentFile=%h/.tradinebotte-creds
```

Sauvegarder, puis recharger :

```bash
systemctl --user daemon-reload
systemctl --user restart tradinebotte-live.service
```

Répéter pour les autres unités si nécessaire
(`tradinebotte-accumulation.service`, `tradinebotte-orderbook.service`).

> **Pourquoi un drop-in ?** Le fichier de service de base est écrasé à chaque
> rsync de déploiement. Un drop-in dans `service.d/` n'est jamais touché par
> rsync et est fusionné automatiquement au rechargement.

---

## 4. Redéployer pour prendre en compte le changement

```bash
# Redéployer tous les comptes
bash tradinebotte-cex/scripts/deploy_all.sh

# Ou cibler un compte directement, par ex. :
bash tradinebotte-polymarket/scripts/update_claude2.sh
```

---

## 5. Vérifier — confirmer le mode réel dans le log de démarrage

Après le redémarrage, l'avertissement « orders SIMULATED » doit être
**absent** du log de démarrage :

```bash
# Sur le compte distant
grep -iE "simul|LIVE BOT|credentials" ~/tradinebotte/live.log | tail -10
```

En mode réel, la bannière de démarrage affiche le connecteur et la stratégie
sans aucune mention simulation. En mode simulation, on voit :

```
[INFO] POLY_PRIVATE_KEY not set — orders SIMULATED
# ou
[WARN] Binance — simulated order (BINANCE_API_KEY/SECRET not set)
# ou
[WARN] MEXC Futures — order simulated (MEXC_FUTURES_API_KEY/SECRET not set)
```

---

## 6. Mettre à jour la page de statut

Dans `tradinebotte-status/generate_status.py`, ajouter le bot à `_LIVE_BOTS` :

```python
# Par défaut : tous les bots sont en SIM. Ajouter une entrée ici quand un bot passe en réel.
_LIVE_BOTS: set[tuple[str, str]] = {
    ("acct-2", "live_bot"),           # exemple : bot Polymarket acct-2 en mode réel
    ("acct-4", "accumulation_bot"),   # exemple : accumulation_bot acct-4 en mode réel
}
```

`acct_short` est le premier mot de l'entrée correspondante dans `_ACCOUNT_LABELS`
(ex. `"acct-2"` pour `"acct-2 [poly]"`). `bot_name` correspond exactement au
champ `bot_name` du heartbeat.

Régénérer après modification :

```bash
python3 tradinebotte-status/generate_status.py
```

---

## Référence rapide — détection de simulation par connecteur

| Connecteur | Simulé quand… | Avertissement dans le log |
|------------|---------------|--------------------------|
| `polymarket` | `POLY_PRIVATE_KEY` vide | `orders SIMULATED` |
| `binance` | `BINANCE_API_KEY` ou `BINANCE_API_SECRET` vide | `simulated order (BINANCE_API_KEY/SECRET not set)` |
| `mexc` | `MEXC_API_KEY` ou `MEXC_API_SECRET` vide | `order simulated (MEXC_API_KEY/SECRET not set)` |
| `mexc_futures` | `MEXC_FUTURES_API_KEY` ou `MEXC_FUTURES_API_SECRET` vide | `order simulated (MEXC_FUTURES_API_KEY/SECRET not set)` |

Toutes les vérifications de simulation se font au niveau du connecteur — le
moteur de stratégie et le système de heartbeat sont identiques dans les deux
modes.
