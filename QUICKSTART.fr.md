# tradinebotte — Démarrage rapide

> 🇬🇧 [English version](QUICKSTART.md) · Guide complet : [INSTALL.fr.md](INSTALL.fr.md) · CI : pylint 10/10 · mypy 0 erreur · 108 tests

## Avant de commencer

- Python 3.8+ sur Linux/Mac (VPS recommandé)
- Un wallet Polygon **EOA** (clé MetaMask — pas de Safe/Gnosis multisig)
- Sur ce wallet : **MATIC > 0,1** (frais de gas) et **USDC.e > 10 $** (`0x2791Bca1...`)
  - USDC natif (`0x3c499c...`) fonctionne aussi — `setup.py` effectue le swap automatiquement

---

## 1 — Cloner et installer

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh
```

Crée `~/tradinebotte/` avec un virtualenv et toutes les dépendances. Aucun accès root requis.

---

## 2 — Connecter le wallet (une seule fois)

```bash
python3 scripts/setup.py
```

La clé privée est demandée de façon interactive (masquée, jamais dans l'historique shell).  
Le script vérifie les balances, swape l'USDC si nécessaire, approuve l'exchange, et écrit
`~/tradinebotte/config.json` (chmod 600).

---

## 3 — Démarrer le bot

```bash
bash scripts/start_bot.sh
```

---

## 4 — Monitorer

```bash
bash scripts/monitor.sh          # tableau de bord en temps réel
tail -f ~/tradinebotte/live.log  # flux de logs bruts
```

---

## Tester sans argent réel

```bash
bash scripts/start_bot.sh --simulate
```

Tous les fichiers sont isolés dans `/tmp/tradinebotte-sim`. Aucun ordre n'est placé on-chain.

---

## Arrêter le bot

```bash
pkill -f live_bot.py
```
