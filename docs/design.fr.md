# Architecture multi-processus et flux de messages ZeroMQ

> 🇬🇧 [English version](design.md)

Ce document décrit l'architecture multi-processus de tradinebotte : chaque
processus, chaque socket ZeroMQ, chaque flux de données, et chaque service
systemd.

---

## 1. Diagramme d'architecture haute résolution

![Diagramme d'architecture tradinebotte à trois couches](architecture.png)

> Trois anneaux concentriques : **ENGINE** (status_collector · feed · indicators)
> au centre, **BOTS** (Polymarket Option A/B · CEX) sur l'anneau intermédiaire, et
> **DATA SOURCES** (APIs WebSocket à gauche, APIs REST/RPC à droite) sur l'anneau
> extérieur. Les rayons pointillés rouges représentent les heartbeats PUSH → `:5562`
> TCP. Généré par `docs/gen_architecture_diagram.py`.

Le diagramme ASCII ci-dessous représente la même topologie avec les adresses de
sockets explicites :

```
╔══════════════════════════════════════════════════════════════════════════════════════════════════╗
║                                  SERVICES EXTERNES                                              ║
║                                                                                                  ║
║  wss://ws-subscriptions-clob.polymarket.com/ws/market   [Polymarket WebSocket]                  ║
║  https://gamma-api.polymarket.com/markets               [Polymarket Gamma REST]                 ║
║  https://clob.polymarket.com                            [Polymarket CLOB REST]                  ║
║  wss://stream.binance.com   https://api.binance.com     [Binance WS + REST]                     ║
║  https://polygon.drpc.org                               [Polygon RPC]                           ║
╚══════╤══════════════════════════════════╤═══════════════╤════════════════════════════════════════╝
       │ connexion WS unique              │               │ Binance WS/REST (klines, depth, trades)
       ▼                                  │               ▼
┌──────────────────────────────┐          │    ┌───────────────────────────────────────────────┐
│         feed.py              │          │    │            indicators.py                      │
│  tradinebotte-polymarket/    │          │    │     tradinebotte-indicators/                  │
│  [tradinebotte-feed.service] │          │    │  [tradinebotte-indicators.service]            │
│                              │          │    │                                               │
│  PUB bind :5557              │◄─────────┘    │  SUB connect :5557   (événements feed)        │
│  IPC: tradinebotte-feed.sock │               │  REP bind :5561      (enregistrement dyn.)   │
└──────────────┬───────────────┘               │  PUB bind :5559      (indicateurs calculés)  │
               │                               │  IPC: tradinebotte-indicators.sock            │
               │  ZMQ PUB :5557                │       tradinebotte-ind-reg.sock               │
               │  IPC (même utilisateur)       └───────────────────┬───────────────────────────┘
               │  market/book/ping                                  │ ZMQ PUB :5559 IPC (même user)
       ┌───────┴──────────────────┐                                 │ flux d'indicateurs
       │                          │                                 │
       ▼                          ▼                        ┌────────┴──────────────┐
┌──────────────────────┐  ┌───────────────────┐           │                       │
│    account_bot.py    │  │    live_bot.py     │           ▼                       ▼
│ tradinebotte-polymar.│  │ tradinebotte-poly. │  ┌────────────────────┐  ┌────────────────────┐
│ [tradinebotte-       │  │ (comptes 2–5,      │  │  orderbook_bot.py  │  │ accumulation_bot.py│
│  account-<cpte>.svc] │  │  autonome)         │  │ tradinebotte-cex/  │  │ tradinebotte-cex/  │
│                      │  │                    │  │                    │  │                    │
│ SUB connect :5557    │  │ (WS direct)        │  │ SUB connect :5559  │  │ SUB connect :5559  │
│ REQ connect :5561    │  │                    │  │                    │  │                    │
│ PUSH → :5562 (HB)   │  │ PUSH → :5562 (HB)  │  │ PUSH → :5562 (HB)  │  │ PUSH → :5562 (HB)  │
│                      │  │                    │  │                    │  │                    │
│ live.db (SQLite WAL) │  │ live.db            │  │ live_ob.db         │  │ live_accum.db      │
└──────────────────────┘  └───────────────────┘  └────────────────────┘  └────────────────────┘
       │                          │                        │                        │
       │   PUSH :5562             │   PUSH :5562           │   PUSH :5562           │   PUSH :5562
       │   TCP loopback           │   TCP loopback         │   TCP loopback         │   TCP loopback
       │   (cross-user)           │   (cross-user)         │   (cross-user)         │   (cross-user)
       └──────────────────────────┴────────────────────────┴────────────────────────┘
                                                │
                        HEARTBEAT PUSH → tcp://127.0.0.1:5562
                        (chaque bot, toutes les 3600 s, déclenché au démarrage)
                                                │
                                                ▼
                              ┌─────────────────────────────────────────┐
                              │         status_collector.py             │
                              │     tradinebotte-status/                │
                              │  [tradinebotte-status.service]          │
                              │                                         │
                              │  PULL bind tcp://127.0.0.1:5562         │
                              │  heartbeat.db (SQLite — tous comptes)   │
                              └─────────────────────────────────────────┘

feed.py envoie aussi son propre heartbeat PUSH → :5562  ────────────────────────┘
indicators.py envoie aussi le sien ─────────────────────────────────────────────┘

─────────────────────────────────────────────────────────────────────────────────
 Légende transport
   IPC   ipc:///run/user/$UID/tradinebotte-<nom>.sock  (même utilisateur OS)
   TCP   tcp://127.0.0.1:<port>                         (cross-user, même hôte)
   TCP†  utiliser TRADINEBOTTE_PORT_BASE pour décaler tous les ports
─────────────────────────────────────────────────────────────────────────────────
```

---

## 2. Modes de déploiement

Deux modes de déploiement existent. Ils partagent le même code et les mêmes
fichiers de stratégie.

### Option A — Autonome (processus unique)

```
Polymarket WebSocket
        │
        ▼
  ┌───────────────┐
  │  live_bot.py  │  ← WebSocket + évaluation signal + ordres + BD
  └───────────────┘
```

Un seul processus fait tout : maintient la connexion WebSocket, évalue les
signaux, place les ordres, écrit la base de données. Utilisé pour les comptes
2–5 en production.

```bash
python3 tradinebotte-polymarket/live_bot.py
```

### Option B — Multi-bot (feed + N bots de compte)

```
Polymarket WebSocket
        │
        ▼
  ┌──────────┐
  │ feed.py  │  ← WebSocket uniquement, pas de clés, pas de trading
  └────┬─────┘
       │ ZeroMQ PUB  IPC (/run/user/$UID/tradinebotte-feed.sock)
       │
  ┌────┴──────────────────────┐
  ▼                           ▼
┌──────────────────┐  ┌──────────────────┐
│  account_bot.py  │  │  account_bot.py  │  (N instances, isolées)
│  ~/compte-a      │  │  ~/compte-b      │
└──────────────────┘  └──────────────────┘
```

Le feed détient la connexion WebSocket unique et diffuse tous les événements.
Chaque bot de compte exécute la pile de trading complète de façon indépendante
avec ses propres clés, sa propre base de données et ses propres paramètres de
stratégie.

```bash
python3 tradinebotte-polymarket/feed.py &
TRADINEBOTTE_DIR=~/compte-a python3 tradinebotte-polymarket/account_bot.py &
TRADINEBOTTE_DIR=~/compte-b python3 tradinebotte-polymarket/account_bot.py &
```

**Démarrage automatique du feed :** le premier `account_bot.py` à démarrer
lance `feed.py` automatiquement. Un verrou fichier POSIX
(`/tmp/tradinebotte-feed/feed-<hash>.lock`) garantit qu'un seul bot de compte
démarre le feed ; les autres attendent que le feed soit joignable avant de
se connecter.

---

## 3. Inventaire des processus

| Processus | Fichier | Rôle | Credentials | Socket ZMQ |
|---|---|---|---|---|
| `feed` | `tradinebotte-polymarket/feed.py` | Relais WebSocket diffusion seule | **Aucune** | PUB bind `:5557` |
| `account_bot` | `tradinebotte-polymarket/account_bot.py` | Logique de trading par compte | Clé privée requise | SUB connect `:5557`, REQ connect `:5561`, PUSH → `:5562` |
| `live_bot` | `tradinebotte-polymarket/live_bot.py` | Bot autonome : WebSocket + signal + ordres + BD | Clé privée requise | PUSH → `:5562` (heartbeat uniquement) |
| `indicators` | `tradinebotte-indicators/indicators.py` | Pipeline d'indicateurs techniques partagé | Aucune | SUB connect `:5557`, PUB bind `:5559`, REP bind `:5561`, PUSH → `:5562` |
| `orderbook_bot` | `tradinebotte-cex/orderbook_bot.py` | Scalping OBI Binance ; moteurs interchangeables (OBI, DCA, Swing, SwingHold) | Clé API Binance (optionnelle en mode paper) | SUB connect `:5559`, PUSH → `:5562` |
| `accumulation_bot` | `tradinebotte-cex/accumulation_bot.py` | Accumulation BTC spot long terme : achat initial + renforcement OBI + ladder de profits | Clé API Binance | SUB connect `:5559`, PUSH → `:5562` |
| `status_collector` | `tradinebotte-status/status_collector.py` | Collecteur de heartbeats — reçoit les heartbeats de tous les bots | Aucune | PULL bind `:5562` |

`orderbook_bot` et `accumulation_bot` sont des bots Binance dans le sous-service
`tradinebotte-cex`. Ils ne participent pas à la topologie ZeroMQ feed/account-bot
Polymarket, mais consomment tous deux le service `indicators` partagé (ZMQ SUB
sur `:5559`). Fichiers d'état : `live_ob.db` / `orderbook_bot.pid` /
`orderbook_bot.log` et `live_accum.db` / `accumulation_bot.pid` /
`accumulation_bot.log`. Configs de stratégie :
`tradinebotte-cex/strategies/scalping/orderbook_btc.json` et
`tradinebotte-cex/strategies/accumulation/btc_accumulation.json`.

---

## 4. Table des adresses ZMQ

| Constante | Port | Patron | Direction | Adresse par défaut (IPC) | Remplacement TCP |
|---|---|---|---|---|---|
| `PORT_FEED` | 5557 | PUB (bind) / SUB (connect) | feed.py → account_bot.py, indicators.py | `ipc:///run/user/$UID/tradinebotte-feed.sock` | `tcp://127.0.0.1:5557` |
| `PORT_FEED_ALT` | 5558 | PUB (bind) alternat | adresse alternative de feed.py | `ipc:///run/user/$UID/tradinebotte-feed-alt.sock` | `tcp://127.0.0.1:5558` |
| `PORT_INDICATORS` | 5559 | PUB (bind) / SUB (connect) | indicators.py → orderbook_bot.py, accumulation_bot.py | `ipc:///run/user/$UID/tradinebotte-indicators.sock` | `tcp://127.0.0.1:5559` |
| `PORT_IND_REG` | 5561 | REP (bind) / REQ (connect) | indicators.py ← account_bot.py (enregistrement) | `ipc:///run/user/$UID/tradinebotte-ind-reg.sock` | `tcp://127.0.0.1:5561` |
| `PORT_STATUS` | 5562 | PULL (bind) / PUSH (connect) | status_collector.py ← tous les bots (heartbeats) | `tcp://127.0.0.1:5562` | (toujours TCP — cross-user) |

**Règle de transport :** les ports 5557, 5558, 5559, 5561 utilisent IPC par
défaut quand tous les processus partagent le même utilisateur OS. Définir
`TRADINEBOTTE_PORT_BASE` pour basculer en TCP. Le port 5562 (heartbeat) utilise
toujours TCP loopback car il reçoit des bots tournant sous des utilisateurs OS
différents.

---

## 5. Topologie ZeroMQ (détaillée)

```
                ┌──────────────────────────────────────────────────────┐
                │               SYSTÈMES EXTERNES                     │
                │  Polymarket WebSocket (wss://ws-*.clob...)           │
                │  API REST Gamma (https://gamma-api.poly...)          │
                │  Binance kline WebSocket + API REST                  │
                └─────────────────────┬────────────────────────────────┘
                                      │ connexion WS unique
                                      ▼
                            ┌──────────────────┐
                            │    feed.py        │
                            │  PUB bind :5557   │
                            └─────────┬─────────┘
                                      │ diffuse : market / book / ping
                        ┌─────────────┼─────────────────┐
                        ▼             ▼                  ▼
             ┌──────────────┐  ┌──────────────┐  ┌────────────────────────┐
             │ account_bot  │  │ account_bot  │  │    indicators.py       │
             │ SUB :5557    │  │ SUB :5557    │  │    SUB :5557           │
             │ REQ :5561 ───┼──┼──────────────┼─▶│    REP bind :5561     │
             │ ~/compte-a   │  │ REQ :5561 ───┼─▶│    PUB bind :5559     │
             └──────────────┘  └──────────────┘  └───────────┬────────────┘
                                                              │ indicators
                                                              ▼
                                                   ┌─────────────────────┐
                                                   │   tout consumer     │
                                                   │   SUB :5559         │
                                                   └─────────────────────┘
```

**Flux d'enregistrement dynamique** — au démarrage, chaque `account_bot`
envoie un REQ à `:5561` décrivant les flux dont il a besoin. `indicators.py`
démarre la tâche asyncio correspondante si elle ne tourne pas déjà et répond
`{"status":"ok","stream_id":"..."}`. Toute la sortie est diffusée sur le
PUB `:5559` ; les consommateurs filtrent par `stream_id`.

```
démarrage account_bot :
  REQ → {"cmd":"subscribe","asset":"BTCUSDT","timeframe":"4h",
         "source":"binance_ws","indicators":[{"type":"rsi","period":14}]}
  REP ← {"status":"ok","stream_id":"btc_4h"}
  SUB → :5559  (filtre : stream_id == "btc_4h")
```

### Patrons de socket utilisés

| Patron | Direction | Utilisé par |
|---|---|---|
| `zmq.PUB` bind | 1 → N diffusion | `feed.py`, `indicators.py` |
| `zmq.SUB` connect | N → 1 réception | `account_bot.py`, `indicators.py` |
| `zmq.REP` bind | serveur requête/réponse | `indicators.py` |
| `zmq.REQ` connect | client requête/réponse | `account_bot.py` (au démarrage) |
| `zmq.PUSH` connect | émetteur heartbeat | tous les bots |
| `zmq.PULL` bind | récepteur heartbeat | `status_collector.py` |

Tous les messages sont des objets JSON mono-frame. ZeroMQ garantit la
livraison atomique de chaque frame — jamais de message partiel.

### Adresses par défaut

| Variable | Défaut | Bindé par | Connecté par |
|---|---|---|---|
| `TRADINEBOTTE_FEED_ADDR` | IPC détecté auto. (`/run/user/$UID/tradinebotte-feed.sock`) | `feed.py` | `account_bot.py`, `indicators.py` |
| `TRADINEBOTTE_INDICATORS_ADDR` | IPC détecté auto. (`/run/user/$UID/tradinebotte-indicators.sock`) | `indicators.py` PUB | tout consumer |
| `TRADINEBOTTE_INDICATORS_REG_ADDR` | IPC détecté auto. (`/run/user/$UID/tradinebotte-ind-reg.sock`) | `indicators.py` REP | `account_bot.py` (REQ au démarrage) |
| `TRADINEBOTTE_STATUS_ADDR` | `tcp://127.0.0.1:5562` | `status_collector.py` PULL | tous les bots (PUSH) |

Les trois adresses feed/indicators utilisent par défaut des sockets IPC Unix
dans `/run/user/$UID/` (mode 0700 imposé par systemd-logind — isolation par
UID au niveau noyau). Utiliser `TRADINEBOTTE_PORT_BASE` pour basculer en TCP
et faire tourner plusieurs stacks indépendants sur la même machine.

---

## 6. Topologie de production

Six comptes tournent sur le même serveur, chacun sous un utilisateur OS distinct.

| Compte | Bots en cours | Version déployée |
|---|---|---|
| acct-1 | feed + indicators + account_bot + status_collector | ef5d23e (bots), bdff296 (status) |
| acct-2 | live_bot (grille Polymarket) | bdff296 |
| acct-3 | live_bot (grille Polymarket) + accumulation_bot (Binance) | bdff296 |
| acct-4 | live_bot (Polymarket) + orderbook_bot + accumulation_bot (Binance) | bdff296 |
| acct-5 | live_bot (swing Polymarket) | bdff296 |
| acct-6 | indicators + feed + account_bot [test uniquement] | unknown |

**acct-1** exécute l'Option B : feed diffuse vers account_bot ; pipeline indicators partagé.
**acct-2 à acct-5** exécutent l'Option A : live_bot autonome par compte.
**acct-6** reproduit le stack Option B pour les tests ; hors rotation de production.

Tous les heartbeats de tous les comptes transitent vers `status_collector.py`
sur acct-1 via `tcp://127.0.0.1:5562`. acct-1 est le seul détenteur de
`heartbeat.db`.

---

## 7. Système de heartbeat

### heartbeat_loop (tradinetools/__init__.py)

Chaque bot exécute une tâche asyncio de fond (`heartbeat_loop`) au démarrage.

```python
async def heartbeat_loop(
    bot_name: str,
    install_dir: str | None,
    get_extra: Callable[[], dict[str, Any]],
    *,
    mode: str | None = None,
    interval: int = 120,
) -> None:
```

- **Déclenché immédiatement** au démarrage, puis toutes les `interval` secondes
  (défaut 120 s ; surchargé par `TRADINEBOTTE_HB_INTERVAL`).
- Construit un payload JSON via `build_heartbeat()` et l'envoie en une seule
  frame ZMQ PUSH.
- L'adresse est résolue depuis la variable `TRADINEBOTTE_STATUS_ADDR`, puis
  `default_status_addr()` → `tcp://127.0.0.1:5562`.
- Toutes les exceptions sont avalées (une panne du collecteur ne crashe jamais
  un bot).
- LINGER=0 garantit un arrêt propre même si le collecteur est inaccessible.

### Schéma du payload heartbeat

```json
{
  "ts":        1745664123,
  "bot_name":  "account_bot",
  "account":   "acct-1",
  "version":   "ef5d23e",
  "status":    "running",
  "bounds_ok": true
}
```

| Champ | Type | Description |
|---|---|---|
| `ts` | int | Horodatage Unix (secondes) à la construction du heartbeat |
| `bot_name` | string | Nom du processus bot (`feed`, `account_bot`, `live_bot`, etc.) |
| `account` | string | Depuis `TRADINEBOTTE_ACCOUNT`, puis `USER` ; identifie le compte OS |
| `version` | string | Hash git court depuis `version.stamp` ou `TRADINEBOTTE_VERSION` |
| `status` | string | Toujours `"running"` (futur : `"degraded"`, `"stopping"`) |
| `bounds_ok` | bool\|null | Optionnel ; défini par les bots qui suivent les bornes de paramètres |

### Schéma de heartbeat.db

```sql
CREATE TABLE heartbeats (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    account   TEXT    NOT NULL,
    bot_name  TEXT    NOT NULL,
    version   TEXT,
    status    TEXT,
    bounds_ok INTEGER,      -- 0/1/NULL
    payload   TEXT          -- blob JSON complet
);
```

Index : `(account, bot_name)` et `ts`.

### Transport : toujours TCP loopback

Les heartbeats utilisent toujours `tcp://127.0.0.1:5562`. Les bots tournant
sous des utilisateurs OS différents (acct-1 à acct-6) ne peuvent pas accéder
aux sockets IPC de `/run/user/$UID/` d'un autre compte. TCP loopback est le
seul transport permettant à tous les comptes d'atteindre le status_collector
unique sur acct-1.

### Outil de requête

```bash
# Depuis la machine opérateur — SSH sur acct-1 puis :
python3 tradinebotte-status/heartbeat_query.py

# Ou via le script wrapper (SSH + requête en une étape) :
bash tradinebotte-status/scripts/heartbeat_status.sh

# Rapport complet : heartbeats + états de services par compte :
bash tradinebotte-status/scripts/bot_status.sh
```

### Endpoint HTTP de santé (optionnel)

`heartbeat_loop` **pousse** l'état vers le collecteur. `health_server`
(`tradinetools/__init__.py`) en est le pendant en **pull** : un endpoint
`aiohttp` minimal permettant à un cron externe, un reverse proxy ou un moniteur
d'uptime de lire le même état en HTTP sans parler ZMQ. Il est monté comme
troisième tâche de fond à côté de `heartbeat_loop` et `control_loop` dans chaque
bot de trading (`live_bot`, `account_bot`, `accumulation_bot`, `orderbook_bot`).

```python
async def health_server(
    bot_name: str,
    install_dir: str | None,
    get_extra: Callable[[], dict[str, Any]],
    *,
    mode: str | None = None,
    host: str = "127.0.0.1",
    port: int | None = None,
) -> None:
```

- **Optionnel.** Désactivé tant que `TRADINEBOTTE_HEALTH_PORT` n'est pas défini ;
  sans cette variable la coroutine retourne immédiatement et ne bind rien —
  empreinte par défaut nulle, déploiements existants inchangés.
- **Pas de dérive.** On lui passe le *même* callback `get_extra` qu'à
  `heartbeat_loop` : la vue HTTP ne peut donc jamais diverger du heartbeat poussé.
- **Loopback uniquement.** Bind `127.0.0.1` par défaut et journalise un
  avertissement `SECURITY` s'il est pointé vers un hôte non-loopback — le payload
  contient capital/PnL et n'a pas d'authentification propre. À placer derrière un
  tunnel SSH ou un proxy authentifiant pour tout accès distant.
- **Tolérant aux pannes.** Les erreurs de setup/service sont journalisées et
  avalées ; une panne du serveur de santé ne crashe jamais le bot. La tâche est
  annulée à l'arrêt.

#### Activation

Définir le port dans l'unité systemd du bot (ou l'environnement) puis redémarrer :

