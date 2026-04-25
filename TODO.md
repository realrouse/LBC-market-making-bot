# TODO — Idées et améliorations futures

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
