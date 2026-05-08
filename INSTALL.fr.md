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

### Prérequis administrateur serveur (Debian/Ubuntu)

`scripts/install.sh` **détecte automatiquement les paquets manquants** et
affiche la commande `sudo apt-get install` exacte à exécuter en root — inutile
de connaître les noms de paquets à l'avance.

Il suffit de lancer le script en tant qu'utilisateur normal :

```bash
bash scripts/install.sh
```

Si quelque chose manque, le script affiche :

```
ERREUR : paquets système manquants. Lance cette commande en root (une seule fois par machine) :

  sudo apt-get install -y python3-venv python3.10-venv
```

Le numéro de version (`3.10`) est détecté automatiquement depuis le Python
système — aucune substitution manuelle nécessaire. Lancer la commande affichée
en root, puis relancer `install.sh`.

Une fois installés, `install.sh` place toutes les dépendances Python dans un
venv isolé et **ne touche plus jamais au Python système**.


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


## Obtenir le code source

Trois méthodes sont disponibles selon votre configuration. Les trois aboutissent
à la même étape `bash scripts/install.sh`.

### Méthode 1 — Git clone (recommandée si GitHub est accessible)

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh
```

Pour installer une version précise :

```bash
git clone --branch v0.40 https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh
```

### Méthode 2 — rsync depuis une machine de développement (recommandée pour VPS sans git)

Depuis votre machine locale où le dépôt est déjà cloné :

```bash
rsync -a --exclude='*.db' --exclude='__pycache__' --exclude='.git' --exclude='venv' \
  /chemin/vers/tradinebotte/ user@serveur:~/tradinebotte/
ssh user@serveur "cd ~/tradinebotte && bash scripts/install.sh"
```

Pour mettre à jour une installation existante (préserve `config.json`) :

```bash
rsync -a --exclude='*.db' --exclude='__pycache__' --exclude='.git' --exclude='venv' \
  --exclude='config.json' \
  /chemin/vers/tradinebotte/ user@serveur:~/tradinebotte/
ssh user@serveur "cd ~/tradinebotte && bash scripts/install.sh"
```

> Le flag `--exclude='config.json'` est critique lors des mises à jour — sans lui,
> rsync écrase le fichier de credentials de production.

### Méthode 3 — Archive tar.gz de release officielle (sans git)

Télécharger la dernière archive de release depuis la
[page Releases](https://github.com/neofutur/tradinebotte/releases) :

```bash
# Remplacer v0.40 par la version souhaitée
wget https://github.com/neofutur/tradinebotte/archive/refs/tags/v0.40.tar.gz
tar -xzf v0.40.tar.gz
cd tradinebotte-0.40
bash scripts/install.sh
```

Ou avec `curl` :

```bash
curl -L https://github.com/neofutur/tradinebotte/archive/refs/tags/v0.40.tar.gz \
  | tar -xz
cd tradinebotte-0.40
bash scripts/install.sh
```

Le répertoire s'appelle `tradinebotte-<version>` après extraction. Le script
d'installation détecte automatiquement son emplacement — aucun ajustement de
chemin n'est nécessaire.


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
bash scripts/install.sh [répertoire_installation] [--lang EN|FR] [--with-tests]
```

**Options :**
- `--lang EN|FR` — Définit la langue sans prompt interactif (utile pour CI ou déploiements automatisés).
  Sans ce flag, le script propose le choix au démarrage comme avant.
- `--with-tests` — Copie aussi `tests/`, `scripts/backtest.py` et
  `data/backtest_sample_btc5m_range_2026.db`, puis lance
  la suite complète de tests (163 tests) juste après l'installation.
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

Exécuter `setup.py` avant de lancer le bot — il crée `config.json` :

```bash
python3 scripts/setup.py
```

Lors de la demande de clé privée :
- **Wallet réel :** saisir `0x` + 64 caractères hexadécimaux — le script vérifie les balances,
  effectue le swap USDC natif → USDC.e si nécessaire, approuve le CTF Exchange, dérive les
  credentials API Polymarket et écrit `<TRADINEBOTTE_DIR>/config.json` (chmod 600).
