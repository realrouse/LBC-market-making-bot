# tradinebotte — Guide de mise à jour

> 🇬🇧 [English version](UPDATE.md) · Référence complète : [INSTALL.fr.md](INSTALL.fr.md)

---

## Ce que fait `install.sh` lors d'une mise à jour

Quand le virtualenv existe déjà, `install.sh` **saute la création** et se limite à mettre
à jour pip et les packages. Il ne touche jamais à `config.json`, `live.db` ni aux logs.

---

## Scénario 1 — Repo et répertoire d'installation séparés

Layout type : repo cloné dans `~/src/tradinebotte`, bot installé dans `~/tradinebotte`.

```bash
cd ~/src/tradinebotte
git pull
bash scripts/install.sh      # réutilise ~/tradinebotte/.venv, upgrade packages seulement

kill $(cat ~/tradinebotte/live.pid)
~/tradinebotte/run.sh
# ou : sudo systemctl restart tradinebotte
```

`config.json` n'est pas touché — inutile de relancer `setup.py`.

---

## Scénario 2 — Repo = répertoire d'installation

Layout type : repo cloné directement dans `~/tradinebotte`.

```bash
cd ~/tradinebotte
git pull
bash scripts/install.sh

kill $(cat ~/tradinebotte/live.pid)
~/tradinebotte/run.sh
```

Mêmes garanties — `config.json` et `live.db` sont préservés.

---

## Scénario 3 — Déploiement depuis une machine de dev via rsync

```bash
rsync -az --delete \
    --exclude='config.json' \
    --exclude='live.db' \
    --exclude='*.log' \
    --exclude='venv/' --exclude='.venv/' \
    /chemin/vers/tradinebotte/ user@serveur:~/tradinebotte/

ssh user@serveur 'cd ~/tradinebotte && bash scripts/install.sh'
ssh user@serveur 'kill $(cat ~/tradinebotte/live.pid); ~/tradinebotte/run.sh'
```

**Exclusions critiques :**
- `--exclude='config.json'` — empêche d'effacer les credentials live **et la langue choisie** (champ `"lang"` défini par `setup.py` / `install.sh`)
- `--exclude='live.db'` — préserve l'historique des trades
- `--exclude='venv/' --exclude='.venv/'` — évite de transférer des centaines de Mo sur le réseau (couvre les deux layouts `venv/` et `.venv/`)

Sans `--exclude='config.json'`, `rsync --delete` supprime le fichier et
`start_bot.sh` refusera de démarrer. Relancer `setup.py` si c'est le cas (il
re-demandera la langue et régénérera le fichier).

---

## Scénario 4 — Déploiement allégé avec `update_standalone.sh`

Pour déployer uniquement les fichiers du bot (sans synchronisation complète du dépôt), utiliser le script dédié :

```bash
bash tradinebotte-polymarket/scripts/update_standalone.sh
```

Ce script copie en rsync le contenu de `tradinebotte-polymarket/` à plat dans le répertoire d'installation, les fichiers `tradinebotte-polymarket/strategies/*.json`, `tradinebotte-cex/connectors/`, `tradinebotte-cex/strategy_engines/`, `requirements.txt` et `tradinetools/`, puis exécute `pip install -r requirements.txt` pour mettre à jour les dépendances Python avant de stopper le bot en cours d'exécution (via `live.pid`) et de relancer la nouvelle version dans une seule session SSH. Pratique pour déployer depuis une machine de développement sans passer par git.

**Options :**
- `--skip-restart` — rsync uniquement, sans stop/start du bot
- `--verify-only` — vérifie que les fichiers déployés sont présents et que le bot tourne ; aucun transfert de fichiers

---

## Scénario 5 — Déploiement d'une stratégie CEX / swing / grid / polymarket

Tout bot de trading se déploie désormais via le déployeur natif piloté par l'inventaire — plus de script
bash par stratégie. Déployer un bot (ou toute la flotte) via `deploy_all.sh`, qui dérive chaque étape de
`inventory.toml` :

```bash
# Un bot (filtrer sur son compte/label)
bash tradinebotte-cex/scripts/deploy_all.sh --only "account-5 — binance-swing"

# Toute la flotte
bash tradinebotte-cex/scripts/deploy_all.sh
```

Chaque étape native rsync le code partagé, écrit un `config_<instance>.json` self-contained, rafraîchit
tradinetools et redémarre l'unité systemd `--user`. Les clés/wallet viennent d'un fichier env 600 chargé par
l'unité (`MEXC_API_KEY`, `POLY_PRIVATE_KEY`), jamais de config.json.

---

## v0.50 — Notes de mise à jour du service d'indicateurs

La v0.50 ajoute de nouveaux flux et paramètres à `tradinebotte-indicators/indicators.py`. Lors de la mise à jour depuis la v0.49 :

1. Redémarrer le service d'indicateurs partagé **avant** de redémarrer les bots dépendants (accumulation, scalping, swing) :

```bash
# Avec le service systemd utilisateur (recommandé — sans sudo) :
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user restart tradinebotte-indicators.service
# ou via le fichier PID :
kill $(cat ~/tradinebotte/indicators.pid)
bash tradinebotte-indicators/scripts/start_indicators.sh
```

2. Pour activer la base de données SQLite partagée du carnet d'ordres sur un flux, ajouter les clés suivantes à l'entrée de flux dans le JSON de configuration des indicateurs (toutes optionnelles — omettre `db_path` ou le laisser vide désactive les écritures) :

```json
{
  "stream_id": "btc_full_depth_perp",
  "market": "perp",
  "db_path": "",
  "bucket_size_usd": 50,
  "db_write_every_n": 60,
  "history_retention_h": 24
}
```

Le fichier DB est créé avec les droits `0o644`. Le répertoire parent du chemin doit être accessible en écriture par l'utilisateur du service d'indicateurs.

3. Aucune modification de `config.json`, `live.db` ou des fichiers JSON de stratégie des bots n'est requise pour cette mise à jour.

---

## Tableau de bord de statut multi-bot — chemin de sortie

Depuis la v0.80, `generate_status.py` écrit dans `~/public_html/tradinebottestatus.html`
par défaut au lieu de stdout. Si la sortie était redirigée dans un script ou une crontab,
ajouter `--out /dev/stdout` pour retrouver le comportement précédent, ou définir
`TRADINEBOTTE_STATUS_OUT` avec le chemin souhaité.

---

## Vérifier la mise à jour

```bash
pgrep -fa live_bot.py            # confirmer que le processus tourne
tail -5 ~/tradinebotte/live.log  # confirmer démarrage propre, pas d'erreurs
```