```ini
# ~/.config/systemd/user/tradinebotte-live.service  → [Service]
Environment=TRADINEBOTTE_HEALTH_PORT=9101
```

```bash
curl -s http://127.0.0.1:9101/health | jq
```

#### Réponse

`GET /health` renvoie le payload de `build_heartbeat()` (cf. schéma ci-dessus)
plus un champ `uptime_s` — c.-à-d. les champs du heartbeat fusionnés avec les
stats `get_extra` du bot (capital, PnL, trades ouverts, …) :

```json
{
  "ts":           1745664123,
  "bot_name":     "live_bot",
  "account":      "acct-2",
  "version":      "10fa979",
  "status":       "running",
  "mode":         "sim",
  "capital":      1139.47,
  "daily_pnl":    25.10,
  "pnl_total":    65.31,
  "open_trades":  3,
  "uptime_s":     842
}
```

Les clés de stats exactes dépendent du bot (chacun fournit son propre
`get_extra`) ; l'enveloppe `ts`/`bot_name`/`account`/`version`/`status`/`uptime_s`
est toujours présente. En cas d'erreur interne, l'endpoint renvoie un HTTP 500
avec `{"status": "error", "error": "<détail>"}`.

> Choisir un port distinct par bot lorsque plusieurs tournent sous le même compte
> (p. ex. `live` 9101, `accumulation` 9102), exactement comme les ports ZMQ sont
> décalés par pile.

