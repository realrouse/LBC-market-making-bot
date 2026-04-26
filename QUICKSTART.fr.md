# tradinebotte — Démarrage rapide

> 🇬🇧 [English version](QUICKSTART.md) · Guide complet : [INSTALL.fr.md](INSTALL.fr.md) · CI : pylint 10/10 · mypy 0 erreur · 153 tests

## Avant de commencer

- Python 3.8+ sur Linux/Mac (VPS recommandé)
- Un wallet Polygon **EOA** (clé MetaMask — pas de Safe/Gnosis multisig)
- Sur ce wallet : **MATIC > 0,1** (frais de gas) et **USDC.e > 10 $** (`0x2791Bca1...`)
  - USDC natif (`0x3c499c...`) fonctionne aussi — `setup.py` effectue le swap automatiquement

---

## Choisir le mode de déploiement

| Mode | Quand l'utiliser | Fichiers impliqués |
|---|---|---|
| **Option A — Bot seul** | Un seul compte, setup minimal | `live_bot.py` |
| **Option B — Multi-bot** | Deux comptes ou plus sur le même serveur | `feed.py` + `account_bot.py` |

Les deux modes partagent la même stratégie, le même schéma de base de données et la même logique de signal.

---

## Option A — Bot seul (un compte)

### 1 — Cloner et installer

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh
```

Crée `~/tradinebotte/` avec un virtualenv et toutes les dépendances. Aucun accès root requis.

### 2 — Connecter le wallet (une seule fois)

```bash
python3 scripts/setup.py
```

La clé privée est demandée de façon interactive (masquée, jamais dans l'historique shell).  
Le script vérifie les balances, swape l'USDC si nécessaire, approuve l'exchange, et écrit
`~/tradinebotte/config.json` (chmod 600).

### 3 — Démarrer le bot

```bash
bash scripts/start_bot.sh
```

### 4 — Monitorer

```bash
bash scripts/monitor.sh          # tableau de bord en temps réel
tail -f ~/tradinebotte/live.log  # flux de logs bruts
```

### Redémarrage automatique au reboot (systemd)

```bash
bash scripts/install_service.sh   # génère le fichier d'unité et affiche les commandes
```

Puis suivre les commandes `sudo` affichées pour activer le service.

### Arrêt

```bash
pkill -f live_bot.py              # si lancé manuellement
sudo systemctl stop tradinebotte  # si lancé via systemd
```

---

## Option B — Multi-bot (WebSocket partagé, plusieurs comptes)

Un seul processus `feed.py` ouvre une connexion WebSocket vers Polymarket et diffuse
chaque mise à jour du carnet d'ordres via ZeroMQ. Chaque `account_bot.py` souscrit à
ce feed et trade un compte indépendamment — avec sa propre base de données, son propre
fichier de log et sa propre clé privée. Aucune connexion exchange supplémentaire.

```
feed.py  →  ZMQ PUB (tcp://127.0.0.1:5557)
              ├── account_bot.py  [~/account-a]
              └── account_bot.py  [~/account-b]
```

### 1 — Cloner et installer (venv partagé)

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh           # crée ~/tradinebotte/venv
```

### 2 — Configurer chaque compte (une seule fois par compte)

```bash
TRADINEBOTTE_DIR=~/account-a python3 scripts/setup.py   # clé compte A
TRADINEBOTTE_DIR=~/account-b python3 scripts/setup.py   # clé compte B
```

Chaque compte obtient son propre `~/account-X/config.json` (chmod 600).

### 3 — Lancer le feed, puis chaque account bot

```bash
bash scripts/start_feed.sh                                    # feed partagé
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

### 4 — Monitorer chaque compte

```bash
tail -f ~/account-a/account.log
tail -f ~/account-b/account.log
tail -f ~/tradinebotte/feed.log   # diagnostics du feed
```

### Arrêt

```bash
pkill -f feed.py
pkill -f account_bot.py
```

Guide complet : [INSTALL.fr.md — section multi-bot](INSTALL.fr.md#partage-websocket-multi-bot-option-a--zeromq).

---

## Tester sans argent réel

Fonctionne pour les deux modes — ajouter `--simulate` (bot seul) ou utiliser un répertoire
sans clé privée réelle (multi-bot) :

```bash
# Bot seul en simulation
bash scripts/start_bot.sh --simulate        # écrit dans ~/tradinebotte-sim

# Multi-bot en simulation (chaque compte dans son propre répertoire)
TRADINEBOTTE_DIR=~/sim-a bash scripts/start_bot.sh --simulate
TRADINEBOTTE_DIR=~/sim-b bash scripts/start_bot.sh --simulate
```

Aucun ordre n'est placé on-chain dans les deux cas.
