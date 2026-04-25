# TODO — Idées et améliorations futures

## Roadmap v0.3

### Opérationnel / infrastructure
- ~~**systemd unit**~~ — ✅ done (`scripts/tradinebotte.service` + `scripts/install_service.sh`)
- **Notifications Telegram** — alerte sur chaque trade, déclenchement du stop-loss journalier, reconnexion WebSocket
- **Health-check HTTP** — mini serveur local (ex. port 9090) répondant avec les stats brutes ; monitorable depuis un reverse proxy ou un cron externe

### Stratégie / risk management
- **Sizing dynamique** — Kelly fractionnel sur la taille de mise plutôt que $10 fixe ; adapte le risque à la confiance du signal
- **Filtre heure/jour** — la volatilité BTC présente des patterns horaires ; à mesurer sur les snapshots puis backtester avant d'activer
- **Stop-loss hebdomadaire** — complément au stop-loss journalier pour limiter les séries de pertes sur plusieurs jours

### Backtest / analyse
- **Sharpe / Sortino ratio** — métriques manquantes dans le rapport actuel ; importantes pour comparer des stratégies
- **Walk-forward optimization** — entraîne sur N semaines, valide sur la suivante, glisse la fenêtre ; réduit le risque d'overfitting du `--sweep`
- **Collecte automatique de snapshots** — en mode `--simulate`, enregistrer les snapshots en continu pour enrichir le dataset de backtest

### Qualité de code
- ~~**Fermeture des connexions SQLite dans les tests**~~ — ✅ done (setUp/tearDown + addCleanup sur toutes les classes)
- ~~**mypy strict**~~ — ✅ done (0 erreur, workflow CI `.github/workflows/mypy.yml`)

---

## Exchanges alternatifs

- **Kalshi** — marchés d'événements binaires (US), API REST+WS documentée,
  structure très proche de Polymarket (CLOB binaire, résolution YES/NO).
  Candidat prioritaire pour un deuxième `api_kalshi.py`.

- **MEXC** — leur produit "Prediction Markets" (beta mars 2026) est
  structurellement similaire à Polymarket, mais n'a pas d'API publique
  documentée à ce jour. À réévaluer quand l'API sera disponible.
  Note : leur WebSocket spot/futures utilise protobuf (pas JSON).

## Découverte des marchés

- **Polling prédictif** (option 2) — au lieu de poller toutes les 30 s,
  calculer l'heure exacte d'entrée du prochain marché dans la fenêtre ±6 min
  (`next_boundary = ceil(now/300)*300 - 360`) et scheduler un poll ciblé.
  Élimine le délai résiduel de 30 s sans surcharge API.