---

## 8. Services systemd (acct-1)

acct-1 exécute quatre services utilisateur (`systemctl --user`). Les services
utilisateur persistent entre les redémarrages grâce à `loginctl enable-linger`.

| Nom du service | Gère | RestartSec | StartLimitBurst |
|---|---|---|---|
| `tradinebotte-feed.service` | `feed.py` | 10 s | 10 |
| `tradinebotte-indicators.service` | `indicators.py` | 15 s | 5 |
| `tradinebotte-account-<compte>.service` | `account_bot.py` | 30 s | 5 |
| `tradinebotte-status.service` | `status_collector.py` | 15 s | — |

Tous les services utilisent `Restart=on-failure`. Le service account déclare
`Requires=tradinebotte-feed.service` et `After=tradinebotte-feed.service
tradinebotte-indicators.service` — systemd impose ainsi l'ordre de démarrage.

Le service status utilise `WantedBy=default.target` (portée utilisateur) ;
les autres aussi en mode utilisateur.

**Ordre de démarrage :**

```
1. tradinebotte-feed.service        → feed.py        bind :5557 (IPC)
2. tradinebotte-indicators.service  → indicators.py  SUB→:5557 / bind :5559 :5561 (IPC)
3. tradinebotte-account-<cpte>.svc  → account_bot.py SUB→:5557 REQ→:5561
4. tradinebotte-status.service      → status_collector.py  PULL bind :5562 (TCP)
```

