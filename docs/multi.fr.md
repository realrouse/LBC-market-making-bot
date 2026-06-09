# Architecture multi-bot WebSocket

> 🇬🇧 [English version](multi.md)

Ce document décrit l'**Option B** : partager une seule connexion WebSocket entre
plusieurs bots de trading indépendants via ZeroMQ.

Pour le setup à compte unique, voir [QUICKSTART.fr.md — Option A](../QUICKSTART.fr.md).

## Quand utiliser l'Option B

| Situation | Option A (bot seul) | Option B (multi-bot) |
|---|---|---|
| Compte unique | ✅ plus simple | possible mais inutile |
| Deux wallets, même utilisateur Linux | fonctionne (deux processus, deux connexions WS) | ✅ une seule connexion WS |
| Deux wallets, utilisateurs Linux différents | fonctionne | ✅ une connexion WS, cross-user |
| Un compte, deux stratégies à comparer | deux bots autonomes | ✅ un feed, deux account bots |
| Priorité : simplicité et débogage facile | ✅ | plus de pièces mobiles |
| Priorité : connexions exchange minimales | plus de connexions | ✅ |

**Règle pratique :** commencer par l'Option A. Passer à l'Option B quand on a deux
comptes ou plus à faire tourner simultanément, ou quand on veut comparer des
stratégies sans ouvrir plusieurs connexions WebSocket.

---

## Pourquoi partager le WebSocket ?

L'endpoint WebSocket de Polymarket est une connexion persistante qui reçoit chaque
mise à jour du carnet d'ordres pour tous les marchés souscrits. Ouvrir une connexion
par compte crée trois problèmes :

1. **Bande passante redondante** — chaque message est reçu N fois au lieu d'une.
2. **Charge API dupliquée** — chaque connexion interroge l'API Gamma toutes les 30 s de façon indépendante.
3. **Risque de rate-limit** — plusieurs connexions depuis la même IP peuvent être bridées.

La solution : un seul processus (`feed.py`) maintient la connexion et
**diffuse** chaque événement à tous les bots de trading via ZeroMQ PUB/SUB.

---

## Architecture

```
WebSocket Polymarket
        │
        ▼
   ┌─────────┐
   │ feed.py │  ← processus unique, sans credentials, sans logique de trading
   └────┬────┘
        │ ZeroMQ PUB  IPC (/run/user/$UID/tradinebotte-feed.sock)
        │
   ┌────┴────────────────────────┐
   │                             │
   ▼                             ▼
┌──────────────────┐   ┌──────────────────┐
│  account_bot.py  │   │  account_bot.py  │   (N instances)
│  TRADINEBOTTE_   │   │  TRADINEBOTTE_   │
│  DIR=~/account-a │   │  DIR=~/account-b │
│                  │   │                  │
│  live.db         │   │  live.db         │   ← bases isolées
│  account.log     │   │  account.log     │   ← logs isolés
│  config.json     │   │  config.json     │   ← clés isolées
└──────────────────┘   └──────────────────┘
```

Chaque account bot exécute la **pile de trading complète de live_bot.py** —
évaluation du signal, passage d'ordres, résolution des trades — comme s'il était
un bot autonome, mais reçoit les données de marché depuis le feed partagé au lieu
de maintenir son propre WebSocket.

---

## Composants

### `tradinebotte-polymarket/feed.py`

| Responsabilité | Détails |
|---|---|
| Connexion WebSocket | Une seule connexion persistante vers Polymarket, reconnexion avec backoff exponentiel (1 s → 60 s) |
| Découverte des marchés | Interroge l'API Gamma toutes les 30 s en tâche de fond (même logique que le bot autonome) |
| Purge des marchés | Supprime les marchés expirés de l'état interne pour éviter une croissance illimitée |
| Éditeur ZMQ | Bind un socket PUB ; publie les messages `market`, `book` et `ping` |
| Credentials | **Aucun** — feed.py n'a pas de clé privée et ne passe aucun ordre |

