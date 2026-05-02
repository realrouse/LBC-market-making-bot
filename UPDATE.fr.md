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
bash scripts/install.sh      # réutilise ~/tradinebotte/venv, upgrade packages seulement

pkill -f live_bot.py
bash scripts/start_bot.sh
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

pkill -f live_bot.py
bash scripts/start_bot.sh
```

Mêmes garanties — `config.json` et `live.db` sont préservés.

---

## Scénario 3 — Déploiement depuis une machine de dev via rsync

```bash
rsync -az --delete \
    --exclude='config.json' \
    --exclude='live.db' \
    --exclude='*.log' \
    --exclude='venv/' \
    /chemin/vers/tradinebotte/ user@serveur:~/tradinebotte/

ssh user@serveur 'cd ~/tradinebotte && bash scripts/install.sh'
ssh user@serveur 'pkill -f live_bot.py; bash ~/tradinebotte/scripts/start_bot.sh'
```

**Exclusions critiques :**
- `--exclude='config.json'` — empêche d'effacer les credentials live
- `--exclude='live.db'` — préserve l'historique des trades
- `--exclude='venv/'` — évite de transférer des centaines de Mo sur le réseau

Sans `--exclude='config.json'`, `rsync --delete` supprime le fichier et
`start_bot.sh` refusera de démarrer. Relancer `setup.py` si c'est le cas.

---

## Option B — Mise à jour multi-bot

Mettre à jour le repo partagé et redémarrer. Les répertoires de comptes (`~/account-a`, etc.) ne sont pas touchés.

```bash
cd ~/src/tradinebotte   # ou l'emplacement du repo
git pull
bash scripts/install.sh

pkill -f feed.py
pkill -f account_bot.py

TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

---

## Vérifier la mise à jour

```bash
pgrep -fa live_bot.py            # confirmer que le processus tourne
tail -5 ~/tradinebotte/live.log  # confirmer démarrage propre, pas d'erreurs
```
