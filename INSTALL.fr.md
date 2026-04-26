# tradinebotte — Guide d'installation

> 🇬🇧 [English version](INSTALL.md) · Première fois ? Commencer par [QUICKSTART.fr.md](QUICKSTART.fr.md)


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
  ~/tradinebotte/venv/bin/python3 -c \
    "import sqlite3; c=sqlite3.connect('live.db'); \
     print(c.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0])"
  ```


## Dépendances

Les packages Python suivants sont installés automatiquement par `scripts/install.sh`
dans un virtualenv situé dans `~/tradinebotte/venv/` :

- `aiohttp`
- `websockets`
- `web3`
- `py-clob-client`

La liste canonique est `requirements.txt` à la racine du projet. Les CVE dans ces
packages sont détectés automatiquement à chaque push via `pip-audit` (GitHub Actions)
et Dependabot ouvre des PRs lorsque de nouvelles versions sont disponibles.

Les dépendances de développement (`pylint`, `pip-audit`, `mypy`) sont déclarées dans `requirements-dev.txt`.


## Répertoire d'installation

Tous les scripts lisent la variable d'environnement `TRADINEBOTTE_DIR` pour
déterminer où installer et exécuter le bot. Si elle n'est pas définie,
la valeur par défaut est :

```
~/tradinebotte
```

Aucun accès root requis — le chemin par défaut est dans le répertoire personnel de l'utilisateur.

Exemples :

```bash
# Par défaut (installe dans ~/tradinebotte, sans root)
bash scripts/install.sh

# Chemin personnalisé en argument
bash scripts/install.sh ~/tradinebotte

