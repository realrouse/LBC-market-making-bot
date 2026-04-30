# tradinebotte — Démarrage rapide

> 🇬🇧 [English version](QUICKSTART.md) · Guide complet : [INSTALL.fr.md](INSTALL.fr.md) · CI : pylint 10/10 · mypy 0 erreur · 153 tests

## Avant de commencer

- Python 3.8+ sur Linux/Mac (VPS recommandé)
- Un wallet Polygon **EOA** (clé MetaMask — pas de Safe/Gnosis multisig)
- Sur ce wallet : **MATIC > 0,1** (frais de gas) et **USDC.e > 10 $** (`0x2791Bca1...`)
  - USDC natif (`0x3c499c...`) fonctionne aussi — `setup.py` effectue le swap automatiquement

---

## Choisir le mode de déploiement

**Option A — Bot seul** (`live_bot.py`)
: Chaque bot ouvre sa propre connexion WebSocket vers Polymarket.
: **Utiliser quand :** un seul compte, ou un petit nombre de comptes où la simplicité et la facilité de débogage priment sur l'efficacité des connexions.

**Option B — Multi-bot** (`feed.py` + `account_bot.py`)
: Un seul feed WebSocket partagé ; chaque account bot souscrit via ZeroMQ.
: **Utiliser quand :** deux comptes ou plus sur la même machine, plusieurs utilisateurs Linux ayant chacun leur propre compte, ou exécution de stratégies différentes en parallèle (chaque bot évalue les signaux indépendamment avec ses propres paramètres).

Guide de décision rapide :

| Situation | Recommandé |
|---|---|
| Premier setup, compte unique | **Option A** |
| Deux wallets, même utilisateur Linux | **Option B** |
| Deux wallets, utilisateurs Linux différents (`/home/user1`, `/home/user2`) | **Option B** |
| Un seul compte mais deux stratégies à comparer simultanément | **Option B** |
| Priorité à la simplicité d'opération et de débogage | **Option A** |

Les deux modes partagent le même format JSON de stratégie, le même schéma de base de données, la même logique de signal et les mêmes outils de backtest. Passer de A à B ultérieurement ne nécessite aucune modification des données existantes.

---

## Option A — Bot seul (un compte)

### 1 — Cloner et installer

> **Prérequis administrateur serveur (une fois par machine, si nécessaire) :**
> `scripts/install.sh` détecte les paquets système manquants et affiche la commande
> exacte à exécuter en root. Si des paquets manquent, tu verras par exemple :
> ```
> sudo apt-get install -y python3-venv python3.12-venv
> ```
> Le numéro de version est détecté automatiquement. Exécute cette commande en root
> une seule fois, puis relance `install.sh`. Les utilisateurs individuels n'ont plus
> besoin du root ensuite.

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh
```

Crée `~/tradinebotte/` avec un virtualenv et toutes les dépendances. Aucun accès root requis.

### 2 — Configurer (une seule fois)

```bash
python3 scripts/setup.py
```

La clé privée est demandée de façon interactive (masquée, jamais dans l'historique shell).  
Le script vérifie les balances, swape l'USDC si nécessaire, approuve l'exchange, et écrit
`~/tradinebotte/config.json` (chmod 600).

> **Pas encore de wallet ?** Appuyer sur Entrée sans saisir de clé — `setup.py` crée
> un config de simulation et le bot tourne en ordres simulés (aucune transaction on-chain).

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

### 3 — Lancer tous les account bots simultanément

```bash
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

Inutile de démarrer le feed manuellement — le premier account bot à démarrer
lance `feed.py` automatiquement. Les autres attendent et se connectent dès qu'il
est prêt.

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

Documentation complète de l'architecture : [docs/multi.fr.md](docs/multi.fr.md) · Référence INSTALL : [INSTALL.fr.md — section multi-bot](INSTALL.fr.md#partage-websocket-multi-bot-option-a--zeromq).

### Test d'intégration

Pour vérifier automatiquement que le multi-bot fonctionne de bout en bout entre comptes Linux :

```bash
bash scripts/test_multibot_deploy.sh
```

Ce script nettoie les comptes de test configurés, réinstalle le bot, démarre tous les bots simultanément en mode `--verbose`, surveille pendant 3 minutes, vérifie qu'un seul feed tourne et que tous les bots reçoivent des book updates, puis arrête tout. Configurer le serveur de test via `~/.tradinebotte-test.conf` (copier `scripts/test_multibot.conf.example`). Voir [INSTALL.fr.md — Test d'intégration](INSTALL.fr.md#test-dintégration) pour les détails complets.

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