**Autres comptes** (acct-2 à acct-5) exécutent `live_bot.py` directement via
un service utilisateur `tradinebotte-live.service` ou un service systemd simple,
selon le mode de déploiement.

---

## 9. Référence des API externes

| Service | Endpoint | Utilisé par |
|---|---|---|
| Polymarket WebSocket | `wss://ws-subscriptions-clob.polymarket.com/ws/market` | `feed.py`, `live_bot.py` |
| Polymarket Gamma REST | `https://gamma-api.polymarket.com/markets` | `feed.py`, `live_bot.py` |
| Polymarket CLOB REST | `https://clob.polymarket.com` | `account_bot.py`, `live_bot.py` |
| Binance WebSocket | `wss://stream.binance.com` (klines, depth, aggTrade) | `indicators.py` |
| Binance REST | `https://api.binance.com` | `indicators.py`, `accumulation_bot.py`, `orderbook_bot.py` |
| Binance Futures REST | `https://fapi.binance.com` | `indicators.py` (funding, OI, ratio L/S, liquidations) |
| Deribit REST | `https://www.deribit.com/api/v2/public/get_index_price` | `indicators.py` (DVOL) |
| API Fear & Greed | `https://api.alternative.me/fng/` | `indicators.py` |
| Polygon RPC | `https://polygon.drpc.org` | `account_bot.py`, `live_bot.py` |

---

## 10. Bases de données

| Fichier | Bot | Contenu |
|---|---|---|
| `live.db` | `account_bot.py`, `live_bot.py` | SQLite WAL — `trades` (21 cols) + `snapshots` (snapshots carnet 5 s) |
| `live_ob.db` | `orderbook_bot.py` | SQLite — trades et état de orderbook_bot |
| `live_accum.db` | `accumulation_bot.py` | SQLite — état de accumulation_bot, niveaux de renforcement, ladder de profits |
| `heartbeat.db` | `status_collector.py` (acct-1 uniquement) | SQLite — table `heartbeats`, tous comptes, tous bots |

Chaque `live.db` / `live_ob.db` / `live_accum.db` est privé au compte qui en
est propriétaire. `heartbeat.db` est partagé — il agrège les lignes de tous
les comptes.

---

## 11. Catalogue des messages

Tous les messages partagent un champ discriminant `"t"`.

### `market` — nouveau marché découvert

Publié par `feed.py` quand un marché entre dans la fenêtre ±6 minutes.
Re-publié aussi après chaque reconnexion WebSocket (les consommateurs traitent
les doublons comme des no-ops).

```json
{
  "t":           "market",
  "v":           1,
  "market_id":   "0xabc…",
  "question":    "Bitcoin Up or Down — 5 minutes (13:00 UTC)",
  "up_token_id": "1234…",
  "dn_token_id": "5678…",
  "start_ms":    1745664000000,
  "end_ms":      1745664300000
}
```

| Champ | Type | Description |
|---|---|---|
| `market_id` | string | ID de condition Polymarket |
| `question` | string | Titre du marché (≤80 caractères) |
| `up_token_id` | string | ID du token UP/YES |
| `dn_token_id` | string | ID du token DOWN/NO |
| `start_ms` | int | Horodatage d'ouverture du marché (Unix ms) |
| `end_ms` | int | Horodatage de fermeture du marché (Unix ms) |

Consommateurs : `account_bot.py` (enregistre le marché dans `BotState`).

---

### `book` — mise à jour du carnet d'ordres

Publié par `feed.py` à chaque événement WebSocket `book`, `price_change` ou
`last_trade_price`. Haute fréquence ; pilote l'évaluation des signaux.

```json
{
  "t":        "book",
  "v":        1,
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
| `best_bid` | float | Meilleur prix acheteur (0–1) |
| `best_ask` | float | Meilleur prix vendeur (0–1) |
| `spread` | float | `best_ask − best_bid` |
| `bid_vol` | float | Profondeur agrégée top-5 côté achat (USD) |
| `ask_vol` | float | Profondeur agrégée top-5 côté vente (USD) |
| `obi` | float | Déséquilibre du carnet : `(bid_vol − ask_vol) / (bid_vol + ask_vol)` ∈ [−1, +1] |

Consommateurs : `account_bot.py` (évaluation signal), `indicators.py`
(accumulation de prix).

---

### `ping` — keepalive

Publié par `feed.py` toutes les 10 secondes. L'absence de pings indique un
crash du feed ou un problème réseau.

```json
{"t": "ping", "v": 1, "ts": 1745664123456}
```

Consommateurs : ignoré par `account_bot.py` ; utile pour le monitoring externe.

---

### `indicators` — indicateurs techniques

Publié par `indicators.py` une fois que le buffer d'historique de prix par
token atteint `--min-ticks` (défaut 25) et que toutes les périodes
d'indicateurs sont satisfaites.

```json
{
  "t":        "indicators",
  "v":        1,
  "token_id": "1234…",
  "ts":       1745664125000,
  "rsi_14":   72.3,
  "sma_20":   0.9612,
  "ema_9":    0.9634,
  "vol_20":   0.0021
}
```

| Champ | Type | Description |
|---|---|---|
| `token_id` | string | Token auquel les indicateurs se rapportent |
| `ts` | int | Horodatage de publication (Unix ms) |
| `rsi_N` | float | RSI(N) — formule de Cutler (sum/n sur fenêtre fixe), 0–100 |
| `sma_N` | float | Moyenne mobile simple des N dernières valeurs `best_bid` |
| `ema_N` | float | Moyenne mobile exponentielle (k = 2/(N+1)), amorcée avec SMA |
| `vol_N` | float | Volatilité glissante : écart-type population des log-rendements sur N prix |

Le suffixe numérique encode la période configurée (ex. `rsi_14`, `sma_20`).
Aucun message n'est publié tant qu'au moins un indicateur n'est pas calculable.

**Stream Binance kline** — `source="binance_ws"`, les champs incluent `asset`,
`timeframe` et `stream_id` ; pas de `token_id`.

---

### `indicators` — taux de financement perpétuel Binance (`source="binance_funding"`)

Interrogé depuis `https://fapi.binance.com/fapi/v1/premiumIndex` toutes les
15 min (par défaut). Aucun calcul d'indicateur ; le taux brut est publié tel
quel.