# Chemin personnalisé via variable d'environnement
TRADINEBOTTE_DIR=~/tradinebotte bash scripts/install.sh
```

La même variable doit être définie de manière cohérente pour le setup,
le lancement et le monitoring :

```bash
export TRADINEBOTTE_DIR=~/tradinebotte
```


## Installation

Exécuter le script d'installation depuis la racine du dépôt :

```bash
bash scripts/install.sh [répertoire_installation] [--with-tests]
```

**Options :**
- `--with-tests` — Copie aussi `tests/`, `scripts/backtest.py` et
  `data/backtest_sample_btc5m_range_2026.db`, puis lance
  la suite complète de tests (153 tests) juste après l'installation.
  Le backtest utilise `live.db` uniquement s'il contient ≥ 100 snapshots ;
  sinon il bascule automatiquement sur le dataset embarqué.

Ce script va :
- Installer les packages système (python3, pip, venv, sqlite3)
- Créer le répertoire d'installation
- Copier `bot/live_bot.py` et `bot/api_polymarket.py` vers `<TRADINEBOTTE_DIR>/`
- Copier `strategies/*.json` vers `<TRADINEBOTTE_DIR>/strategies/`
- Créer un virtualenv dans `<TRADINEBOTTE_DIR>/venv/`
- Installer les dépendances Python dans le virtualenv
- Générer `<TRADINEBOTTE_DIR>/run.sh` (wrapper avec `TRADINEBOTTE_DIR` pré-défini)
- Vérifier la syntaxe du bot


## Configuration du wallet (une seule fois)

Exécuter `setup.py` une seule fois avec votre wallet Polygon. Il va :
- Demander la clé privée de manière interactive (stdin masqué)
- Vérifier les balances USDC.e et USDC natif
- Effectuer le swap USDC natif → USDC.e via Uniswap V3 si nécessaire
- Approuver l'allowance CTF Exchange
- Dériver les credentials API Polymarket
- Écrire les credentials dans `<TRADINEBOTTE_DIR>/config.json` (chmod 600)

```bash
TRADINEBOTTE_DIR=~/tradinebotte python3 scripts/setup.py
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
Le fichier `.htpasswd` est stocké dans `TRADINEBOTTE_DIR/.webstatus_htpasswd`
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
   chmod o+r $TRADINEBOTTE_DIR/.webstatus_htpasswd

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
`TRADINEBOTTE_DIR/.webstatus_htpasswd`.

Si `webstatuspage_path` pointe en dehors de `~/public_html`, s'assurer
que le serveur web est configuré pour servir ce répertoire.


## Lancement

```bash
TRADINEBOTTE_DIR=~/tradinebotte bash scripts/start_bot.sh
```

### Démarrage automatique avec systemd (recommandé sur VPS)

Exécuter le script générateur une fois après l'installation :

```bash
TRADINEBOTTE_DIR=~/tradinebotte bash scripts/install_service.sh
```

Il valide l'installation, écrit un fichier d'unité prêt à l'emploi dans `/tmp/tradinebotte.service`
et affiche les commandes exactes pour l'activer :

```bash
sudo cp /tmp/tradinebotte.service /etc/systemd/system/tradinebotte.service
sudo systemctl daemon-reload
sudo systemctl enable tradinebotte   # démarrer au boot
sudo systemctl start tradinebotte    # démarrer maintenant
```

Commandes utiles :

```bash
sudo systemctl status tradinebotte
sudo systemctl stop tradinebotte
sudo systemctl restart tradinebotte
journalctl -u tradinebotte -f        # logs systemd en direct
tail -f ~/tradinebotte/live.log      # logs applicatifs du bot
```

Le service redémarre automatiquement en cas d'erreur (`Restart=on-failure`, délai 30 s,
max 5 redémarrages par 5 minutes). Au reboot, le bot revient dès que le réseau est
disponible (`After=network-online.target`).

> **Multi-bot (Option B)** : utiliser `scripts/install_feed_service.sh` et
> `scripts/install_account_service.sh` à la place. Voir [docs/multi.md](docs/multi.md).

**Flags :**
- *(aucun flag)* — mode normal : les écritures de logs sont asynchrones (thread daemon, ne bloque jamais le event loop)
- `--no-log` — supprime le fichier log pour un I/O disque minimal ; la DB SQLite (trades + snapshots) n'est pas affectée ; combiner avec `--simulate` pour conserver la sortie stdout
- `--simulate` — isole tous les fichiers dans `~/tradinebotte-sim` par défaut, aucun ordre réel. Si `TRADINEBOTTE_DIR` est déjà défini dans l'environnement, ce chemin est utilisé à la place — ce qui permet de faire tourner plusieurs bots en parallèle sans conflit :
  ```bash
  TRADINEBOTTE_DIR=~/compte-a python3 live_bot.py --simulate
  TRADINEBOTTE_DIR=~/compte-b python3 live_bot.py --simulate
  ```

Ou via le wrapper généré (`TRADINEBOTTE_DIR` déjà intégré) :

```bash
~/tradinebotte/run.sh
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

- Logs : `<TRADINEBOTTE_DIR>/live.log`
- Trades : `<TRADINEBOTTE_DIR>/live.db` (SQLite)


## Analyse de latence

Chaque trade émet une ligne `[LATENCY]` dans `live.log`. Lancer l'outil d'analyse après une session :

```bash
python3 scripts/latency.py                           # chemin par défaut
python3 scripts/latency.py ~/tradinebotte/live.log   # chemin explicite
TRADINEBOTTE_DIR=~/tradinebotte python3 scripts/latency.py
```

Exemple de sortie :
```
==============================================================
  LATENCY REPORT — /home/botte/tradinebotte/live.log
  Trades: 42  (UP=27  DOWN=15)
==============================================================
  Metric             min    mean     p50     p90     p99     max
  ----------------------------------------------------------
  signal (ms)        1.2     2.1     1.9     3.4     5.1     6.0
  order RTT (ms)    98.3   143.2   138.7   201.4   310.2   340.5
  total (ms)        99.8   145.3   140.9   204.1   314.8   345.0
==============================================================
```

- **signal_ms** — temps entre la réception du message WebSocket et la décision d'ordre (inclut tous les gardes du signal + la requête SQLite PnL journalier)
- **order_rtt_ms** — round-trip HTTP de l'API CLOB
- **total_ms** — bout en bout : message WebSocket → ordre confirmé

## Backtests

Lancer le moteur de backtest pour rejouer les snapshots enregistrés avec n'importe quel jeu de paramètres :

```bash
# Fichier unique (défaut : live.db, ou dataset embarqué si live.db a < 100 snapshots)
python3 scripts/backtest.py

# Un ou plusieurs fichiers explicites (glob shell supporté)
python3 scripts/backtest.py --db ~/tradinebotte/live.db
python3 scripts/backtest.py --db data/session1.db data/session2.db
python3 scripts/backtest.py --db data/*.db

# Scanner data/ automatiquement (inclut live.db s'il a ≥ 100 snapshots)
python3 scripts/backtest.py --all

# Recherche en grille sur 135 combinaisons seuil/mise
python3 scripts/backtest.py --sweep
python3 scripts/backtest.py --all --sweep
```

Quand plusieurs fichiers sont traités, chaque fichier tourne avec le capital réinitialisé à `capital_start` (simulation indépendante), et un bloc AGGREGATE résume les wins, losses, PnL, taux de victoire et pire drawdown combinés de tous les fichiers.


## Filtre heure / jour

Le bot peut restreindre les entrées en trade à des plages horaires UTC selon le type de jour. Le filtre est configuré dans le fichier de stratégie JSON (`strategies/polymarket_BTC5M.json`) et est **désactivé par défaut** — le comportement existant est préservé jusqu'à activation explicite.

### Pourquoi un filtre horaire ?

La volatilité BTC suit des patterns journaliers et hebdomadaires liés aux flux institutionnels :

| Période | Fenêtre UTC | Caractéristique |
|---|---|---|
| Session asiatique | 00:00–08:00 | Volume modéré, mouvements directionnels |
| Zone morte européenne | 08:00–13:00 | Volume faible, signaux bruités |
| Session US | 13:00–22:00 | Volume élevé, signaux les plus fiables |
| Ouverture hebdomadaire US | Lun 13:30 | Retour institutionnel après le weekend ; fort mouvement directionnel |
| Fermeture hebdomadaire US | Ven 20:00 | Débouclement de positions ; pic de volatilité puis chute |
| Weekend | Sam–Dim | Retail-driven, bruit plus élevé, prévisibilité réduite |

### Configuration

Ajouter ou modifier le bloc `hour_filter` dans votre fichier de stratégie JSON :

```json
"hour_filter": {
    "enabled": true,
    "weekday_utc_ranges": [[0, 8], [13, 22]],
    "weekend_utc_ranges": [],
    "us_weekly_open": true,
    "us_weekly_close": true
}
```

| Clé | Type | Défaut | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Interrupteur général. `false` = pas de filtre, toutes les heures autorisées. |
| `weekday_utc_ranges` | liste de `[début, fin]` | `[]` | Plages UTC autorisées lun–ven. Vide = toutes les heures de semaine autorisées. |
| `weekend_utc_ranges` | liste de `[début, fin]` | `[]` | Plages UTC autorisées sam–dim. Vide = **tout le weekend bloqué**. |
| `us_weekly_open` | bool | `true` | Si `true`, bloque les entrées le **lundi avant 13h30 UTC** (les marchés US ne sont pas encore ouverts pour la semaine). |
| `us_weekly_close` | bool | `true` | Si `true`, bloque les entrées le **vendredi à partir de 20h00 UTC** (les marchés US ont fermé pour la semaine). |

Les plages horaires suivent la convention `[début, fin)` — `[13, 22]` signifie 13:00 ≤ heure < 22:00.
Les contraintes `us_weekly_open` et `us_weekly_close` s'appliquent **en plus** de `weekday_utc_ranges`, et sont prioritaires sur leurs jours respectifs.

### Logique de décision (exemple lundi, filtre actif)

```
Lundi 07:00 UTC
  → vérification plage semaine : 07 est dans [0, 8) → serait OK
  → vérification us_weekly_open : 07 < 13:30 → BLOQUÉ

Lundi 13:45 UTC
  → vérification us_weekly_open : 13:45 ≥ 13:30 → passe
  → vérification plage semaine : 13 est dans [13, 22) → AUTORISÉ

Samedi 15:00 UTC
  → weekend_utc_ranges est [] → BLOQUÉ
```

### Configurations prêtes à l'emploi

**Conservative — session US uniquement, pas de weekend :**
```json
"hour_filter": {
    "enabled": true,
    "weekday_utc_ranges": [[13, 22]],
    "weekend_utc_ranges": [],
    "us_weekly_open": true,
    "us_weekly_close": true
}
```

**Étendue — sessions asiatique + US, pas de weekend :**
```json
"hour_filter": {
    "enabled": true,
    "weekday_utc_ranges": [[0, 8], [13, 22]],
    "weekend_utc_ranges": [],
    "us_weekly_open": true,
    "us_weekly_close": true
}
```

**24/7 — toutes heures, tous jours (équivalent à désactivé) :**
```json
"hour_filter": {
    "enabled": true,
    "weekday_utc_ranges": [],
    "weekend_utc_ranges": [[0, 24]],
    "us_weekly_open": false,
    "us_weekly_close": false
}
```

### Backtest avec filtre

Le moteur de backtest applique la même logique de filtre lors de la relecture des snapshots — mesurer son effet avant d'activer en live :

```bash
# Mettre hour_filter.enabled = true dans le JSON de stratégie, puis :
python3 scripts/backtest.py --all
```

Comparer le taux de victoire et le nombre de trades avec et sans filtre pour valider les fenêtres choisies sur votre dataset de snapshots.

### Log au démarrage

Quand le filtre est actif, le bot affiche la configuration effective au démarrage :

```
[INFO]   Filtre horaire : sem=0-8h 13-22h | we=bloque ouv.lun=13h30 ferm.ven=20h00
```


## Partage WebSocket multi-bot (Option B — ZeroMQ)

> Référence complète de l'architecture et guide de décision : **[docs/multi.fr.md](docs/multi.fr.md)**

Utiliser l'Option B pour faire tourner deux comptes ou plus simultanément, quand
les comptes appartiennent à des utilisateurs Linux différents, ou pour comparer
différentes stratégies en parallèle. Pour un seul compte, l'Option A
(`live_bot.py` autonome) est plus simple.

L'architecture ZeroMQ sépare le bot en deux processus distincts :

| Processus | Fichier | Rôle |
|---|---|---|
| Feed | `bot/feed.py` | Connexion WS unique ; diffuse les mises à jour via ZMQ PUB |
| Account bot | `bot/account_bot.py` | Souscrit au feed ; trade un compte en isolation complète |

### Prérequis

`pyzmq` est déjà inclus dans `requirements.txt`. L'installer avec le reste des
dépendances :

```bash
bash scripts/install.sh
```

### Arborescence (exemple — deux comptes)

```
~/tradinebotte/          ← venv partagé + log du feed
  venv/
  feed.log
~/account-a/             ← compte A : DB, log, config propres
  config.json
  live.db
  account.log
~/account-b/             ← compte B : DB, log, config propres
  config.json
  live.db
  account.log
```

Configurer chaque répertoire de compte d'abord :

```bash
TRADINEBOTTE_DIR=~/account-a python3 scripts/setup.py   # clé compte A
TRADINEBOTTE_DIR=~/account-b python3 scripts/setup.py   # clé compte B
```

### Lancement

```bash
# 1. Lancer le feed partagé (une seule instance)
bash scripts/start_feed.sh

# 2. Lancer chaque account bot dans un terminal séparé
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

Adresse personnalisée (port ou hôte différent) :

```bash
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 bash scripts/start_feed.sh
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
```

### Arrêt

```bash
pkill -f feed.py
pkill -f account_bot.py
```

### Protocole de messages

Le feed publie trois types de messages JSON via ZeroMQ PUB :

| Type | Champs | Rôle |
|---|---|---|
| `market` | `market_id`, `question`, `up_token_id`, `dn_token_id`, `start_ms`, `end_ms` | Nouveau marché enregistré |
| `book` | `token_id`, `best_bid`, `best_ask`, `spread`, `bid_vol`, `ask_vol`, `obi` | Mise à jour du carnet |
| `ping` | `ts` | Keepalive toutes les 10 s |

### Notes d'architecture

- Le feed n'a aucune logique de trading et ne stocke aucune clé — il est sûr de le redémarrer sans affecter l'état des comptes.
- Chaque processus `account_bot.py` écrit dans sa propre base SQLite ; le chemin `handle_book_update` / `check_signal` / `enter_live_trade` de `live_bot.py` s'exécute sans modification.
- Si le feed redémarre, les account bots récupèrent automatiquement — ils rateront les mises à jour pendant l'interruption mais ne placeront pas d'ordres en double car l'ensemble `signalled` est persisté dans la DB entre les sessions.
- Le pattern PUB/SUB ZeroMQ est unidirectionnel : les account bots n'envoient jamais de messages au feed.


## Monitoring

Dashboard en temps réel :

```bash
TRADINEBOTTE_DIR=~/tradinebotte bash scripts/monitor.sh
```

Suivre les logs en direct :

```bash
tail -f ~/tradinebotte/live.log
```

Trades récents :

```bash
sqlite3 ~/tradinebotte/live.db \
  "SELECT id, direction, entry_price, outcome, ROUND(pnl_net,3), capital_after \
   FROM trades ORDER BY id DESC LIMIT 10;"
```

Stats du jour :

```bash
sqlite3 ~/tradinebotte/live.db \
  "SELECT COUNT(*), SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END), ROUND(SUM(pnl_net),2) \
   FROM trades WHERE resolved=1 AND created_at > (strftime('%s','now')-86400)*1000;"
```

Confirmer les ordres réels on-chain (pas simulés) :

```bash
grep "order=" ~/tradinebotte/live.log | grep -v "order=sim" | tail -20
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
écrit dans `~/tradinebotte-sim` — les données de production ne sont jamais touchées) :

```bash
timeout 20 .venv/bin/python3 bot/live_bot.py --simulate
```

Sortie attendue (affichée directement dans le terminal) :

```
[WARNING]  MODE SIMULATION — donnees isolees dans ~/tradinebotte-sim
[INFO]     LIVE BOT v3 — Threshold=0.96 Stake=$10 MinAskVol=10
[WARNING]  POLY_PRIVATE_KEY non definie — ordres SIMULES
[INFO]     DB initialisee : ~/tradinebotte-sim/live.db
[INFO]     State : capital=$100.00 | 0 trades | WR=0.0%
[INFO]     Marches BTC 5-min : 2
[INFO]     Souscription 2 tokens...
[INFO]     WebSocket connecte
```

Le répertoire `.venv/` est listé dans `.gitignore` et ne doit pas être commité.
