# tradinebotte — Démarrage rapide

> 🇬🇧 [English version](QUICKSTART.md) · Guide complet : [INSTALL.fr.md](INSTALL.fr.md) · Mise à jour : [UPDATE.fr.md](UPDATE.fr.md)

## Prérequis

- Python 3.8+ sur Linux/Mac (serveur dédié ou machine locale)
- Wallet Polygon EOA — MATIC > 0,1 (gas) et USDC.e > 10 $
- **Pas encore de wallet ?** Appuyer sur Entrée au prompt `setup.py` → mode simulation, aucun ordre réel

---

## Installer depuis une release officielle (tar.gz)

```bash
# Télécharger la dernière release (remplacer v0.63 par la version actuelle)
wget https://github.com/neofutur/tradinebotte/archive/refs/tags/v0.63.tar.gz
tar -xzf v0.63.tar.gz
cd tradinebotte-0.63
bash scripts/install.sh        # détecte les paquets manquants ; demande la langue (E/F)
python3 scripts/setup.py       # demande la langue (sauvegardée dans config.json) ; Entrée = mode simulation
~/tradinebotte/run.sh
tail -f ~/tradinebotte/live.log
```

Monitoring : `bash tradinebotte-polymarket/scripts/monitor.sh`  
Redémarrage automatique au reboot : voir [INSTALL.fr.md — configuration systemd](INSTALL.fr.md#démarrage-automatique-avec-systemd-recommandé-pour-les-serveurs-dédiés)

**Arrêt :** `kill $(cat ~/tradinebotte/live.pid)` · ou `systemctl --user stop tradinebotte-live.service` (unité user, sans sudo)

---

## Autres méthodes d'installation

Voir [INSTALL.fr.md](INSTALL.fr.md) pour :
- **git clone** — recommandé si GitHub est accessible depuis la machine cible
- **rsync** — recommandé pour les serveurs sans git (déploiement depuis une machine de développement locale)
- Détails complets sur la configuration multi-compte (Option B — architecture trois services ZeroMQ)

### Configuration multi-compte (Option B) — résumé

L'Option B exécute trois services systemd utilisateur par déploiement : **indicators**, **feed** et **account_bot**.
Les trois communiquent via des sockets IPC dans `/run/user/$UID/` — aucun conflit de port TCP entre utilisateurs Linux.

Étape admin unique par utilisateur VPS (root requis) :

```bash
sudo loginctl enable-linger <nom_utilisateur_bot>
```

Puis en tant qu'utilisateur bot (sans sudo) :

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user enable --now tradinebotte-indicators.service
systemctl --user enable --now tradinebotte-feed.service
systemctl --user enable --now tradinebotte-account.service
```

Procédure complète (modèles de fichiers d'unité, installation de tradinetools, configuration) : [INSTALL.fr.md — Partage WebSocket multi-bot](INSTALL.fr.md#partage-websocket-multi-bot-option-b--zeromq)