```json
{
  "t":               "indicators",
  "v":               1,
  "stream_id":       "btc_funding",
  "funding_rate":    0.0001,
  "next_funding_ms": 1746000000000,
  "ts":              1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `stream_id` | string | Tel que déclaré dans la config (ex. `"btc_funding"`) |
| `funding_rate` | float | Taux de financement perpétuel Binance actuel (typiquement ±0,01 %) |
| `next_funding_ms` | int | Horodatage du prochain règlement de financement (Unix ms) |
| `ts` | int | Horodatage de publication (Unix ms) |

---

### `indicators` — volatilité implicite Deribit DVOL (`source="deribit_iv"`)

Interrogé depuis `https://www.deribit.com/api/v2/public/get_index_price` toutes
les 5 min (par défaut). Fournit l'indice de volatilité implicite annualisé BTC.

```json
{
  "t":         "indicators",
  "v":         1,
  "stream_id": "btc_dvol",
  "dvol":      62.5,
  "ts":        1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `stream_id` | string | Tel que déclaré dans la config (ex. `"btc_dvol"`) |
| `dvol` | float | Volatilité implicite annualisée Deribit DVOL (ex. 62,5 ≈ 62,5 %) |
| `ts` | int | Horodatage de publication (Unix ms) |

---

### `indicators` — Indice Fear & Greed (`source="fear_greed"`)

Interrogé depuis `https://api.alternative.me/fng/` toutes les 1 heure (par
défaut). L'indice va de 0 (Peur Extrême) à 100 (Avidité Extrême).

```json
{
  "t":                "indicators",
  "v":                1,
  "stream_id":        "fear_greed",
  "fear_greed":       72,
  "fear_greed_label": "Greed",
  "ts":               1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `stream_id` | string | Toujours `"fear_greed"` (indépendant de l'actif) |
| `fear_greed` | int | Valeur de l'indice 0–100 |
| `fear_greed_label` | string | Libellé : `"Extreme Fear"`, `"Fear"`, `"Neutral"`, `"Greed"`, `"Extreme Greed"` |
| `ts` | int | Horodatage de publication (Unix ms) |

Consommateurs : tout processus souscrivant au port PUB des indicateurs.

---

### `indicators` — open interest futures Binance (`source="binance_oi"`)

Interrogé depuis `https://fapi.binance.com/futures/data/openInterestHist` toutes
les 5 min (par défaut). Fournit l'OI absolu et la variation signée depuis le
poll précédent.

```json
{
  "t":             "indicators",
  "v":             1,
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
| `oi_btc` | float | Open interest en contrats BTC |
| `oi_usd` | float | Open interest en USD |
| `oi_change_btc` | float | Delta OI depuis le poll précédent (+ = nouveaux longs/shorts ouverts) |
| `oi_change_usd` | float | Delta OI en USD depuis le poll précédent |
| `ts` | int | Horodatage de publication (Unix ms) |

OI montant avec le prix = tendance ; OI qui chute = débouclage / risque de
retournement.

---

### `indicators` — ratio long/short Binance (`source="binance_ls_ratio"`)

Interrogé depuis `https://fapi.binance.com/futures/data/topLongShortAccountRatio`
toutes les 5 min (par défaut). Reflète le positionnement des comptes top-traders
(pas la taille des positions).

```json
{
  "t":                "indicators",
  "v":                1,
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
| `ts` | int | Horodatage de publication (Unix ms) |

Signal contrarian : ratio > 1,5 ou < 0,7 précède souvent un retournement.

---

### `indicators` — liquidations forcées Binance (`source="binance_liquidations"`)

Agrège `https://fapi.binance.com/fapi/v1/forceOrders` sur le dernier intervalle
de poll (5 min par défaut). Ordres `SELL` = positions longues liquidées ; ordres
`BUY` = positions courtes liquidées.

