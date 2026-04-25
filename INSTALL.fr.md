# tradinebotte — Guide d'installation

> 🇬🇧 [English version](INSTALL.md)


## Prérequis

- Python 3.8+
- Un wallet Polygon mainnet (EOA — PAS Safe/Gnosis multisig)
- MATIC > 0.1 (frais de gas)
- USDC.e > 10 $ sur Polygon (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)
  Note : PAS USDC natif (`0x3c499c...`) — setup.py effectue le swap automatiquement
- CLI `sqlite3` (optionnel mais recommandé pour les requêtes de monitoring)
  Le bot utilise le module Python sqlite3 intégré et fonctionne sans le CLI.
  Installation Debian/Ubuntu : `sudo apt install sqlite3`
  Sans sudo, utiliser l'alternative Python :
  ```bash
  ~/polymarket/venv/bin/python3 -c \
    "import sqlite3; c=sqlite3.connect('live.db'); \
     print(c.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0])"
  ```


## Dépendances

Les packages Python suivants sont installés automatiquement par `scripts/install.sh`
dans un virtualenv situé dans `~/polymarket/venv/` :

- `aiohttp`
- `websockets`
- `web3`
- `py-clob-client`


## Répertoire d'installation

Tous les scripts lisent la variable d'environnement `POLYMARKET_DIR` pour
déterminer où installer et exécuter le bot. Si elle n'est pas définie,
la valeur par défaut est :

```
~/polymarket
```

Aucun accès root requis — le chemin par défaut est dans le répertoire personnel de l'utilisateur.

Exemples :

```bash
# Par défaut (installe dans ~/polymarket, sans root)
bash scripts/install.sh

# Chemin personnalisé en argument
bash scripts/install.sh ~/polymarket

# Chemin personnalisé via variable d'environnement
POLYMARKET_DIR=~/polymarket bash scripts/install.sh
```

La même variable doit être définie de manière cohérente pour le setup,
le lancement et le monitoring :

```bash
export POLYMARKET_DIR=~/polymarket
```


## Installation

Exécuter le script d'installation depuis la racine du dépôt :

```bash
bash scripts/install.sh [répertoire_installation] [--with-tests]
```

**Options :**
- `--with-tests` — Copie aussi `tests/`, `scripts/backtest.py` et
  `data/backtest_sample_btc5m_range_2026.db`, puis lance
  la suite complète de tests (99 tests) juste après l'installation.
  Le backtest utilise `live.db` uniquement s'il contient ≥ 100 snapshots ;
  sinon il bascule automatiquement sur le dataset embarqué.

Ce script va :
- Installer les packages système (python3, pip, venv, sqlite3)
- Créer le répertoire d'installation
- Copier `bot/live_bot.py` et `bot/api_polymarket.py` vers `<POLYMARKET_DIR>/`
- Copier `strategies/*.json` vers `<POLYMARKET_DIR>/strategies/`
- Créer un virtualenv dans `<POLYMARKET_DIR>/venv/`
- Installer les dépendances Python dans le virtualenv
- Générer `<POLYMARKET_DIR>/run.sh` (wrapper avec `POLYMARKET_DIR` pré-défini)
- Vérifier la syntaxe du bot


## Configuration du wallet (une seule fois)

Exécuter `setup.py` une seule fois avec votre wallet Polygon. Il va :
- Demander la clé privée de manière interactive (stdin masqué)
- Vérifier les balances USDC.e et USDC natif
- Effectuer le swap USDC natif → USDC.e via Uniswap V3 si nécessaire
- Approuver l'allowance CTF Exchange
- Dériver les credentials API Polymarket
- Écrire les credentials dans `<POLYMARKET_DIR>/config.json` (chmod 600)

```bash
POLYMARKET_DIR=~/polymarket python3 scripts/setup.py
```

La clé privée est saisie de manière interactive et n'est jamais visible
dans `ps aux` ni dans l'historique shell.

Le bot lit les credentials depuis `config.json` au démarrage. En l'absence
du fichier, il utilise les variables d'environnement en fallback :
`POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_PASSPHRASE`

Voir `config.json.example` pour la structure attendue.

> **ATTENTION :** Ne jamais commiter `config.json`. Il est listé dans `.gitignore`.


## Page de statut web (optionnel)

Le bot peut publier une page HTML statique consultable dans n'importe
quel navigateur. L'activer en ajoutant ces clés dans `config.json` :

```json
"webstatuspage_html": true,
"webstatuspage_path": "~/public_html/tradinebot_status.html",
"webstatus_user":     "tradinebot",
"webstatus_password": "votre_mot_de_passe"
```

La page affiche le capital, le PnL total et journalier, le taux de
victoire, les positions ouvertes et les 10 derniers trades résolus.
Elle se recharge automatiquement toutes les 60 s.
Un aperçu statique de la page rendue est disponible dans [docs/status_example.html](docs/status_example.html).

Le bot crée le répertoire HTML automatiquement et y écrit un `.htaccess`.
Le fichier `.htpasswd` est stocké dans `POLYMARKET_DIR/.webstatus_htpasswd`
(hors de la racine web).

### Prérequis — Apache

1. Activer les modules nécessaires (Debian/Ubuntu) :

   ```bash
   sudo a2enmod userdir       # sert ~/public_html — ignorer si le
                              #   chemin est déjà sous le DocumentRoot
   sudo a2enmod auth_basic    # support HTTP Basic Auth
   sudo a2enmod authn_file    # lit les credentials depuis .htpasswd
   sudo systemctl reload apache2
   ```

