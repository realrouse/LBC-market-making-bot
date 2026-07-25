# Passer en mode réel — de la simulation à l'argent réel

> 🇬🇧 [English version](going-live.md)

Tous les bots détectent les credentials au démarrage via des variables
d'environnement. En l'absence des variables concernées, le bot tourne en
**mode simulation** : les fonctions d'ordre renvoient des IDs `sim_...`,
aucun ordre réel n'est passé et aucun fond ne bouge. Passer en mode réel
se fait en trois étapes : définir les credentials sur le serveur distant →
les injecter dans le service systemd → basculer `is_live` pour ce bot dans
`inventory.toml`.

---

## 1. Credentials requis par bot

Chaque famille de stratégie tourne dans le même processus hôte,
`live_bot.py` (déploiement single-tree natif — voir
`docs/plan_D_decoupling.md`) ; le connecteur chargé dépend de la config
(`strategy_type` / `connector` dans le JSON de stratégie du bot), pas d'un
binaire séparé par stratégie.

| Famille de stratégie | Connecteur | Variables d'environnement |
|-----|------------|--------------------------|
| Polymarket (plugin `pm_strategy`) | `polymarket` | `POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET` |
| Grid / Swing / DCA (Binance) | `binance` | `BINANCE_API_KEY`, `BINANCE_API_SECRET` |
| Grid / Swing (MEXC spot) | `mexc` | `MEXC_API_KEY`, `MEXC_API_SECRET` |
| Grid (MEXC Futures) | `mexc_futures` | `MEXC_FUTURES_API_KEY`, `MEXC_FUTURES_API_SECRET` |
| Accumulation / BAMM (MEXC ou Binance) | `mexc` / `binance` | `MEXC_API_KEY`/`SECRET` ou `BINANCE_API_KEY`/`SECRET` |

**Note Polymarket :** `POLY_API_KEY`, `POLY_API_SECRET` et `POLY_API_PASSPHRASE`
sont dérivés de la clé privée du wallet. Exécuter `python3 scripts/setup.py`
une seule fois sur le compte pour les générer ; le script les écrit dans
`~/.polymarket_creds`, qu'on peut ensuite sourcer dans l'environnement.

---

## 2. Créer un fichier de credentials sur le compte distant

Se connecter en SSH sur le compte cible et créer `~/.tradinebotte-creds` :

```bash
cat > ~/.tradinebotte-creds << 'EOF'
# Binance — utilisé par live_bot.py pour grid/swing/DCA/accumulation sur le connecteur binance
BINANCE_API_KEY=votre_cle_ici
BINANCE_API_SECRET=votre_secret_ici

# MEXC Futures — utilisé par live_bot.py pour le grid sur le connecteur mexc_futures
# MEXC_FUTURES_API_KEY=votre_cle_ici
# MEXC_FUTURES_API_SECRET=votre_secret_ici

# Polymarket — utilisé par live_bot.py via le plugin pm_strategy
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
(`tradinebotte-accumulation.service`, `tradinebotte-grid.service`).

> **Pourquoi un drop-in ?** Le fichier de service de base est écrasé à chaque
> rsync de déploiement. Un drop-in dans `service.d/` n'est jamais touché par
> rsync et est fusionné automatiquement au rechargement.

---

## 4. Redéployer pour prendre en compte le changement

```bash
# Redéployer tous les comptes
bash tradinebotte-cex/scripts/deploy_all.sh

# Ou cibler un compte/bot directement, par ex. :
bash tradinebotte-cex/scripts/deploy_all.sh --only account-2
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

## 6. Basculer `is_live` dans inventory.toml

Le badge LIVE/SIM de la page de statut n'est **pas** modifié à la main dans
`generate_status.py` — il est dérivé de `inventory.toml`, la source de
vérité unique de la flotte (`live_bots()` dans
`tradinebotte-status/inventory_labels.py`, indexé sur le flag `is_live` de
chaque bot). Modifier un ensemble `_LIVE_BOTS` directement dans
`generate_status.py` n'a plus aucun effet : cet ensemble est recalculé à
partir d'`inventory.toml` à chaque génération.

`inventory.toml` lui-même est local et ignoré par git (il décrit vos
comptes/bots réels — voir `inventory.toml.example` si le vôtre n'existe
pas encore : `cp inventory.toml.example inventory.toml`). S'il est absent,
`generate_status.py` affiche un avertissement explicite et se dégrade sans
**aucun** badge LIVE — ne jamais supposer "pas d'avertissement" sans
vérifier que le fichier existe.

Trouver la ligne `[[bot]]` du bot concerné (identifiée par `account_idx` +
son `bot_name`/bot_id généré) et basculer :

```toml
is_live       = true   # ⚠ argent réel — laisser un commentaire : budget, connecteur, date d'armement
```

Régénérer, ou attendre le prochain tick de `statuspage.timer` (~2 min) :

```bash
python3 tradinebotte-status/generate_status.py
```

`is_live` est structurant au-delà de la page de statut : c'est aussi ce
flag qui fait que `botctl.sh` refuse les commandes destructives
(reset/wipe) sur ce bot. Voir la ligne BAMM idx7 dans `inventory.toml` pour
le format de commentaire attendu.

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
