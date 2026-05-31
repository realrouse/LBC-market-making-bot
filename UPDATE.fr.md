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

kill $(cat ~/tradinebotte/live.pid)
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

kill $(cat ~/tradinebotte/live.pid)
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
ssh user@serveur 'kill $(cat ~/tradinebotte/live.pid); bash ~/tradinebotte/scripts/start_bot.sh'
```

**Exclusions critiques :**
- `--exclude='config.json'` — empêche d'effacer les credentials live **et la langue choisie** (champ `"lang"` défini par `setup.py` / `install.sh`)
- `--exclude='live.db'` — préserve l'historique des trades
- `--exclude='venv/'` — évite de transférer des centaines de Mo sur le réseau

Sans `--exclude='config.json'`, `rsync --delete` supprime le fichier et
`start_bot.sh` refusera de démarrer. Relancer `setup.py` si c'est le cas (il
re-demandera la langue et régénérera le fichier).

---

## Scénario 4 — Déploiement allégé avec `update_standalone.sh`

Pour déployer uniquement les fichiers du bot (sans synchronisation complète du dépôt), utiliser le script dédié :

```bash
bash tradinebotte-polymarket/scripts/update_standalone.sh
```

Ce script copie en rsync le contenu de `tradinebotte-polymarket/` à plat dans le répertoire d'installation, les fichiers `tradinebotte-polymarket/strategies/*.json` et `requirements.txt`, puis exécute `pip install -r requirements.txt` pour mettre à jour les dépendances Python avant de stopper le bot en cours d'exécution (via `live.pid`) et de relancer la nouvelle version dans une seule session SSH. Pratique pour déployer depuis une machine de développement sans passer par git.

**Options :**
- `--skip-restart` — rsync uniquement, sans stop/start du bot
- `--verify-only` — vérifie que les fichiers déployés sont présents et que le bot tourne ; aucun transfert de fichiers

---

## Scénario 5 — Déploiement du compte swing

Pour le compte de déploiement dédié à la stratégie swing, utiliser le script de déploiement spécifique :

```bash
bash tradinebotte-cex/scripts/update_swing.sh
```

Ce script copie en rsync le moteur de stratégie swing et sa configuration vers le répertoire d'installation du compte swing, écrit son `config.json`, redémarre le bot via le fichier PID et vérifie que le processus tourne — le tout dans une seule session SSH. Il suit le même schéma que `update_standalone.sh`, mais cible la structure du compte swing.

---

## Scénario 6 — Déploiement d'une stratégie CEX

Trois scripts couvrent les stratégies du sous-service CEX. Chacun copie en rsync le moteur et la configuration concernés, redémarre le bot via son fichier PID et vérifie le processus — le tout dans une seule session SSH.

```bash
# Bot de scalping (OBI Binance)
bash tradinebotte-cex/scripts/deploy_scalping_claude4.sh

# Bot d'accumulation BTC v1.5
bash tradinebotte-cex/scripts/deploy_accumulation_claude4.sh

# Stratégie swing
bash tradinebotte-cex/scripts/update_swing.sh
```

---

## Option B — Mise à jour multi-bot

Mettre à jour le repo partagé et redémarrer. Les répertoires de comptes (`~/account-a`, etc.) ne sont pas touchés.

```bash
cd ~/src/tradinebotte   # ou l'emplacement du repo
git pull
bash scripts/install.sh

kill $(cat ~/tradinebotte/feed.pid)
kill $(cat ~/account-a/account.pid)
kill $(cat ~/account-b/account.pid)

TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

---

## Vérifier la mise à jour

```bash
pgrep -fa live_bot.py            # confirmer que le processus tourne
tail -5 ~/tradinebotte/live.log  # confirmer démarrage propre, pas d'erreurs
```