2. Autoriser les overrides `.htaccess` dans le répertoire HTML.
   Éditer `/etc/apache2/mods-enabled/userdir.conf` (ou le VirtualHost) :

   ```apache
   <Directory /home/*/public_html>
       AllowOverride AuthConfig
       Options Indexes FollowSymLinks
       Require all granted
   </Directory>
   ```

   Puis recharger : `sudo systemctl reload apache2`

3. Donner au processus Apache accès en lecture au fichier `.htpasswd`.
   Le fichier est écrit en chmod 640 (lecture propriétaire + groupe).
   Apache tourne en `www-data` et doit pouvoir le lire :

   ```bash
   # Option A — lisible par tous (plus simple, expose le hash aux utilisateurs locaux)
   chmod o+r $POLYMARKET_DIR/.webstatus_htpasswd

   # Option B — ajouter www-data au groupe principal de l'utilisateur bot (plus sûr)
   sudo usermod -aG $(id -gn $USER) www-data
   sudo systemctl reload apache2
   ```

### Prérequis — nginx

nginx ne traite pas les fichiers `.htaccess`. Le bot écrit toujours la
page sur le disque, mais la protection par mot de passe n'a aucun
effet. Configurer la Basic Auth directement dans le bloc server nginx :

```nginx
location /tradinebot_status.html {
    auth_basic "Tradinebot Status";
    auth_basic_user_file /chemin/vers/.webstatus_htpasswd;
}
```

Le `.htpasswd` généré par le bot utilise le format Apache `{SHA}`,
également supporté par nginx. Utiliser le chemin
`POLYMARKET_DIR/.webstatus_htpasswd`.

Si `webstatuspage_path` pointe en dehors de `~/public_html`, s'assurer
que le serveur web est configuré pour servir ce répertoire.


## Lancement

```bash
POLYMARKET_DIR=~/polymarket bash scripts/start_bot.sh
```

Ou via le wrapper généré (`POLYMARKET_DIR` déjà intégré) :

```bash
~/polymarket/run.sh
```

Vérifier que le bot tourne :

```bash
pgrep -fa live_bot.py
```

`start_bot.sh` refuse de démarrer si une instance tourne déjà (pour éviter
d'interrompre un trade ouvert). L'arrêter manuellement si besoin :

```bash
pkill -f live_bot.py
```

- Logs : `<POLYMARKET_DIR>/live.log`
- Trades : `<POLYMARKET_DIR>/live.db` (SQLite)


## Monitoring

Dashboard en temps réel :

```bash
POLYMARKET_DIR=~/polymarket bash scripts/monitor.sh
```

Suivre les logs en direct :

```bash
tail -f ~/polymarket/live.log
```

Trades récents :

```bash
sqlite3 ~/polymarket/live.db \
  "SELECT id, direction, entry_price, outcome, ROUND(pnl_net,3), capital_after \
   FROM trades ORDER BY id DESC LIMIT 10;"
```

Stats du jour :

```bash
sqlite3 ~/polymarket/live.db \
  "SELECT COUNT(*), SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END), ROUND(SUM(pnl_net),2) \
   FROM trades WHERE resolved=1 AND created_at > (strftime('%s','now')-86400)*1000;"
```

Confirmer les ordres réels on-chain (pas simulés) :

```bash
grep "order=" ~/polymarket/live.log | grep -v "order=sim" | tail -20
```


## Tester dans un environnement virtuel

Utiliser [uv](https://github.com/astral-sh/uv) pour créer un environnement
de test isolé sans toucher au Python système ni au venv de production.

Installer uv (si pas déjà installé) :

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

Créer le venv et installer les dépendances :

```bash
uv venv .venv --python 3.13
uv pip install aiohttp websockets web3 py-clob-client --python .venv/bin/python3
```

Vérification de la syntaxe :

```bash
.venv/bin/python3 -m py_compile bot/live_bot.py && echo "SYNTAX OK"
```

Vérification des imports (s'assure que le code au niveau module s'exécute
sans erreur) :

```bash
.venv/bin/python3 -c "
import sys; sys.path.insert(0, '.')
import bot.live_bot as b
print('CONFIG_PATH:', b.CONFIG_PATH)
print('PRIVATE_KEY set:', bool(b.PRIVATE_KEY))
print('SIGNAL_THRESHOLD:', b.SIGNAL_THRESHOLD)
"
```

Lancer le bot pendant 20 secondes en mode simulation isolé (logs sur stdout,
écrit dans `/tmp/polymarket-sim` — les données de production ne sont jamais touchées) :

```bash
timeout 20 .venv/bin/python3 bot/live_bot.py --simulate
```

Sortie attendue (affichée directement dans le terminal) :

```
[WARNING]  MODE SIMULATION — donnees isolees dans /tmp/polymarket-sim
[INFO]     LIVE BOT v3 — Threshold=0.96 Stake=$10 MinAskVol=10
[WARNING]  POLY_PRIVATE_KEY non definie — ordres SIMULES
[INFO]     DB initialisee : /tmp/polymarket-sim/live.db
[INFO]     State : capital=$100.00 | 0 trades | WR=0.0%
[INFO]     Marches BTC 5-min : 2
[INFO]     Souscription 2 tokens...
[INFO]     WebSocket connecte
```

Le répertoire `.venv/` est listé dans `.gitignore` et ne doit pas être commité.
