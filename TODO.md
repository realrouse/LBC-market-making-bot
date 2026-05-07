# TODO — Idées et améliorations futures

## Roadmap v0.3

### Opérationnel / infrastructure
- ~~**systemd unit**~~ — ✅ done (`scripts/tradinebotte.service` + `scripts/install_service.sh`)
- **Notifications Telegram** — alerte sur chaque trade, déclenchement du stop-loss journalier, reconnexion WebSocket
- **Health-check HTTP** — mini serveur local (ex. port 9090) répondant avec les stats brutes ; monitorable depuis un reverse proxy ou un cron externe
  > 📋 *ameliorationarchitecture.txt item VII (P3, ~15 lignes)* — `aiohttp.web` sur `127.0.0.1:8765`, `GET /health` → `{"status":"ok","capital":…,"wins":…,"losses":…,"open_trades":…,"uptime_s":…}`

### Stratégie / risk management
- **Sizing dynamique** — Kelly fractionnel sur la taille de mise plutôt que $10 fixe ; adapte le risque à la confiance du signal
- ~~**Filtre heure/jour**~~ — ✅ done (`hour_filter` dans strategy JSON : plages UTC semaine/weekend, ouverture/fermeture hebdomadaire US)
- **Stop-loss hebdomadaire** — complément au stop-loss journalier pour limiter les séries de pertes sur plusieurs jours

### Backtest / analyse
- **Sharpe / Sortino ratio** — métriques manquantes dans le rapport actuel ; importantes pour comparer des stratégies
- **Walk-forward optimization** — entraîne sur N semaines, valide sur la suivante, glisse la fenêtre ; réduit le risque d'overfitting du `--sweep`
- **Collecte automatique de snapshots** — en mode `--simulate`, enregistrer les snapshots en continu pour enrichir le dataset de backtest

### Robustesse API
- **Circuit-breaker sur les échecs d'ordre CLOB** — compter les échecs consécutifs dans `BotState` (`api_fail_streak`, `api_cooldown_until`) ; suspendre les entrées pendant 5 min après N échecs (ex. 3) ; logger un WARN ; réinitialiser sur le premier succès
  > 📋 *ameliorationarchitecture.txt item III (P2, ~20 lignes)*
- **Versionnage de schéma DB** — table `schema_version` + dict `MIGRATIONS = {1: "ALTER TABLE …", 2: …}` ; `init_db()` applique les migrations manquantes dans l'ordre ; garantit la compatibilité ascendante sur les VPS avec DB existante
  > 📋 *ameliorationarchitecture.txt item IV (P2, ~30 lignes)*

### Maintenabilité
- **Dédupliquer `live_bot.py` / `account_bot.py`** — extraire la logique partagée (purge des marchés expirés, traitement book/market) dans `bot_state_manager.py` (option A, ambiteuse) ou factoriser au minimum la boucle de purge dans `bot_utils.py` (option B)
  > 📋 *ameliorationarchitecture.txt item V (P3, effort élevé option A / moyen option B)*

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