- **Simulation (sans wallet) :** appuyer sur Entrée sans saisir de clé — le script écrit un
  `config.json` minimal avec des credentials vides ; le bot tourne en ordres simulés et aucune
  transaction on-chain n'est effectuée.

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
- `--no-snapshots` — ne pas écrire les snapshots de prix toutes les 5 s dans la DB ; les trades continuent d'être enregistrés ; réduit la pression d'écriture sur les longues sessions ; à utiliser quand les données de snapshots ne sont pas nécessaires pour l'analyse post-session
- `--reset-db` — sauvegarde `live.db` dans `live.db.bak.YYYYMMDD_HHMMSS` puis le supprime avant le lancement ; le bot repart de zéro (capital et historique de trades) ; demande une confirmation `yes` ; sans effet si la DB est absente
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

# Grille étendue (405 combos) sur toutes les BDs — optimisation de stratégie
python3 scripts/backtest.py --sweep-all
python3 scripts/backtest.py --sweep-all --sort pnl   # trier par pnl|ratio|wr
python3 scripts/backtest.py --sweep-all --top 10     # top-10 configs uniques (dédupliqué)
```

Quand plusieurs fichiers sont traités, chaque fichier tourne avec le capital réinitialisé à `capital_start` (simulation indépendante), et un bloc AGGREGATE résume les wins, losses, PnL, taux de victoire et pire drawdown combinés de tous les fichiers.

**Flags de paramètres** (écrasent les valeurs par défaut du JSON de stratégie pour un seul run) :

| Flag | Défaut | Description |
|---|---|---|
| `--threshold FLOAT` | 0.95 | Seuil du signal d'entrée (`best_bid >= seuil`) |
| `--min-secs FLOAT` | 30.0 | Secondes minimum restantes à l'entrée |
| `--min-ask FLOAT` | 10.0 | Volume minimum côté ask en USD à l'entrée |
| `--obi FLOAT` | −0.25 | Seuil de rejet OBI (les entrées avec un OBI inférieur à cette valeur sont ignorées) |
| `--stake FLOAT` | 10.0 | Mise en USD par trade |
| `--sweep-all` | — | Grille étendue (405 combos) sur toutes les BDs (ajoute les axes OBI et DSL) |
| `--sort METRIC` | `ratio` | Trier les résultats du sweep par `ratio` (PnL/MaxDD), `pnl` ou `wr` |
| `--top N` | 0 (tous) | Afficher uniquement les top-N configs uniques (dédupliqué sur seuil/min_secs/obi) |


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

**Flags de l'account bot** (`scripts/start_account.sh` les transmet à `bot/account_bot.py`) :

- `--verbose` — active le logging DEBUG pour le diagnostic ; affiche chaque book update, évaluation de signal et message ZMQ reçu ; utile lors de la mise en place initiale ou du débogage

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

### Tests d'intégration

Deux tests d'intégration SSH couvrent les scénarios de serveur partagé. Les deux lisent depuis le même `~/.tradinebotte-test.conf` :

```bash
cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf
editor ~/.tradinebotte-test.conf
```

**Lancer tous les tests d'intégration (recommandé) :**

```bash
bash scripts/run_integration_tests.sh              # les deux tests en séquence
bash scripts/run_integration_tests.sh --standalone # Option A seulement
bash scripts/run_integration_tests.sh --multibot   # Option B seulement
```

**`test_standalone_deploy.sh`** — Option A multi-utilisateur (`live_bot.py` standalone) :
- Déploie sur 2 utilisateurs Linux du même serveur
- L'utilisateur 1 lance `start_bot.sh` → doit réussir
- L'utilisateur 2 lance `start_bot.sh` pendant que l'utilisateur 1 tourne → doit aussi réussir
- Vérifie l'absence de "une instance est déjà en cours" dans les logs (détecte la classe de bugs `pgrep` scope)
- Connexions WebSocket confirmées dans les deux logs

**`test_multibot_deploy.sh`** — Option B multi-utilisateur (feed ZeroMQ + account bots) :
- Feed auto-démarré quand 3 bots se lancent simultanément (verrou fichier sans race condition)
- Exactement un processus `feed.py` visible depuis tous les utilisateurs Linux
- Les 3 processus `account_bot.py` se connectent et reçoivent des book updates
- Aucune ligne ERROR/CRITICAL dans les logs pendant la fenêtre de 3 minutes
- Tous les processus arrêtés proprement après le test

```bash
# Lancements individuels avec options :
bash scripts/test_standalone_deploy.sh --skip-deploy
bash scripts/test_multibot_deploy.sh --skip-deploy --duration 300
```


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


## Interface bilingue

Tous les scripts interactifs proposent un choix de langue au démarrage :

```
Language / Langue :  [E] English   [F] Français
>>>
```

Le choix est persisté sous la clé `"lang": "EN"` ou `"lang": "FR"` dans `config.json` par `setup.py`.
Les scripts suivants (`start_bot.sh`, `monitor.sh`) lisent cette clé automatiquement — aucune re-saisie.

Si `config.json` est absent (avant le premier `setup.py`), `start_bot.sh` et `monitor.sh`
utilisent l'anglais par défaut. `install.sh` demande toujours interactivement car il s'exécute avant `setup.py`.

Pour changer la langue après la configuration initiale, éditez `config.json` :

```json
{ "lang": "FR" }
```

ou relancez `python3 scripts/setup.py` et choisissez à nouveau.


## Connecteurs CEX (Binance, MEXC)

Deux adaptateurs d'exchange supplémentaires sont inclus comme remplaçants directs de `api_polymarket.py` :

| Fichier | Exchange | Frais | Flux WebSocket |
|---|---|---|---|
| `bot/api_binance.py` | Binance spot | 0,1 % taker | `btcusdt@depth5@100ms` |
| `bot/api_mexc.py` | MEXC spot | 0,2 % taker | `spot@public.limit.depth.v3.api@BTCUSDT@5` |

Les deux implémentent l'interface publique identique : `get_markets`, `post_order`,
`parse_book_update`, `compute_fee` et les helpers de métadonnées de marché.

**Credentials** — via variables d'environnement ou `config.json` :

```bash
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...
export MEXC_API_KEY=...
export MEXC_API_SECRET=...
```

**Changer d'exchange** — modifier une seule ligne dans `live_bot.py` (ligne 62) :

```python
import api_binance as api   # à la place de api_polymarket
# ou
import api_mexc as api
```

**Important** : le signal Polymarket (`best_bid >= 0.96`) opère sur une échelle 0–1 (probabilités).
Les prix Binance/MEXC sont des valeurs USDT absolues (ex. 65000). Les seuils de stratégie dans
`strategies/*.json` doivent être recalibrés avant d'utiliser un connecteur CEX.


## Benchmark de latence API

Comparer la latence REST et WebSocket des trois exchanges :

```bash
python3 scripts/benchmark_api.py             # 15 rounds, tous les exchanges
python3 scripts/benchmark_api.py --rounds 30 # plus d'échantillons
python3 scripts/benchmark_api.py --no-ws     # REST uniquement (plus rapide)
```

Les résultats peuvent être sauvegardés :

```bash
python3 scripts/benchmark_api.py 2>&1 | tee latence_api.txt
```

Latences de référence mesurées depuis un VPS Amsterdam :

| Exchange | REST moyen | REST p99 | WS moyen |
|---|---|---|---|
| Polymarket Gamma | ~14 ms | ~20 ms | ~65 ms |
| MEXC | ~15 ms | ~80 ms | ~905 ms |
| Binance | ~225 ms | ~232 ms | ~990 ms |

La latence Binance élevée depuis l'Europe s'explique par le routage géographique ;
depuis un VPS asiatique les chiffres seraient inversés.
