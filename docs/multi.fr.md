# Architecture multi-bot WebSocket

> 🇬🇧 [English version](multi.md)

Ce document décrit l'**Option B** : faire tourner plusieurs comptes de trading
indépendants sur le même serveur en partageant une seule connexion WebSocket vers
Polymarket.

Pour le setup à compte unique, voir [QUICKSTART.fr.md — Option A](../QUICKSTART.fr.md).

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
        │ ZeroMQ PUB  tcp://127.0.0.1:5557
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

### `bot/feed.py`

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

### `bot/account_bot.py`

| Responsabilité | Détails |
|---|---|
| Souscripteur ZMQ | Connecte un socket SUB à l'adresse du feed ; souscrit à tous les messages |
| Enregistrement des marchés | Construit les paires `TokenState` depuis les messages `market` du feed |
| Évaluation du signal | Appelle `live_bot.handle_book_update()` → `check_signal()` sur chaque message `book` |
| Passage d'ordres | Appelle `live_bot.enter_live_trade()` → API CLOB Polymarket |
| Résolution des trades | WIN/LOSS/expiration résolus via `check_resolution()` comme dans le bot autonome |
| Persistance | Propre `live.db`, `account.log`, `config.json` sous `TRADINEBOTTE_DIR` |

L'account bot importe `live_bot` pour l'intégralité de sa pipeline de trading. Tous
les paramètres de stratégie, gardes du signal et calculs de frais sont identiques au
mode autonome.

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
| `TRADINEBOTTE_FEED_ADDR` | `tcp://127.0.0.1:5557` | feed.py + account_bot.py | Adresse ZeroMQ bind/connect |
| `TRADINEBOTTE_DIR` | `~/tradinebotte` | account_bot.py uniquement | Répertoire de données par compte (DB, log, config) |

Les deux variables doivent être définies de façon cohérente entre le feed et tous
les account bots qui s'y connectent.

### `config.json` par compte

Chaque répertoire de compte nécessite son propre `config.json` généré par `scripts/setup.py` :

```bash
TRADINEBOTTE_DIR=~/account-a python3 scripts/setup.py
TRADINEBOTTE_DIR=~/account-b python3 scripts/setup.py
```

Le config contient la clé privée, les credentials API, et les éventuelles surcharges
de stratégie. Les fichiers sont chmod 600 et ne sont jamais partagés entre comptes.

### Paramètres de stratégie

Les paramètres de stratégie (`strategies/polymarket_BTC5M.json`) sont lus depuis
`TRADINEBOTTE_DIR/strategies/` par chaque account bot indépendamment. Chaque compte
peut donc utiliser un seuil, une mise ou un filtre horaire différents.

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

L'ordre est important : le feed doit binder son socket PUB avant que les account
bots se connectent.

```bash
# Étape 1 — lancer le feed partagé
bash scripts/start_feed.sh

# Étape 2 — lancer chaque account bot (terminaux séparés ou nohup)
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

Les scripts affichent le PID et les premières lignes de chaque log. Un délai de
démarrage de 2 secondes est intégré pour détecter les crashs immédiats avant de
signaler le succès.

Avec une adresse personnalisée (port différent) :

```bash
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 bash scripts/start_feed.sh
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
```

---

## Monitoring

### Santé du feed

```bash
tail -f ~/tradinebotte/feed.log
```

Sortie attendue toutes les 10 s (boucle ping) et à chaque refresh de marchés :

```
[INFO]  Feed PUB bind sur tcp://127.0.0.1:5557
[INFO]  WebSocket connecte — diffusion sur tcp://127.0.0.1:5557
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
# En tant que user1 — lancer le feed partagé (bind tcp://127.0.0.1:5557)
bash ~/tradinebotte/scripts/start_feed.sh

# En tant que user2 — lancer son account bot (se connecte à la même adresse)
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557 \
TRADINEBOTTE_DIR=~/account-2 \
bash ~/tradinebotte/scripts/start_account.sh

# En tant que user3 — un autre account bot
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557 \
TRADINEBOTTE_DIR=~/account-3 \
bash ~/tradinebotte/scripts/start_account.sh
```

Chaque utilisateur utilise **son propre venv** (`~/tradinebotte/venv/`) via son
propre `start_account.sh`. Le `TRADINEBOTTE_FEED_ADDR` doit être identique pour
tous les utilisateurs.

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

### Conflits de port

Si le port 5557 est déjà utilisé sur la machine :

```bash
# Vérifier ce qui utilise le port
ss -tlnp | grep 5557

# Utiliser un port différent pour tous les participants
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 bash scripts/start_feed.sh
# — chaque account bot doit utiliser la même adresse
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 TRADINEBOTTE_DIR=~/account-2 bash scripts/start_account.sh
```

### Unités systemd (cross-user)

Chaque utilisateur peut installer sa propre unité systemd utilisateur de façon
indépendante :

```bash
# En tant que user1 — unité feed
bash ~/tradinebotte/scripts/install_service.sh   # suivre les commandes sudo affichées

# En tant que user2 — unité account bot (adapter install_service.sh ou écrire manuellement)
# Cible : ExecStart=.../account_bot.py avec TRADINEBOTTE_DIR et TRADINEBOTTE_FEED_ADDR définis
```

Une unité de service feed dédiée peut être installée au niveau système
(`/etc/systemd/system/tradinebotte-feed.service`) par un administrateur pour garantir
qu'elle démarre avant les unités account bot de chaque utilisateur.


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
