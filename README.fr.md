# tradinebotte

> 🇬🇧 [English version](README.md)

Bot de trading automatisé pour les marchés de prédiction [Polymarket](https://polymarket.com), ciblant les marchés Bitcoin Hausse/Baisse 5 minutes sur Polygon. Utilise une stratégie quantitative basée sur un signal (`best_bid >= 0.96`) backtestée à **98,3% de taux de victoire** sur 1663 trades (avril 2026).

## Stratégie

- Surveille les marchés "Bitcoin Up or Down — 5 minutes" dont `endDate` est dans une fenêtre de ±6 minutes
- Signal d'entrée : `best_bid >= 0.96` sur un token UP ou DOWN
- Exécute un ordre LIMIT BUY au `best_ask` via l'API CLOB de Polymarket
- Résolution : WIN si bid >= 0.99, LOSS si bid <= 0.01, ou à l'expiration du marché (bid >= 0.50 = WIN)
- Stop-loss journalier : 30 $ | Mise par trade : 10 $ | Frais : 2%

## Prérequis

- Python 3.8+
- Un wallet Polygon mainnet (EOA — **pas** Safe/Gnosis multisig)
- MATIC > 0.1 (frais de gas)
- USDC.e > 10 $ sur Polygon (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)
  - **Pas** USDC natif (`0x3c499c...`) — `setup.py` effectue le swap automatiquement

## Dépendances

Installées automatiquement par `scripts/install.sh` dans un virtualenv `/opt/polymarket-live/venv/` :

```
aiohttp
websockets
web3
py-clob-client
```

## Installation

```bash
bash scripts/install.sh
```

## Configuration

Exécuter `setup.py` une seule fois. Il va :
- Vérifier les balances USDC.e et USDC natif
- Effectuer le swap USDC natif → USDC.e via Uniswap V3 si nécessaire
- Approuver l'allowance CTF Exchange
- Dériver les credentials API Polymarket et les écrire dans `/opt/polymarket-live/config.json`

```bash
python3 scripts/setup.py
```

La clé privée est saisie de manière interactive (stdin masqué — non visible dans `ps aux` ni dans l'historique shell).

Le bot lit les credentials depuis `config.json` au démarrage (fallback sur les variables d'environnement `POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_PASSPHRASE` si le fichier est absent).

Voir `config.json.example` pour la structure attendue.

> **Ne jamais commiter `config.json`.** Il est listé dans `.gitignore`.

## Lancement

```bash
bash scripts/start_bot.sh
```

Vérifier que le bot tourne :

```bash
pgrep -fa live_bot.py
```

Une seule instance doit tourner à la fois. Le bot écrit ses logs dans `/opt/polymarket-live/live.log` et persiste tous les trades dans `/opt/polymarket-live/live.db` (SQLite).

## Monitoring

```bash
# Dashboard en temps réel
bash scripts/monitor.sh

# Suivre les logs
tail -f /opt/polymarket-live/live.log

# Trades récents
sqlite3 /opt/polymarket-live/live.db \
  "SELECT id, direction, entry_price, outcome, ROUND(pnl_net,3), capital_after \
   FROM trades ORDER BY id DESC LIMIT 10;"

# Stats du jour
sqlite3 /opt/polymarket-live/live.db \
  "SELECT COUNT(*), SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END), ROUND(SUM(pnl_net),2) \
   FROM trades WHERE resolved=1 AND created_at > (strftime('%s','now')-86400)*1000;"

# Confirmer les ordres réels on-chain (pas simulés)
grep "order=" /opt/polymarket-live/live.log | grep -v "order=sim" | tail -20
```

## Tester dans un environnement virtuel

Utiliser [uv](https://github.com/astral-sh/uv) pour créer un environnement de test isolé sans toucher au Python système ni au venv de production.

**Installer uv** (si pas déjà installé) :

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

**Créer le venv et installer les dépendances :**

```bash
uv venv .venv --python 3.13
uv pip install aiohttp websockets web3 py-clob-client --python .venv/bin/python3
```

**Vérification de la syntaxe :**

```bash
.venv/bin/python3 -m py_compile bot/live_bot.py && echo "SYNTAX OK"
```

**Vérification des imports** (s'assure que le code au niveau module s'exécute sans erreur) :

```bash
.venv/bin/python3 -c "
import sys; sys.path.insert(0, '.')
import bot.live_bot as b
print('CONFIG_PATH:', b.CONFIG_PATH)
print('PRIVATE_KEY set:', bool(b.PRIVATE_KEY))
print('SIGNAL_THRESHOLD:', b.SIGNAL_THRESHOLD)
"
```

**Lancer le bot pendant 20 secondes** (aucun credential requis — les ordres sont simulés) :

```bash
timeout 20 .venv/bin/python3 bot/live_bot.py
```

Puis inspecter les logs :

```bash
cat /opt/polymarket-live/live.log
```

La sortie attendue confirme que le bot démarre, se connecte à l'API Polymarket, trouve des marchés BTC 5-min actifs, souscrit au WebSocket et entre en mode simulation :

```
[INFO] LIVE BOT v3 — Threshold=0.96 Stake=$10 MinAskVol=10
[WARNING] POLY_PRIVATE_KEY non definie — ordres SIMULES
[INFO] DB initialisee : /opt/polymarket-live/live.db
[INFO] State : capital=$100.00 | 0 trades | WR=0.0%
[INFO] Marches BTC 5-min : 2
[INFO] Souscription 2 tokens...
[INFO] WebSocket connecte
```

Le répertoire `.venv/` est listé dans `.gitignore` et ne doit pas être commité.

## Notes

- Les timeouts WebSocket (~90s) en période calme sont **normaux** — le bot se reconnecte automatiquement
- Si `POLY_PRIVATE_KEY` n'est pas défini, les ordres sont simulés (aucune exécution on-chain)
- Les signaux peuvent être rares en période de faible volatilité BTC — c'est attendu
- Ne pas modifier `SIGNAL_THRESHOLD` (0.96) sans relancer le backtest complet

## Licence

Voir [LICENSE](LICENSE).
