# Upstream relationship

**LBC-market-making-bot** is a product extract / fork of:

- **tradinebotte** by neofutur — multi-strategy multi-bot trading platform  
- License: **GNU GPL v3** (see `LICENSE`)

This repository started as a full fork of tradinebotte v0.90 and adds a standalone
package `lbcmm/` focused on **MEXC LBC/USDT** liquidity for the LBC community.

## What we kept

- GPL-3.0 license obligations  
- MEXC connector patterns (precision, LIMIT_MAKER, protobuf depth)  
- BAMM pure planner concepts  
- Operational lessons: sim-by-default, fail-closed precision  

## What we simplified for community use

- No required ZMQ shared services  
- No multi-account inventory / SSH fleet status  
- No Polymarket / Binance-first flows  
- Local GUI + CLI only  

## Attribution (required)

When discussing this bot publicly:

> “I forked @neofutur’s multibot design to build an LBC-only bot.”

neofutur may choose not to share future tradinebotte updates with this fork;
that is compatible with GPL and the stated business model.