```json
{
  "t":             "indicators",
  "v":             1,
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
| `liq_long_usd` | float | Valeur USD totale des positions longues liquidées sur l'intervalle |
| `liq_short_usd` | float | Valeur USD totale des positions courtes liquidées sur l'intervalle |
| `liq_net_usd` | float | `liq_short_usd − liq_long_usd` (négatif = surtout des longs liquidés) |
| `liq_count` | int | Nombre d'ordres forcés sur l'intervalle |
| `ts` | int | Horodatage de publication (Unix ms) |

Consommateurs : tout processus souscrivant au port PUB des indicateurs.

---

### `indicators` — OBI et flux de trades Binance (`source="binance_scalping"`)

Piloté par le stream Binance combiné `depth20@100ms` + `aggTrade`. Publié
tous les `publish_every_n` événements de profondeur (défaut 10). Calcule en
temps réel l'OBI et le trade flow imbalance.

```json
{
  "t":                "indicators",
  "v":                1,
  "stream_id":        "btc_scalping_spot",
  "asset":            "BTCUSDT",
  "market":           "spot",
  "obi":              0.12,
  "obi_ema":          0.10,
  "obi_decel":        -0.003,
  "spread_bps":       1.8,
  "tfi":              0.23,
  "realized_vol_bps": 4.7,
  "ts":               1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `obi` | float | OBI brut : `(bid_vol − ask_vol) / (bid_vol + ask_vol)` ∈ [−1, +1] |
| `obi_ema` | float | OBI lissé par EMA (filtre anti-spoofing) |
| `obi_decel` | float | Première différence de `obi_ema` (signal d'accélération) |
| `spread_bps` | float | Spread bid/ask en points de base |
| `tfi` | float | Trade flow imbalance sur `tfi_window_s` : `(buy_vol − sell_vol) / total_vol` ∈ [−1, +1] |
| `realized_vol_bps` | float | Écart-type des log-rendements du prix mid en points de base |
| `ts` | int | Horodatage de publication (Unix ms) |

---

### `indicators` — Carnet d'ordres complet Binance (`source="binance_full_depth"`)

Maintient le carnet d'ordres spot Binance complet (jusqu'à 5 000 niveaux) via
snapshot REST + diffs WebSocket incrémentiels avec resynchronisation complète
en cas de gap. Publié tous les `publish_every_n` événements de profondeur
(défaut 10).

```json
{
  "t":                  "indicators",
  "v":                  1,
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
| `best_bid`, `best_ask`, `mid` | float | Prix top-of-book |
| `spread_bps` | float | Spread en points de base |
| `obi_N` | float | OBI à N niveaux pour chaque N dans `obi_levels_list` |
| `cum_bid_vol_Xpct` / `cum_ask_vol_Xpct` | float | Quantité cumulée dans X% du mid côté achat/vente |
| `wall_bid_price`, `wall_bid_qty` | float | Plus grand niveau acheteur unique dans `wall_range_pct` du mid |
| `wall_ask_price`, `wall_ask_qty` | float | Plus grand niveau vendeur unique dans `wall_range_pct` du mid |
| `book_levels_bid`, `book_levels_ask` | int | Nombre de niveaux de prix actuellement suivis |
| `ts` | int | Horodatage de publication (Unix ms) |

---

### `indicators` — Contexte de prix VWAP (`source="binance_vwap_context"`)

Interrogé toutes les heures (par défaut). Récupère les 24 dernières bougies
4h fermées depuis Binance REST, calcule le VWAP via le prix typique × volume,
puis récupère le prix spot courant pour dériver un score de dip.

```json
{
  "t":         "indicators",
  "v":         1,
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
| `dip_score` | float | `(vwap − price) / vwap`. Positif = sous le VWAP (dip) ; négatif = au-dessus |
| `dip_zone` | string | `"below_vwap"` ou `"above_vwap"` |
| `ts` | int | Horodatage de publication (Unix ms) |

---

### `indicators` — Profil de volume taker (`source="binance_volume_profile"`)

Interrogé toutes les heures (par défaut). Récupère les 288 dernières bougies
5m fermées, agrège le volume taker achat/vente dans des tranches de 500 $ et
identifie les 5 nœuds à volume élevé (HVN) principaux.

```json
{
  "t":               "indicators",
  "v":               1,
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
| `bucket_buy_vol` / `bucket_sell_vol` | float | Volume taker achat/vente dans la tranche courante |
| `bucket_net_vol` | float | `bucket_buy_vol − bucket_sell_vol` |
| `price_zone` | string | `"buy_hvn"` / `"sell_hvn"` / `"neutral"` |
| `zone_score` | float | `bucket_net_vol / total_bucket_vol` ∈ [−1, +1] |
| `hvn_buckets` | list[float] | Liste triée des `hvn_top_n` bornes inférieures des HVN |
| `ts` | int | Horodatage de publication (Unix ms) |

---

### `indicators` — OBI macro depuis les klines (`source="binance_macro_obi"`)

Interrogé toutes les minutes (par défaut). Récupère les 60 dernières bougies
1m fermées, calcule pour chaque bougie le déséquilibre de flux taker
`(taker_buy / total − 0,5) × 2`, et lisse la série par EMA.

```json
{
  "t":                   "indicators",
  "v":                   1,
  "stream_id":           "btc_macro_obi",
  "macro_obi":           0.18,
  "macro_obi_raw":       0.24,
  "macro_obi_direction": "bullish",
  "ts":                  1745664125000
}
```

| Champ | Type | Description |
|---|---|---|
| `macro_obi` | float | Déséquilibre de flux taker lissé par EMA ∈ [−1, +1] |
| `macro_obi_raw` | float | Valeur brute de la bougie 1m la plus récente |
| `macro_obi_direction` | string | `"bullish"` / `"neutral"` / `"bearish"` |
| `ts` | int | Horodatage de publication (Unix ms) |

Consommateurs : tout processus souscrivant au port PUB des indicateurs.

---

## 12. Mécanisme de démarrage automatique du feed

En mode multi-bot, la gestion manuelle du feed n'est pas nécessaire. Le
premier `account_bot.py` à démarrer lance `feed.py` automatiquement.

```
account_bot démarre
    │
    ├─── sonde l'adresse du feed (réception sous 5 s) ?
    │       OUI ──▶ connecte SUB, commence le trading
    │
    │       NON
    │        │
    │        ├─── essaie LOCK_EX | LOCK_NB sur le fichier verrou
    │        │       ÉCHEC (un autre bot a le verrou) ──▶ attend LOCK_SH ──▶ connecte SUB
    │        │
    │        │       SUCCÈS (on détient le verrou exclusif)
    │        │            │
    │        │            ├─── subprocess.Popen(feed.py, env=env_minimal)
    │        │            ├─── sonde jusqu'à ce que le feed réponde (30 s max)
    │        │            └─── libère le verrou ──▶ connecte SUB
    │        │
    └─────────────────────────────────────────────────────────
```

**Fichier verrou :** `/tmp/tradinebotte-feed/feed-<hash(addr)>.lock`
Le hash est dérivé de `TRADINEBOTTE_FEED_ADDR` : chaque stack indépendant
(port différent) obtient son propre verrou.

**Environnement minimal :** `feed.py` hérite uniquement de `PATH`, `HOME`,
`LANG`, `VIRTUAL_ENV`, `PYTHONPATH`, `LC_ALL`, `LC_CTYPE` et
`TRADINEBOTTE_FEED_ADDR`. La clé privée `POLY_PRIVATE_KEY` du parent n'est
jamais transmise au sous-processus feed.

### `feed_auto_start = false` — feed géré par systemd

Quand `feed.py` est géré par un processus externe (ex. `tradinebotte-feed.service`),
définir `"feed_auto_start": false` dans le `config.json` du compte. Le chemin
verrou/Popen est entièrement ignoré. `account_bot` sonde l'adresse du feed en
boucle de tentatives (6 × 5 s = 30 s max) et quitte avec une erreur si le feed
est inaccessible. C'est le mode recommandé pour les déploiements cross-user où
le feed tourne en service systemd sous un autre utilisateur.

```
feed_auto_start = false :

account_bot démarre
    │
    ├─── sonde l'adresse du feed (réception sous 5 s) ?
    │       OUI ──▶ connecte SUB, enregistre les indicateurs, commence le trading
    │
    │       NON (jusqu'à 6 tentatives)
    │        │
    │        ├─── toutes les tentatives épuisées ?
    │        │       OUI ──▶ log ERROR + sys.exit(1)
    │        │       NON ──▶ log WARNING + attente 5 s + nouvelle tentative
    │        │
    └─────────────────────────────────────────────────────────
```

---

## 13. Isolation des processus

Chaque instance `account_bot.py` est un **processus OS distinct** avec sa
propre copie du module `live_bot`. Cela garantit :

| Ressource | Isolée ? | Notes |
|---|---|---|
| Base SQLite (`live.db`) | Oui | `TRADINEBOTTE_DIR` distinct par compte |
| Fichier log (`account.log`) | Oui | `TRADINEBOTTE_DIR` distinct par compte |
| Clé privée | Oui | Lue depuis le `config.json` propre au compte |
| Paramètres de stratégie | Oui | Dossier `strategies/` distinct par compte |
| État du capital | Oui | `BotState` en mémoire, reconstruit depuis la BD locale |
| Compteur stop-loss journalier | Oui | Cache `state.daily_pnl` par processus |
| Déduplication de signal | Oui | Set `state.signalled` par processus |

Le feed est entièrement **agnostique au signal** : il publie chaque mise à
jour brute du carnet sans filtrage. L'évaluation du signal, le placement des
ordres et le suivi des trades se font indépendamment dans chaque bot de compte.

---

## 14. Pipeline d'indicateurs

`indicators.py` est un étage pipeline optionnel et sans état. Il ne trade pas.
En production, il tourne en tant que service systemd `tradinebotte-indicators`
avec `tradinebotte-indicators/strategies/indicators_all.json`, qui regroupe les
14 flux en un seul processus sur les ports 5559/5561.

Le pipeline comporte trois catégories de flux :

**1. WebSocket** (tâches asyncio événementielles) :
- `binance_ws` — WebSocket klines Binance ; publie à chaque bougie fermée. Alimente le buffer ring `PriceSeries` pour RSI/SMA/EMA/Vol et les indicateurs OHLCV (ATR, Bollinger, VWAP, vol_zscore, rolling_max).
- `binance_scalping` — Binance combiné `depth20@100ms` + `aggTrade` ; publie l'OBI, l'EMA-OBI, le TFI et la vol réalisée à 100 ms.
- `binance_full_depth` — Binance `depth@100ms` + snapshot REST ; maintient un carnet complet de 5 000 niveaux et publie l'OBI multi-profondeur, le volume cumulé et les niveaux murailles.

**2. REST-pollés** (boucles asyncio sleep) :
- `binance_funding` (15 min), `binance_oi` (5 min), `binance_ls_ratio` (5 min), `binance_liquidations` (5 min)
- `deribit_iv` (5 min), `fear_greed` (1 h)
- `binance_vwap_context` (1 h), `binance_volume_profile` (1 h), `binance_macro_obi` (1 min)

**3. Feed** (SUB à `feed.py`) :
- Source `feed` — ticks Polymarket `best_bid` par token. Alimente le buffer ring `PriceSeries` pour les indicateurs calculés. Doit être déclaré statiquement dans la config JSON (non disponible via l'enregistrement dynamique).

```
Ring-buffer par token (deque, maxlen=200)
    │
    │  push(best_bid) à chaque message "book"  [source feed]
    │  push(close, high, low, volume) à la bougie fermée  [source binance_ws]
    │
    ├── RSI(N)         Cutler : sum(gains)/n ÷ sum(losses)/n sur N deltas
    ├── SMA(N)         moyenne(prices[-N:])
    ├── EMA(N)         itératif : ema = prix*k + ema*(1−k),  k = 2/(N+1)
    ├── Vol(N)         écart-type des log-rendements sur N+1 derniers prix
    ├── ATR(N)         average true range  [binance_ws uniquement]
    ├── Bollinger(N)   SMA ± 2σ  [binance_ws uniquement]
    ├── VWAP(N)        moyenne pondérée par le volume  [binance_ws uniquement]
    ├── vol_zscore(N)  z-score du volume vs moyenne/écart-type glissants  [binance_ws uniquement]
    └── rolling_max(N) max(highs[-N:])  [binance_ws uniquement]
         │
         └── publie "indicators" quand tous les indicateurs configurés ont une valeur valide
```

Les indicateurs retournent `None` tant que le buffer n'a pas assez d'historique.
Aucun message n'est publié tant qu'un seul indicateur n'est pas calculable :
les consommateurs ne reçoivent jamais de données partielles.

**Partage multi-bots** — `indicators.py` exécute une tâche asyncio par flux. Toute la sortie est diffusée sur un seul socket PUB ; les consommateurs filtrent par `stream_id`.

**Enregistrement dynamique** — les flux peuvent être ajoutés à l'exécution sans redémarrer `indicators.py`. Chaque bot envoie un REQ au socket REP (`:5561`) avec ses besoins ; le serveur démarre la tâche si elle est nouvelle et répond avec le `stream_id` à écouter.

**Sources supportées pour l'enregistrement dynamique :** `"binance_ws"`,
`"binance_scalping"`, `"binance_full_depth"`, `"binance_funding"`,
`"deribit_iv"`, `"fear_greed"`, `"binance_oi"`, `"binance_ls_ratio"`,
`"binance_liquidations"`, `"binance_vwap_context"`, `"binance_volume_profile"`
et `"binance_macro_obi"`. Les flux feed-source (ticks Polymarket) doivent être
déclarés statiquement dans le fichier de config JSON.

**Démarrage du pipeline :**

```bash
# Production : service unifié (14 flux)
TRADINEBOTTE_INDICATORS_CONFIG=tradinebotte-indicators/strategies/indicators_all.json \
  bash tradinebotte-indicators/scripts/start_indicators.sh

# Ou démarrer le feed puis les indicateurs manuellement
python3 tradinebotte-polymarket/feed.py &
python3 tradinebotte-indicators/indicators.py &
# Un consommateur souscrit à :5559
```

---

## 15. Ordre de démarrage

Pour l'Option B avec indicateurs :

```
1. tradinebotte-polymarket/feed.py       bind  :5557   (service systemd, ou démarrage auto si feed_auto_start=true)
2. tradinebotte-indicators/indicators.py SUB→  :5557   / bind :5559 + :5561  (service systemd, optionnel)
3. tradinebotte-polymarket/account_bot   SUB→  :5557, REQ→ :5561 (enregistrement des flux au démarrage)
4. tradinebotte-cex/orderbook_bot        SUB→  :5559  (consommateur indicators)
5. tradinebotte-cex/accumulation_bot     SUB→  :5559  (consommateur indicators)
```

ZeroMQ PUB/SUB est **sans état de connexion côté publisher** : le socket PUB
continue de fonctionner qu'il y ait ou non des clients SUB connectés. Les
messages publiés avant qu'un SUB se connecte sont perdus (pas de buffering
côté publisher). Cela signifie que démarrer les indicateurs après le feed
n'entraîne aucune perte de données — les éventuels messages `market` manqués
sont re-publiés au prochain refresh de 30 secondes.

---

## 16. Récapitulatif des variables d'environnement

| Variable | Défaut | Portée | Description |
|---|---|---|---|
| `TRADINEBOTTE_PORT_BASE` | (non défini) | feed.py, account_bot.py, indicators.py | Quand défini, bascule tous les défauts d'adresse en TCP et décale les ports de `PORT_BASE − 5557`. Laisser non défini pour IPC (recommandé en mono-machine). |
| `TRADINEBOTTE_FEED_ADDR` | IPC détecté auto. | feed.py, account_bot.py, indicators.py | Adresse ZMQ exacte du socket PUB feed. Remplace la détection automatique. |
| `TRADINEBOTTE_INDICATORS_ADDR` | IPC détecté auto. | indicators.py, account_bot.py | Adresse ZMQ PUB du service indicators. `indicators.py` la bind ; `account_bot.py` s'y abonne quand `indicators_streams` est défini. |
| `TRADINEBOTTE_INDICATORS_REG_ADDR` | IPC détecté auto. | indicators.py, account_bot.py | Adresse ZMQ REP pour l'enregistrement dynamique de flux. |
| `TRADINEBOTTE_STATUS_ADDR` | `tcp://127.0.0.1:5562` | status_collector.py, tous les bots | Adresse PULL bind (collecteur) / PUSH connect (bots). Toujours TCP. |
| `TRADINEBOTTE_DIR` | `~/tradinebotte` | account_bot.py, live_bot.py | Répertoire de données par compte (BD, log, config, stratégies) |
| `TRADINEBOTTE_ACCOUNT` | (fallback `USER`) | tous les bots | Identifiant de compte écrit dans les payloads heartbeat |
| `TRADINEBOTTE_VERSION` | (fallback version.stamp) | tous les bots | Hash git écrit dans les payloads heartbeat ; défini par les scripts de déploiement |
| `TRADINEBOTTE_HEALTH_PORT` | (non défini) | tous les bots de trading | Si défini, expose un endpoint HTTP `GET /health` sur `127.0.0.1:<port>` renvoyant le payload heartbeat du bot + `uptime_s`. Non défini = pas de serveur HTTP (défaut). Loopback uniquement — ne pas exposer sans couche d'authentification en façade. |

### Faire tourner deux piles indépendantes sur la même machine

```bash
# Pile A — ports par défaut (5557, 5559, 5561 …)
TRADINEBOTTE_DIR=~/compte-a python3 tradinebotte-polymarket/account_bot.py &

# Pile B — tous les ports décalés de +1000
TRADINEBOTTE_PORT_BASE=6557 TRADINEBOTTE_DIR=~/compte-b python3 tradinebotte-polymarket/account_bot.py &
TRADINEBOTTE_PORT_BASE=6557 TRADINEBOTTE_INDICATORS_CONFIG=tradinebotte-indicators/strategies/indicators_4h_bitcoin.json \
  bash tradinebotte-indicators/scripts/start_indicators.sh &
```

---

## 17. ZeroMQ vs MQTT — analyse des compromis pour ce projet

ZeroMQ et MQTT implémentent tous deux du publish/subscribe, mais font des
choix architecturaux opposés. Cette section explique pourquoi ZeroMQ a été
retenu et quels sont les compromis concrets dans le contexte de tradinebotte.

### Différence clé : avec ou sans broker

MQTT est **basé sur un broker** : chaque message transite par un serveur
central (Mosquitto, EMQX, …). Publishers et subscribers ne communiquent jamais
directement — le broker sert d'intermédiaire, stocke les messages retenus et
applique les niveaux de QoS.

ZeroMQ est **sans broker** : le publisher bind un port TCP ; les subscribers
s'y connectent directement. Aucun processus intermédiaire n'existe.

```
Topologie MQTT              Topologie ZeroMQ (la nôtre)
──────────────────          ──────────────────────────────
feed.py                     feed.py
  │  publish                PUB bind :5557
  ▼                           │
[broker Mosquitto]            ├──▶ account_bot A (SUB connect)
  │  subscribe               ├──▶ account_bot B (SUB connect)
  ├──▶ account_bot A         └──▶ indicators.py (SUB connect)
  ├──▶ account_bot B
  └──▶ indicators.py
```

### Avantages de ZeroMQ dans notre cas

| Critère | Détail |
|---|---|
| **Aucun processus broker** | Aucun daemon supplémentaire à déployer, configurer, surveiller ou redémarrer. 3 points de défaillance en moins par serveur dédié. |
| **Latence** | TCP loopback sans saut broker : ~10–50 µs contre ~1 ms via un broker MQTT local. Critique pour les messages `book` qui pilotent l'évaluation du signal au seuil 0,96. |
| **Pas de données périmées** | ZeroMQ PUB/SUB ne retient aucun message. Un SUB qui se connecte tardivement manque les anciens messages — exactement ce que l'on veut : un `account_bot` redémarré ne doit pas recevoir des centaines de prix en file d'attente. |
| **Simplicité** | `pip install pyzmq` suffit ; aucun paquet broker, aucun fichier de config, aucune règle ACL. Une ligne pour bind, une pour connect. |
| **High-water mark (HWM)** | Si un subscriber est lent, ZeroMQ abandonne silencieusement les messages à la HWM. Pour un flux de données de marché, abandonner est le bon comportement : un prix périmé est pire qu'aucun prix. |
| **Déploiement localhost** | Tous les processus tournent sur le même serveur dédié. `tcp://127.0.0.1:*` ne nécessite ni auth, ni TLS, ni ACL réseau. |

### Inconvénients de ZeroMQ dans notre cas

| Critère | Détail |
|---|---|
| **Pas de persistance** | Un subscriber non connecté au moment de l'envoi ne recevra jamais le message. Ce n'est généralement pas un problème (`book` est continu ; `market` est re-publié toutes les 30 s). |
| **Pas de filtrage par topic côté broker** | ZeroMQ PUB envoie chaque message à tous les SUB connectés. Le filtrage se fait côté application (`if msg["stream_id"] != "btc_4h": continue`). Ce n'est pas un problème aujourd'hui (N ≤ 3 subscribers). |
| **Pas de monitoring intégré** | Les brokers MQTT exposent un arbre de topics `$SYS`. ZeroMQ n'a pas d'équivalent — le diagnostic nécessite une instrumentation applicative. |
| **Perte de messages à la reconnexion** | Après un redémarrage du feed, les SUB se reconnectent automatiquement mais perdent les messages publiés pendant le gap. Le refresh `market` toutes les 30 s atténue ce problème. |

### Pourquoi MQTT serait moins adapté ici

| Fonctionnalité MQTT | Notre situation |
|---|---|
| **Messages retenus** | On ne veut précisément *pas* qu'un `account_bot` fraîchement connecté reçoive le dernier prix en cache : il pourrait corrompre la phase de warmup `min_ticks`. |
| **QoS 1 / QoS 2** | Ajoute des aller-retours d'acquittement. Pour les prix en streaming, une livraison dupliquée est néfaste (les ticks comptés deux fois faussent l'historique des indicateurs). |
| **HA broker / clustering** | Notre architecture tourne sur un serveur dédié unique. La HA broker MQTT ajoute de la complexité sans aucun bénéfice. |
| **Orienté WAN / IoT** | MQTT a été conçu pour des appareils contraints sur des réseaux peu fiables. Nos pipes sont du TCP loopback. |

### Verdict

ZeroMQ est le bon choix pour ce projet : il supprime le broker du chemin
critique, élimine une classe entière de défaillances de déploiement, et fournit
la sémantique sans-persistance, basse-latence que requiert un flux de carnets
d'ordres haute fréquence. Le seul scénario où MQTT deviendrait intéressant
serait une distribution des subscribers sur plusieurs hôtes distincts et un
filtrage par topic au niveau du broker pour gérer la bande passante — un
scénario hors périmètre actuellement.

---

## 18. Fichiers liés

| Fichier | Rôle |
|---|---|
| `tradinebotte-polymarket/live_bot.py` | Bot autonome Polymarket (Option A) |
| `tradinebotte-polymarket/feed.py` | Diffuseur ZMQ (Option B) |
| `tradinebotte-polymarket/account_bot.py` | Abonné par compte + logique de trading |
| `tradinebotte-indicators/indicators.py` | Étage pipeline d'indicateurs techniques |
| `tradinebotte-cex/orderbook_bot.py` | Bot de scalping OBI Binance ; moteurs interchangeables (OBI, DCA, Swing, SwingHold) |
| `tradinebotte-cex/accumulation_bot.py` | Bot d'accumulation BTC ; consommateur ZMQ du service indicators |
| `tradinebotte-status/status_collector.py` | Collecteur de heartbeats ; service autonome sur acct-1 |
| `tradinetools/tradinetools/__init__.py` | Tâche asyncio `heartbeat_loop` ; partagée par tous les bots |
| `tradinetools/tradinetools/zmq.py` | Helpers ZMQ ; constantes de ports ; `default_status_addr()` |
| `tradinebotte-indicators/strategies/indicators_all.json` | Config indicators unifiée de production (14 flux) |
| `tradinebotte-cex/strategies/scalping/orderbook_btc.json` | Config de stratégie pour `orderbook_bot` |
| `tradinebotte-cex/strategies/accumulation/btc_accumulation.json` | Config de stratégie pour `accumulation_bot` |
| `tradinebotte-status/scripts/heartbeat_status.sh` | SSH sur acct-1, requête heartbeat.db |
| `tradinebotte-status/scripts/bot_status.sh` | Rapport complet : heartbeats + états de services par compte |
| `tradinebotte-status/heartbeat_query.py` | Requête heartbeat.db, affiche table BOUNDS/VERSION |
| `tradinebotte-cex/scripts/deploy_all.sh` | Déploiement séquentiel sur tous les comptes de production |
| `docs/multi.fr.md` | Guide de configuration Option B et stratégies par compte |
| `docs/GridTrading.fr.md` | Architecture de la stratégie grid et config JSON |
| `tradinebotte-polymarket/tests/test_multibot.py` | Tests d'intégration ZMQ pour feed + account_bot |
| `tradinebotte-indicators/tests/test_indicators.py` | Tests unitaires pour les maths d'indicateurs et PriceSeries |
