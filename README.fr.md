# tradinebotte

> 🇬🇧 [English version](README.md)

Bot de trading automatisé pour les marchés de prédiction [Polymarket](https://polymarket.com), ciblant les marchés Bitcoin Hausse/Baisse 5 minutes sur Polygon. Utilise une stratégie quantitative basée sur un signal (`best_bid >= 0.96`) backtestée à **98,3% de taux de victoire** sur 1663 trades (avril 2026).

## Stratégie

- Surveille les marchés "Bitcoin Up or Down — 5 minutes" dont `endDate` est dans une fenêtre de ±6 minutes
- Signal d'entrée : `best_bid >= 0.96` sur un token UP ou DOWN
- Exécute un ordre LIMIT BUY au `best_ask` via l'API CLOB de Polymarket
- Résolution : WIN si bid >= 0.99, LOSS si bid <= 0.01, ou à l'expiration du marché (bid >= 0.50 = WIN)
- Stop-loss journalier : 30 $ | Mise par trade : 10 $ | Frais : 2%

## Installation

Voir **[INSTALL.fr](INSTALL.fr)** pour le guide d'installation complet : prérequis, dépendances, configuration du wallet, lancement, monitoring, et comment tester dans un environnement virtuel.

## Notes

- Les timeouts WebSocket (~90s) en période calme sont **normaux** — le bot se reconnecte automatiquement
- Si `POLY_PRIVATE_KEY` n'est pas défini, les ordres sont simulés (aucune exécution on-chain)
- Les signaux peuvent être rares en période de faible volatilité BTC — c'est attendu
- Ne pas modifier `SIGNAL_THRESHOLD` (0.96) sans relancer le backtest complet

## Licence

Voir [LICENSE](LICENSE).
