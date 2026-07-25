# tradinebotte — Démarrage rapide

> 🇬🇧 [English version](QUICKSTART.md) · Guide complet : [INSTALL.fr.md](INSTALL.fr.md) · Mise à jour : [UPDATE.fr.md](UPDATE.fr.md)

## Prérequis

- Python 3.8+ sur Linux/Mac (serveur dédié ou machine locale)
- Selon la famille de stratégie, le trading réel nécessite des credentials
  exchange (voir [docs/going-live.md](docs/going-live.md)) — Polymarket
  nécessite un wallet Polygon EOA (MATIC > 0,1 pour le gas, USDC.e > 10 $) ;
  les stratégies CEX (grid/swing/DCA/accumulation) nécessitent une clé API
  Binance/MEXC/Bitstamp à la place.
- **Pas encore de credentials ?** Chaque famille de stratégie tourne en
  **mode simulation** par défaut (pas de clé = paper trading, aucun ordre
  réel) — rien ici n'est spécifique à Polymarket.

---

## Installer depuis une release officielle (tar.gz)

```bash
# Télécharger la dernière release (remplacer v0.90 par la version actuelle)
wget https://github.com/neofutur/tradinebotte/archive/refs/tags/v0.90.tar.gz
tar -xzf v0.90.tar.gz
cd tradinebotte-0.90
bash scripts/install.sh        # détecte les paquets manquants ; demande la langue (E/F)
python3 scripts/setup.py       # demande la langue (sauvegardée dans config.json) ; Entrée = mode simulation
~/tradinebotte/run.sh
tail -f ~/tradinebotte/live.log
```

Monitoring : `bash tradinebotte-polymarket/scripts/monitor.sh`  
Tableau de bord multi-bot : `python3 tradinebotte-status/generate_status.py` → `~/public_html/tradinebottestatus.html`  
Redémarrage automatique au reboot : voir [INSTALL.fr.md — configuration systemd](INSTALL.fr.md#démarrage-automatique-avec-systemd-recommandé-pour-les-serveurs-dédiés)

**Arrêt :** `kill $(cat ~/tradinebotte/live.pid)` · ou `systemctl --user stop tradinebotte-live.service` (unité user, sans sudo)

---

## Autres méthodes d'installation

Voir [INSTALL.fr.md](INSTALL.fr.md) pour :
- **git clone** — recommandé si GitHub est accessible depuis la machine cible
- **rsync** — recommandé pour les serveurs sans git (déploiement depuis une machine de développement locale)

## Faire tourner plusieurs bots (multi-compte / multi-stratégie)

Chaque bot de trading — quelle que soit la famille de stratégie, sur
autant de comptes que nécessaire — se déploie nativement dans une
arborescence partagée unique `~/tradinebotte/`, pilotée par
`inventory.toml` (une ligne `[[bot]]` par bot, source unique de vérité de
la flotte — locale et ignorée par git, puisqu'elle décrit vos propres
comptes/bots) :

```bash
cp inventory.toml.example inventory.toml                 # une fois, puis éditer pour votre flotte
bash tradinebotte-cex/scripts/deploy_all.sh              # déployer/redéployer toute la flotte
bash tradinebotte-cex/scripts/deploy_all.sh --only <jeton>  # cibler un compte/bot
```

Ajouter un bot en ajoutant une ligne `[[bot]]` dans `inventory.toml`, puis
redéployer. Voir
[INSTALL.fr.md — Multi-bot / multi-compte : déploiement](INSTALL.fr.md#multi-bot--multi-compte--déploiement)
pour le schéma complet et l'architecture "Option B" retirée qu'il remplace.
