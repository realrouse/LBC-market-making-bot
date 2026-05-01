# tradinebotte — Démarrage rapide

> 🇬🇧 [English version](QUICKSTART.md) · Guide complet : [INSTALL.fr.md](INSTALL.fr.md) · Mise à jour : [UPDATE.fr.md](UPDATE.fr.md)

## Prérequis

- Python 3.8+ sur Linux/Mac (VPS recommandé)
- Wallet Polygon EOA — MATIC > 0,1 (gas) et USDC.e > 10 $
- **Pas encore de wallet ?** Appuyer sur Entrée au prompt `setup.py` → mode simulation, aucun ordre réel

---

## Option A — Un compte (bot seul)

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh        # détecte automatiquement les paquets système manquants
python3 scripts/setup.py       # Entrée = mode simulation
bash scripts/start_bot.sh
tail -f ~/tradinebotte/live.log
```

Monitoring : `bash scripts/monitor.sh`  
Redémarrage automatique au reboot : `bash scripts/install_service.sh` (puis suivre les commandes `sudo` affichées)

**Arrêt :** `pkill -f live_bot.py` · ou `sudo systemctl stop tradinebotte` si systemd

---

## Option B — Plusieurs comptes (WebSocket partagé)

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh
TRADINEBOTTE_DIR=~/account-a python3 scripts/setup.py   # clé du compte A
TRADINEBOTTE_DIR=~/account-b python3 scripts/setup.py   # clé du compte B
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

Le feed (`feed.py`) démarre automatiquement — aucune étape manuelle nécessaire.

**Arrêt :** `pkill -f feed.py; pkill -f account_bot.py`

Architecture complète : [docs/multi.fr.md](docs/multi.fr.md)

---

## Quelle option choisir ?

| Situation | Option |
|---|---|
| Compte unique | **Option A** |
| Plusieurs wallets ou utilisateurs Linux | **Option B** |
| Comparer des stratégies en parallèle | **Option B** |
| Setup le plus simple possible | **Option A** |
