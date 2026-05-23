# tradinebotte — Démarrage rapide

> 🇬🇧 [English version](QUICKSTART.md) · Guide complet : [INSTALL.fr.md](INSTALL.fr.md) · Mise à jour : [UPDATE.fr.md](UPDATE.fr.md)

## Prérequis

- Python 3.8+ sur Linux/Mac (serveur dédié ou machine locale)
- Wallet Polygon EOA — MATIC > 0,1 (gas) et USDC.e > 10 $
- **Pas encore de wallet ?** Appuyer sur Entrée au prompt `setup.py` → mode simulation, aucun ordre réel

---

## Installer depuis une release officielle (tar.gz)

```bash
# Télécharger la dernière release (remplacer v0.44 par la version actuelle)
wget https://github.com/neofutur/tradinebotte/archive/refs/tags/v0.44.tar.gz
tar -xzf v0.44.tar.gz
cd tradinebotte-0.44
bash scripts/install.sh        # détecte les paquets manquants ; demande la langue (E/F)
python3 scripts/setup.py       # demande la langue (sauvegardée dans config.json) ; Entrée = mode simulation
bash scripts/start_bot.sh
tail -f ~/tradinebotte/live.log
```

Monitoring : `bash scripts/monitor.sh`  
Redémarrage automatique au reboot : voir [INSTALL.fr.md — configuration systemd](INSTALL.fr.md#démarrage-automatique-avec-systemd-recommandé-pour-les-serveurs-dédiés)

**Arrêt :** `kill $(cat ~/tradinebotte/live.pid)` · ou `sudo systemctl stop tradinebotte` si systemd

---

## Autres méthodes d'installation

Voir [INSTALL.fr.md](INSTALL.fr.md) pour :
- **git clone** — recommandé si GitHub est accessible depuis la machine cible
- **rsync** — recommandé pour les serveurs sans git (déploiement depuis une machine de développement locale)
- Détails complets sur la configuration multi-compte (Option B — WebSocket partagé ZeroMQ)
