"""
botcore — the neutral bot core (Plan D step 3 → Plan C).

A strategy-agnostic package that imports nothing from any exchange plugin
(Polymarket or CEX). It holds the shared interfaces and machinery that both
plugins depend on. Step 3 grows this incrementally; today it carries the
`Strategy` protocol, the single seam every strategy conforms to.

Deployed flat alongside the bot (copied into INSTALL_DIR like `connectors/` and
`strategy_engines/`), so `import botcore` resolves both in the monorepo and on a
deployed account.
"""

from botcore.strategy import Strategy

__all__ = ["Strategy"]