Le feed ne stocke aucun état de trading. Il peut être redémarré à tout moment sans
affecter les account bots (ils rateront les mises à jour pendant l'interruption mais
ne placeront pas d'ordres en double à la reconnexion).

### `tradinebotte-polymarket/account_bot.py`

| Responsabilité | Détails |
|---|---|
| Souscripteur ZMQ | Connecte un socket SUB à l'adresse du feed ; souscrit à tous les messages |
| Enregistrement des marchés | Construit les paires `TokenState` depuis les messages `market` du feed |
| Évaluation du signal | Appelle `live_bot.handle_book_update()` → `check_signal()` sur chaque message `book`, avec **ses propres** paramètres de stratégie |
| Passage d'ordres | Appelle `live_bot.enter_live_trade()` → API CLOB Polymarket |
| Résolution des trades | WIN/LOSS/expiration résolus via `check_resolution()` comme dans le bot autonome |
| Persistance | Propre `live.db`, `account.log`, `config.json` sous `TRADINEBOTTE_DIR` |

L'account bot est un **processus OS séparé**. Au démarrage, il définit `TRADINEBOTTE_DIR`
puis importe `live_bot`, qui lit tous les paramètres de stratégie depuis
`strategies/polymarket/polymarket_BTC5M.json` de ce répertoire à l'import. Comme chaque processus
possède sa propre copie du module `live_bot`, **chaque account bot peut exécuter une
stratégie totalement différente** — seuil différent, mise différente, filtre horaire
différent, stop-loss différent — tout en partageant le même feed WebSocket brut.

Le feed est entièrement **agnostique du signal** : il diffuse chaque mise à jour
du carnet d'ordres brute sans aucun filtrage. L'évaluation du signal se fait
indépendamment dans chaque processus account bot.

---

## Protocole de messages

Le feed publie des messages JSON via ZeroMQ PUB. Les trois types sont des objets
JSON en trame unique.

### `market` — nouveau marché découvert

Envoyé une fois lors de la première registration d'un marché, et à nouveau lors de
la reconnexion du feed (les account bots traitent les doublons comme des no-ops).

```json
{
  "t": "market",
  "market_id":    "0xabc…",
  "question":     "Bitcoin Up or Down — 5 minutes (13:00 UTC)",
  "up_token_id":  "1234…",
  "dn_token_id":  "5678…",
  "start_ms":     1745664000000,
  "end_ms":       1745664300000
}
```

| Champ | Type | Description |
|---|---|---|
| `market_id` | string | Condition ID Polymarket |
| `question` | string | Titre du marché (tronqué à 80 caractères) |
| `up_token_id` | string | Token ID de l'issue UP/YES |
| `dn_token_id` | string | Token ID de l'issue DOWN/NO |
| `start_ms` | entier | Horodatage d'ouverture Unix (ms) |
| `end_ms` | entier | Horodatage de clôture Unix (ms) |

### `book` — mise à jour du carnet d'ordres

Émis à chaque événement WebSocket `book`, `price_change` ou `last_trade_price`.
C'est le message haute fréquence qui pilote l'évaluation du signal.

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
| `best_bid` | float | Meilleure offre d'achat (0–1) |
| `best_ask` | float | Meilleure offre de vente (0–1) |
| `spread` | float | `best_ask − best_bid` |
| `bid_vol` | float | Profondeur top-5 côté bid (USD) |
| `ask_vol` | float | Profondeur top-5 côté ask (USD) |
| `obi` | float | Déséquilibre du carnet : `(bid_vol − ask_vol) / (bid_vol + ask_vol)`, plage −1 à +1 |

### `ping` — keepalive

Envoyé toutes les 10 secondes. Les account bots l'ignorent ; utile pour surveiller
la santé du feed (absence de pings → crash du feed ou problème réseau).

```json
{"t": "ping", "ts": 1745664123456}
```

---

## Configuration

### Variables d'environnement

| Variable | Défaut | Portée | Description |
|---|---|---|---|
| `TRADINEBOTTE_FEED_ADDR` | IPC auto-détecté (`/run/user/$UID/tradinebotte-feed.sock`) | feed.py, account_bot.py | Adresse ZeroMQ PUB/SUB du feed partagé. Laisser vide pour IPC. Mettre `tcp://127.0.0.1:5557` pour forcer TCP (multi-stacks ou multi-serveurs). |
| `TRADINEBOTTE_INDICATORS_ADDR` | IPC auto-détecté (`/run/user/$UID/tradinebotte-indicators.sock`) | indicators.py, account_bot.py | Adresse ZMQ PUB du service indicators partagé. |
| `TRADINEBOTTE_INDICATORS_REG_ADDR` | IPC auto-détecté (`/run/user/$UID/tradinebotte-ind-reg.sock`) | indicators.py, account_bot.py | Adresse ZMQ REP pour l'enregistrement dynamique de flux. Chaque `account_bot` y envoie des requêtes subscribe au démarrage. |
| `TRADINEBOTTE_DIR` | `~/tradinebotte` | account_bot.py uniquement | Répertoire de données par compte (BD, log, config). |

Les trois adresses ZMQ utilisent par défaut des sockets IPC Unix dans `/run/user/$UID/`
(isolement kernel, mode 0700, géré par systemd-logind). Tous les services d'un même compte
partagent le même UID — aucune configuration d'adresse n'est nécessaire en déploiement
mono-serveur standard. Pour plusieurs stacks TCP indépendants, définir `TRADINEBOTTE_PORT_BASE`
avec des valeurs différentes par stack (ex. 5557 pour le stack A, 6557 pour le stack B).

### `config.json` par compte

Chaque répertoire de compte nécessite son propre `config.json` généré par `scripts/setup.py` :

```bash
TRADINEBOTTE_DIR=~/account-a python3 scripts/setup.py
TRADINEBOTTE_DIR=~/account-b python3 scripts/setup.py
```

Le config contient la clé privée, les credentials API, et les éventuelles surcharges
de stratégie. Les fichiers sont chmod 600 et ne sont jamais partagés entre comptes.

**Clés pertinentes pour le mode multi-bot :**

| Clé | Défaut | Description |
|---|---|---|
| `feed_addr` | IPC auto-détecté | Adresse ZMQ du feed partagé. Omettre pour IPC ; définir explicitement pour TCP. |
| `feed_auto_start` | `true` | Mettre à `false` quand le feed est géré par systemd — account_bot sonde avec des tentatives répétées au lieu de forker feed.py. |
| `indicators_reg_addr` | IPC auto-détecté | Adresse ZMQ REP du service indicators partagé. Omettre pour IPC ; définir explicitement pour TCP. |
| `indicators_streams` | `[]` | Liste d'abonnements aux flux à enregistrer auprès du service indicators au démarrage. Laisser vide pour ignorer les indicateurs. |

Exemple avec indicators activés :

```json
{
  "feed_auto_start":     false,
  "indicators_streams": [
    {
      "source":     "binance_ws",
      "asset":      "BTCUSDT",
      "timeframe":  "4h",
      "indicators": [{"type": "rsi", "period": 14},
                     {"type": "vol", "period": 20}]
    }
  ]
}
```

### Paramètres de stratégie — chaque account bot est indépendant

Les paramètres de stratégie (`strategies/polymarket/polymarket_BTC5M.json`) sont lus depuis
`TRADINEBOTTE_DIR/strategies/` par chaque account bot **au démarrage du processus**.
Comme chaque `account_bot.py` est un processus OS séparé avec sa propre copie du
module `live_bot`, chaque account bot évalue les signaux indépendamment selon **ses
propres** paramètres. Le feed diffuse les mises à jour brutes du carnet d'ordres
sans aucun filtrage — il n'a aucune connaissance d'un seuil ou d'une stratégie.

Il est donc possible de faire tourner des stratégies réellement différentes en
parallèle :

| Compte | `signal_threshold` | `stake` | `hour_filter` | Objectif |
|---|---|---|---|---|
| `~/account-conservateur` | `0.98` | `5 $` | Session US uniquement | Faible risque, moins d'entrées |
| `~/account-standard` | `0.96` | `10 $` | désactivé | Défaut backtesté |
| `~/account-agressif` | `0.94` | `20 $` | 24/7 | Fréquence plus élevée |

Chaque compte a besoin de son propre répertoire `strategies/` avec un fichier JSON
configuré pour cette stratégie :

```bash
# Configurer un compte conservateur avec un seuil personnalisé
mkdir -p ~/account-conservateur/strategies
cp strategies/polymarket/polymarket_BTC5M.json ~/account-conservateur/strategies/
# Éditer ~/account-conservateur/strategies/polymarket/polymarket_BTC5M.json :
#   "signal_threshold": 0.98, "stake": 5, "daily_stop_loss": 15
TRADINEBOTTE_DIR=~/account-conservateur python3 scripts/setup.py
```

Gardes du signal qui diffèrent par account bot (tous lus depuis le JSON de stratégie) :

| Paramètre | Clé JSON | Effet |
|---|---|---|
| Seuil d'entrée | `signal_threshold` | `best_bid` minimum pour entrer |
| Mise | `stake` | USD par trade |
| Stop-loss journalier | `daily_stop_loss` | Perte max avant suspension |
| Temps restant minimum | `min_secs_remaining` | Secondes minimum avant clôture du marché |
| Volume ask minimum | `min_ask_vol` | Liquidité minimum à l'entrée |
| Seuil de rejet OBI | `obi_reject_thresh` | Plancher de déséquilibre du carnet |
| Filtre heure / jour | `hour_filter` | Plages horaires UTC par jour semaine/weekend |

---

## Arborescence

### Même utilisateur Linux (le plus simple)

```
~/tradinebotte/               ← venv partagé + log du feed (sans credentials)
  venv/
  feed.log
  strategies/
    polymarket_BTC5M.json     ← stratégie partagée (ou lien symbolique par compte)

~/account-a/                  ← compte A : DB, log, config, credentials propres
  config.json                 (chmod 600)
  live.db
  account.log
  strategies/
    polymarket_BTC5M.json     ← stratégie spécifique optionnelle

~/account-b/                  ← compte B : DB, log, config, credentials propres
  config.json
  live.db
  account.log
```

### Utilisateurs Linux différents (cross-user)

Chaque utilisateur gère sa propre installation ; ils ne partagent que l'adresse
ZeroMQ. Voir [Déploiement cross-user](#déploiement-cross-user-comptes-linux-différents) ci-dessous.

```
/home/user1/tradinebotte/     ← utilisateur du feed : venv, feed.log (sans credentials)
/home/user1/account-1/        ← compte de trading de user1

/home/user2/tradinebotte/     ← venv propre de user2 (installation séparée)
/home/user2/account-2/        ← compte de trading de user2 (clé propre, DB propre)
```

---

## Séquence de lancement

### Lancement manuel (feed_auto_start=true, par défaut)

**account_bot.py démarre le feed automatiquement** — il n'est pas nécessaire de
lancer feed.py séparément.  Il suffit de démarrer tous les account bots en même
temps :

```bash
# Les trois peuvent être lancés simultanément — aucun ordre requis
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-c bash scripts/start_account.sh
```

**Fonctionnement (sans race condition) :**

1. Chaque account_bot sonde l'adresse du feed pendant 5 secondes au démarrage.
2. Si aucun feed n'est trouvé, il tente d'acquérir un verrou exclusif
   (`/tmp/tradinebotte-feed-<hash>.lock`).
3. Le gagnant démarre `feed.py` en sous-processus et attend jusqu'à 30 s qu'il
   soit prêt, puis libère le verrou.
4. Les autres bloquent sur le verrou, voient le feed actif au déblocage et
   procèdent sans démarrer un second feed.

Les logs du feed vont dans `/tmp/tradinebotte-feed-<hash>.log`.

Si vous préférez démarrer le feed explicitement (ex. pour systemd) :

```bash
# Optionnel : démarrage manuel du feed
bash scripts/start_feed.sh

# Les account bots sautent l'auto-démarrage et se connectent directement
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

Avec une adresse personnalisée :

```bash
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

### Lancement systemd (feed_auto_start=false, recommandé en production)

Quand le feed est géré par systemd, désactiver l'auto-démarrage dans chaque `config.json` :

```json
{ "feed_auto_start": false }
```

Lancer ensuite les account bots normalement — ils sondent le service feed déjà actif
au lieu de le forker :

```bash
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

Si le feed est inaccessible après 30 s (6 × 5 s), le bot quitte avec une erreur
et systemd le redémarre automatiquement.

---

## Monitoring

### Santé du feed

```bash
tail -f ~/tradinebotte/feed.log
```

Sortie attendue toutes les 10 s (boucle ping) et à chaque refresh de marchés :

```
[INFO]  Feed PUB bind sur ipc:///run/user/1000/tradinebotte-feed.sock
[INFO]  WebSocket connecte — diffusion sur ipc:///run/user/1000/tradinebotte-feed.sock
[INFO]  Marches BTC 5-min : 4
[INFO]  Nouveaux tokens : 8
```

Absence de lignes de log pendant > 60 s (deux cycles de refresh) indique un problème.

### Santé des account bots

```bash
tail -f ~/account-a/account.log
tail -f ~/account-b/account.log
```

La sortie normale reflète le bot autonome : gardes du signal, entrées de trades,
résolutions.

Si un bot affiche `Aucun message du feed depuis Xs`, il est connecté mais le feed
ne publie plus — vérifier `feed.log`.

### Requêtes SQLite (par compte)

```bash
sqlite3 ~/account-a/live.db "SELECT id, direction, outcome, pnl_net FROM trades ORDER BY id DESC LIMIT 5;"
sqlite3 ~/account-b/live.db "SELECT COUNT(*), ROUND(SUM(pnl_net),2) FROM trades WHERE resolved=1;"
```

---

## Modes de défaillance

### Crash du feed

Les account bots détectent le silence : si aucun message n'arrive pendant 60 s
(`FEED_TIMEOUT`), ils loggent un avertissement et continuent d'attendre. Ils ne
s'arrêtent pas.

Quand le feed redémarre, il republie les messages `market` pour tous les marchés
actifs. Les account bots les traitent comme des no-ops (les enregistrements
dupliqués sont idempotents). Les mises à jour du carnet reprennent immédiatement —
aucun redémarrage manuel nécessaire.

### Crash d'un account bot

Le feed n'est pas affecté. Seul le compte crashé rate les mises à jour pendant
l'interruption. Au redémarrage, `restore_state_from_db()` recharge les trades
ouverts depuis la base SQLite (même chemin de récupération après crash que le bot
autonome) et le bot reprend depuis l'état courant.

### Coupure réseau

Le feed gère la reconnexion WebSocket avec backoff exponentiel (cap 60 s). Les
account bots attendent silencieusement pendant cette période.

---

## Ajouter un troisième compte

```bash
# Configurer le nouveau répertoire de compte
TRADINEBOTTE_DIR=~/account-c python3 scripts/setup.py

# Lancer son bot (le feed tourne déjà)
TRADINEBOTTE_DIR=~/account-c bash scripts/start_account.sh
```

Aucun redémarrage du feed ni des account bots existants nécessaire.

---

## Déploiement cross-user (comptes Linux différents)

Le socket ZeroMQ PUB se bind sur une **adresse TCP loopback** (`127.0.0.1`).
Sur Linux, le TCP loopback est accessible à tout processus de la machine, quel que
soit l'utilisateur qui l'exécute — sans configuration particulière, sans système de
fichiers partagé, sans sudo. Le feed peut donc tourner sous `user1` et les account
bots sous `user2`, `user3`, etc.

### Prérequis par utilisateur

Chaque utilisateur Linux a besoin de sa propre installation indépendante :

```bash
# En tant que user2 — configuration initiale
git clone https://github.com/neofutur/tradinebotte.git ~/tradinebotte
bash ~/tradinebotte/scripts/install.sh          # crée ~/tradinebotte/venv avec pyzmq
TRADINEBOTTE_DIR=~/account-2 python3 ~/tradinebotte/scripts/setup.py   # config wallet
```

> Le `config.json` de chaque utilisateur (clé privée, credentials API) reste dans
> son propre répertoire personnel sous ses propres permissions Unix. Aucune credential
> n'est jamais partagée.

### Qui exécute le feed ?

Le feed n'a aucune credential et aucune logique de trading — n'importe quel
utilisateur peut le lancer. Choix typiques :

| Qui lance feed.py | Quand l'utiliser |
|---|---|
| Le même utilisateur qu'un des comptes (ex. `user1`) | 2–3 comptes, setup simple |
| Un compte système dédié (`tradinebotte-feed`) | Production ; séparation claire des responsabilités |

### Séquence de lancement (cross-user)

```bash
# En tant que user1 — lancer le feed partagé (IPC par défaut)
bash ~/tradinebotte/scripts/start_feed.sh

# En tant que user2 — lancer son account bot.
# Cross-user : l'IPC est par UID, donc user2 ne peut pas accéder au socket IPC de user1.
# Utiliser TCP quand feed et account bots tournent sous des OS users différents :
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557 \
TRADINEBOTTE_DIR=~/account-2 \
bash ~/tradinebotte/scripts/start_account.sh

# En tant que user3 — un autre account bot
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557 \
TRADINEBOTTE_DIR=~/account-3 \
bash ~/tradinebotte/scripts/start_account.sh
```

Chaque utilisateur utilise **son propre venv** (`~/tradinebotte/venv/`) via son
propre `start_account.sh`. En TCP (cross-user), `TRADINEBOTTE_FEED_ADDR` doit être
identique pour tous les utilisateurs. Quand tous les services tournent sous le même
OS user, l'IPC est utilisé automatiquement — aucune variable d'env n'est nécessaire.

### Arborescence (cross-user)

```
/home/user1/
  tradinebotte/
    venv/                     ← venv de user1 (pyzmq, aiohttp, etc.)
    feed.log                  ← diagnostics du feed
    strategies/
      polymarket_BTC5M.json
  account-1/
    config.json               (chmod 600, user1 uniquement)
    live.db
    account.log

/home/user2/
  tradinebotte/
    venv/                     ← venv propre de user2 (installation indépendante)
    strategies/
      polymarket_BTC5M.json
  account-2/
    config.json               (chmod 600, user2 uniquement)
    live.db
    account.log

/home/user3/
  tradinebotte/
    venv/
  account-3/
    config.json               (chmod 600, user3 uniquement)
    live.db
    account.log
```

### Monitoring cross-user

Chaque utilisateur surveille ses propres logs indépendamment :

```bash
# user1 surveille le feed + son propre compte
tail -f ~/tradinebotte/feed.log
tail -f ~/account-1/account.log

# user2 surveille uniquement son compte
tail -f ~/account-2/account.log
```

Vérifier que le feed est actif depuis n'importe quel utilisateur :

```bash
pgrep -u user1 -f feed.py && echo "feed actif" || echo "feed arrêté"
```

### Modèle de sécurité

| Ce qui est partagé | Qui peut y accéder | Risque |
|---|---|---|
| Messages ZMQ du feed (`market`, `book`, `ping`) | Tout utilisateur local connaissant le port | Données de marché uniquement — sans credentials ni clés |
| `config.json` | Propriétaire uniquement (chmod 600) | Jamais exposé |
| `live.db` | Propriétaire uniquement | Jamais exposé |
| `account.log` | Propriétaire uniquement | Jamais exposé |

Le feed ne stocke délibérément aucune credential. Tout utilisateur local pourrait se
connecter au port ZMQ et recevoir les données de marché, mais ces données sont
publiques (le carnet d'ordres de Polymarket est public) et ne contiennent rien de
sensible. Les clés privées ne quittent jamais le `TRADINEBOTTE_DIR` de chaque
utilisateur.

### Conflits de port (mode TCP multi-stacks)

Le mode IPC (par défaut) utilise des sockets filesystem et n'a pas de conflits de port.
Si vous utilisez TCP (ex. `TRADINEBOTTE_PORT_BASE` est défini) et que le port 5557 est
déjà occupé :

```bash
# Vérifier ce qui utilise le port
ss -tlnp | grep 5557

# Utiliser un base différent pour tous les participants
TRADINEBOTTE_PORT_BASE=6557 bash scripts/start_feed.sh
# — chaque account bot doit utiliser le même base
TRADINEBOTTE_PORT_BASE=6557 TRADINEBOTTE_DIR=~/account-2 bash scripts/start_account.sh
```

### Services systemd

Le projet fournit trois scripts générateurs dédiés :

| Script | Génère | Rôle |
|---|---|---|
| `scripts/install_feed_service.sh` | `tradinebotte-feed.service` | Feed WebSocket système (un par machine) |
| `scripts/install_indicators_service.sh` | `tradinebotte-indicators.service` | Pipeline indicators partagé (un par machine, optionnel) |
| `scripts/install_account_service.sh` | `tradinebotte-account-<nom>.service` | Bot de trading par compte (un par portefeuille) |

**Étape 1 — installer le service feed (une fois par machine) :**

```bash
bash scripts/install_feed_service.sh
# optionnel : adresse ZMQ non standard
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 bash scripts/install_feed_service.sh

# Suivre les commandes sudo affichées :
sudo cp /tmp/tradinebotte-feed.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tradinebotte-feed
sudo systemctl start tradinebotte-feed
```

**Étape 1b — installer le service indicators (optionnel, une fois par machine) :**

Le service indicators est un **processus partagé** — une seule instance tourne
sur la machine (comme le feed). Chaque `account_bot` enregistre les flux dont il
a besoin au démarrage via la socket REP ; le service démarre les tâches correspondantes
dynamiquement.

```bash
INDICATORS_CONFIG=~/tradinebotte/strategies/indicators/indicators_4h_bitcoin.json \
bash tradinebotte-indicators/scripts/install_indicators_service.sh

# Suivre les commandes sudo affichées :
sudo cp ~/tmp/tradinebotte-indicators.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tradinebotte-indicators
sudo systemctl start tradinebotte-indicators
journalctl -u tradinebotte-indicators -f
```

**Étape 2 — installer un service de compte (une fois par répertoire de portefeuille) :**

S'assurer que `config.json` contient `"feed_auto_start": false` avant cette étape
— le script émet un avertissement si cette clé est manquante.

```bash
# Chaque propriétaire de portefeuille exécute ceci pour son répertoire :
TRADINEBOTTE_DIR=~/account-a bash scripts/install_account_service.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/install_account_service.sh

# Suivre les commandes sudo affichées pour chacun :
sudo cp /tmp/tradinebotte-account-<nom_utilisateur>.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tradinebotte-account-<nom_utilisateur>
sudo systemctl start tradinebotte-account-<nom_utilisateur>
```

L'unité account déclare :
- `Requires=tradinebotte-feed.service` — systemd refuse de la démarrer si le feed n'est pas actif, et la redémarre automatiquement si le feed revient après un crash.
- `Wants=tradinebotte-indicators.service` — systemd démarre le service indicators en premier s'il est installé (optionnel ; l'account bot continue sans indicateurs si le service est absent).

**Cross-user** : le service feed tourne sous l'utilisateur qui a exécuté
`install_feed_service.sh`. Les services account tournent chacun sous le
propriétaire du portefeuille. Tous se connectent via `127.0.0.1` — aucun
droit Linux supplémentaire requis.

```bash
# Commandes de surveillance utiles :
sudo systemctl status tradinebotte-feed
sudo systemctl status tradinebotte-account-account-a
journalctl -u tradinebotte-feed -f
journalctl -u tradinebotte-account-account-a -f
```


## Comparaison avec le mode autonome

| Fonctionnalité | Autonome (`live_bot.py`) | Multi-bot (`feed.py` + `account_bot.py`) |
|---|---|---|
| Connexions WebSocket | 1 par compte | 1 au total |
| Polls API Gamma | 1 par compte toutes les 30 s | 1 au total toutes les 30 s |
| Base SQLite | `~/tradinebotte/live.db` | `~/account-X/live.db` par compte |
| Fichier log | `live.log` | `account.log` par compte, `feed.log` pour le feed |
| Config / clé | `~/tradinebotte/config.json` | `~/account-X/config.json` par compte |
| Fichier stratégie | `TRADINEBOTTE_DIR/strategies/` | `TRADINEBOTTE_DIR/strategies/` par compte |
| Reprise après crash | `restore_state_from_db()` au démarrage | identique, par compte |
| Logique du signal | `live_bot.check_signal()` | identique (importé depuis `live_bot`) |
| Service systemd | `tradinebotte.service` | unités séparées pour le feed + chaque compte |

---

## Tests

L'architecture multi-bot est couverte par `tests/test_multibot.py` (30 tests) :

```bash
bash scripts/run_tests.sh
```

| Classe | Tests | Ce qui est vérifié |
|---|---|---|
| `TestFeedRegisterMarket` | 7 | `feed.register_market()` : nouveaux tokens, doublons, expirés, champs manquants, marchés multiples, métadonnées |
| `TestAccountBotRegister` | 9 | `_register_from_market_msg()` : états token, directions, carte market_tokens, skips expirés/manquants, idempotence |
| `TestSingleBotIntegration` | 8 | Round-trip ZMQ avec un seul bot : enregistrement marché, mise à jour book, seuil signal, ping |
| `TestTwoBotIntegration` | 6 | Round-trip ZMQ avec deux bots simultanés : les deux reçoivent les mises à jour, DBs et capitaux isolés, idempotence marché dupliqué |

Tous les tests utilisent SQLite en mémoire et des sockets ZMQ TCP loopback — aucun
accès réseau ni credentials d'exchange requis.
