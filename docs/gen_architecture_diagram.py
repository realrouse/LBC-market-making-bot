#!/usr/bin/env python3
"""Three-ring architecture: Engine · Bots · Data Sources (WS left / REST right)."""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Circle
import numpy as np

# ── Palette ───────────────────────────────────────────────────────────────────
BG       = "#0d1117"
COL_DIV  = "#161b22"
C_ENGINE = "#f0883e"
C_POLY   = "#58a6ff"
C_CEX    = "#3fb950"
C_BOT    = "#e3b341"
C_SRC    = "#8b949e"
C_IND    = "#bc8cff"
C_TCP    = "#f85149"
C_IPC    = "#58a6ff"
C_REG    = "#d2a8ff"
C_WHITE  = "#e6edf3"
C_DIM    = "#6e7681"

# ── Geometry ──────────────────────────────────────────────────────────────────
W, H   = 36, 26
LCOL   = 6.8          # left column width
CX, CY = 21.0, 12.5   # diagram center

R_ENG  = 3.8          # ENGINE ring boundary
R_BOT  = 8.2          # BOTS ring boundary
R_SRC  = 12.0         # DATA SOURCES ring boundary
r_bot  = 6.0          # bot node positions
r_src  = 10.4         # API node positions

fig, ax = plt.subplots(figsize=(W, H))
fig.patch.set_facecolor(BG)
ax.set_facecolor(BG)
ax.set_xlim(0, W);  ax.set_ylim(0, H);  ax.axis("off")

# ── Helpers ───────────────────────────────────────────────────────────────────
def pol(r, deg):
    a = np.radians(deg)
    return CX + r*np.cos(a), CY + r*np.sin(a)

def rbox(ax, cx, cy, w, h, fc, alpha=0.28, r=0.15, zorder=4, lw=1.6, ec=None):
    ax.add_patch(FancyBboxPatch(
        (cx-w/2, cy-h/2), w, h,
        boxstyle=f"round,pad=0.04,rounding_size={r}",
        facecolor=fc, edgecolor=(ec or fc),
        alpha=alpha, linewidth=lw, zorder=zorder))

def txt(ax, x, y, s, size=8, color=C_WHITE, bold=False, zorder=9,
        ha="center", va="center", italic=False):
    ax.text(x, y, s, ha=ha, va=va, fontsize=size, color=color,
            fontweight="bold" if bold else "normal",
            style="italic" if italic else "normal",
            fontfamily="monospace", zorder=zorder)

def arr(ax, x0, y0, x1, y1, color, lw=1.2, ls="-", rad=0.0,
        label="", ss=0.0, se=0.0):
    dx, dy = x1-x0, y1-y0
    dist = np.hypot(dx, dy)
    if dist < 0.1:
        return
    ux, uy = dx/dist, dy/dist
    ax.annotate("",
        xy=(x1-ux*se, y1-uy*se), xytext=(x0+ux*ss, y0+uy*ss),
        arrowprops=dict(arrowstyle="-|>", color=color, lw=lw,
                        linestyle=ls, connectionstyle=f"arc3,rad={rad}"))
    if label:
        mx = (x0+ux*ss + x1-ux*se)/2 - uy*0.28
        my = (y0+uy*ss + y1-uy*se)/2 + ux*0.28
        ax.text(mx, my, label, ha="center", va="center", fontsize=6.5,
                color=color, fontfamily="monospace", zorder=11,
                bbox=dict(facecolor=BG, edgecolor="none", alpha=0.82, pad=1.5))

def ring_badge(r, color, label, deg=90, alpha=0.30):
    ax.add_patch(Circle((CX,CY), r, fill=False, edgecolor=color,
                         linewidth=1.6, alpha=alpha, linestyle=(0,(6,4)), zorder=2))
    ax.add_patch(Circle((CX,CY), r, facecolor=color, edgecolor="none",
                         alpha=0.03, zorder=0))
    bx, by = pol(r, deg)
    ax.text(bx, by, f"  {label}  ", ha="center", va="center", fontsize=9,
            fontweight="bold", color=color, fontfamily="monospace", zorder=12,
            bbox=dict(facecolor=BG, edgecolor=color, alpha=0.9, lw=1.4,
                      pad=5, boxstyle="round,pad=0.4"))

# ═════════════════════════════════════════════════════════════════════════════
# LEFT COLUMN — legend + ZMQ port reference
# ═════════════════════════════════════════════════════════════════════════════
ax.add_patch(plt.Rectangle((0,0), LCOL, H, facecolor=COL_DIV, edgecolor="none", zorder=0))
ax.axvline(x=LCOL, color="#30363d", linewidth=1.2, zorder=2)

leg_top = H - 0.85
txt(ax, LCOL/2, leg_top, "Légende", size=11, bold=True)

legend_rows = [
    ("ring", C_ENGINE, "ENGINE — feed · indicators · status"),
    ("ring", C_BOT,    "BOTS — Polymarket · CEX"),
    ("ring", C_SRC,    "DATA SOURCES — external APIs"),
    ("o",    C_POLY,   "Polymarket services / bots"),
    ("o",    C_CEX,    "CEX bots"),
    ("o",    C_IND,    "Indicators / pipeline"),
    ("o",    C_ENGINE, "Heartbeat & monitoring"),
    ("->",   C_IPC,    "ZMQ IPC (same OS user)"),
    ("->",   C_TCP,    "ZMQ TCP :5562 (cross-user)"),
    ("->",   C_REG,    "ZMQ REQ/REP :5561"),
    ("--",   C_TCP,    "Heartbeat → status_collector"),
    ("--",   C_IPC,    "Direct API access (Option A)"),
]
for j, (mk, c, lbl) in enumerate(legend_rows):
    ey = leg_top - 0.80 - j*0.57
    if mk == "ring":
        ax.plot(0.55, ey, "o", color=c, ms=12,
                markerfacecolor="none", markeredgewidth=2, zorder=11)
    elif mk == "o":
        ax.plot(0.55, ey, "o", color=c, ms=9, zorder=11)
    elif mk == "->":
        ax.annotate("", xy=(1.05, ey), xytext=(0.25, ey),
                    arrowprops=dict(arrowstyle="-|>", color=c, lw=1.8))
    else:
        ax.plot([0.25, 1.05], [ey, ey], color=c, lw=1.5,
                linestyle=(0,(4,2)), zorder=11)
    txt(ax, 1.25, ey, lbl, size=7.5, color=C_DIM, ha="left")

sep_y = leg_top - 0.80 - len(legend_rows)*0.57 - 0.25
ax.axhline(y=sep_y, xmin=0, xmax=LCOL/W, color="#30363d", lw=0.8, zorder=3)
port_title_y = sep_y - 0.52
txt(ax, LCOL/2, port_title_y, "Ports ZMQ", size=11, bold=True)

ports = [
    (":5557", "PUB/SUB", "IPC",  C_IPC,  "feed → account_bot\n    + indicators"),
    (":5558", "PUB",     "IPC",  C_IPC,  "feed — alternate address"),
    (":5559", "PUB/SUB", "IPC",  C_IPC,  "indicators → CEX bots"),
    (":5561", "REP/REQ", "IPC",  C_REG,  "indicators REP\n    ← account_bot REQ"),
    (":5562", "PULL",    "TCP",  C_TCP,  "status_collector ←\n    ALL bots (heartbeat)"),
]
PORT_BOX_H = 1.22     # tall box — all text inside
PORT_STEP  = 1.68     # vertical spacing between port boxes
for j, (port, pat, tr, c, desc) in enumerate(ports):
    yc = port_title_y - 0.82 - j*PORT_STEP
    rbox(ax, LCOL/2, yc, 6.0, PORT_BOX_H, c, alpha=0.30, r=0.14, zorder=5, lw=1.6)
    txt(ax, LCOL/2, yc + 0.34, f"{port}  {pat}  ({tr})",
        size=8.5, bold=True, color=c)
    lines = desc.split("\n")
    txt(ax, LCOL/2, yc - 0.04, lines[0], size=7.8, color=C_DIM)
    if len(lines) > 1:
        txt(ax, LCOL/2, yc - 0.42, lines[1], size=7.8, color=C_DIM)

# ═════════════════════════════════════════════════════════════════════════════
# TITLE
# ═════════════════════════════════════════════════════════════════════════════
cx_t = (LCOL + W) / 2
txt(ax, cx_t, H - 0.50, "tradinebotte — Three-Layer Architecture", size=14, bold=True)
txt(ax, cx_t, H - 0.98,
    "ENGINE (processing)  ·  BOTS (execution)  ·  DATA SOURCES (external APIs)",
    size=8.5, color=C_DIM)

# ═════════════════════════════════════════════════════════════════════════════
# RING BOUNDARIES + GROUP BADGES
# ═════════════════════════════════════════════════════════════════════════════
ring_badge(R_SRC, C_SRC,    "DATA SOURCES", deg=90, alpha=0.28)
ring_badge(R_BOT, C_BOT,    "BOTS",         deg=90, alpha=0.35)
ring_badge(R_ENG, C_ENGINE, "ENGINE",        deg=90, alpha=0.55)

# Section badges inside the DATA SOURCES ring: WS on left, REST on right
for label, deg in [("  WebSocket  ", 153), ("  REST / RPC  ", 355)]:
    lx, ly = pol(R_SRC + 0.72, deg)
    ax.text(lx, ly, label, ha="center", va="center", fontsize=8.5,
            fontweight="bold", color=C_SRC, fontfamily="monospace", zorder=12,
            bbox=dict(facecolor=BG, edgecolor=C_SRC, alpha=0.85, lw=1.0,
                      pad=4, boxstyle="round,pad=0.35"))

# ═════════════════════════════════════════════════════════════════════════════
# ENGINE — triangle layout
#   status_collector  top (CX, CY+1.55)
#   feed              bottom-left (CX-2.0, CY-0.85)
#   indicators        bottom-right (CX+2.0, CY-0.85)
# ═════════════════════════════════════════════════════════════════════════════
SC_X, SC_Y   = CX,        CY + 1.55
FD_X,  FD_Y  = CX - 2.00, CY - 0.85
IND_X, IND_Y = CX + 2.00, CY - 0.85
eng = {"sc": (SC_X, SC_Y), "fd": (FD_X, FD_Y), "ind": (IND_X, IND_Y)}

# status_collector
rbox(ax, SC_X, SC_Y, 3.4, 1.80, C_ENGINE, alpha=0.42, r=0.25, zorder=6, lw=2.0)
txt(ax, SC_X, SC_Y+0.55,  "status_collector.py",         size=9.5, bold=True, color=C_ENGINE)
txt(ax, SC_X, SC_Y+0.15,  "PULL bind :5562 (TCP)",        size=8.5, bold=True, color=C_TCP)
txt(ax, SC_X, SC_Y-0.25,  "heartbeat.db — all accounts",  size=7.5, color=C_DIM)
txt(ax, SC_X, SC_Y-0.62,  "tradinebotte-status.service",  size=7.0, color=C_DIM, italic=True)

# feed.py
rbox(ax, FD_X, FD_Y, 2.85, 1.65, C_POLY, alpha=0.38, r=0.20, zorder=6, lw=1.8)
txt(ax, FD_X, FD_Y+0.52, "feed.py",                  size=9,   bold=True, color=C_POLY)
txt(ax, FD_X, FD_Y+0.12, "PUB :5557  PUB :5558",     size=7.5, bold=True, color=C_IPC)
txt(ax, FD_X, FD_Y-0.28, "PUSH :5562 (HB)",          size=7.2, color=C_DIM)
txt(ax, FD_X, FD_Y-0.62, "tradinebotte-feed.service", size=6.5, color=C_DIM, italic=True)

# indicators.py
rbox(ax, IND_X, IND_Y, 2.85, 1.65, C_IND, alpha=0.38, r=0.20, zorder=6, lw=1.8)
txt(ax, IND_X, IND_Y+0.52, "indicators.py",                    size=9,   bold=True, color=C_IND)
txt(ax, IND_X, IND_Y+0.12, "SUB :5557  REP :5561",             size=7.5, bold=True, color=C_IPC)
txt(ax, IND_X, IND_Y-0.28, "PUB :5559  PUSH :5562",            size=7.2, color=C_DIM)
txt(ax, IND_X, IND_Y-0.62, "tradinebotte-indicators.service",  size=6.5, color=C_DIM, italic=True)

# Internal ZMQ: feed → indicators :5557
arr(ax, FD_X+1.42, FD_Y, IND_X-1.42, IND_Y,
    C_IPC, lw=1.6, rad=-0.35, label=":5557 PUB→SUB")

# ═════════════════════════════════════════════════════════════════════════════
# BOTS — 3 generic bots
#   135°  Bot Polymarket Option B
#     0°  Bot Polymarket Option A
#   255°  Bot CEX
# ═════════════════════════════════════════════════════════════════════════════
bots_def = [
    (135, "botB",   "Bot Polymarket", "(Option B)  account_bot.py",
     "SUB :5557  REQ :5561\nPUSH :5562 (HB)  live.db",   C_POLY),
    (  0, "botA",   "Bot Polymarket", "(Option A)  live_bot.py",
     "WS direct  CLOB REST\nPUSH :5562 (HB)  live.db",   C_POLY),
    (255, "botCEX", "Bot CEX",        "orderbook / accumulation",
     "SUB :5559  PUSH :5562 (HB)\nlive_ob.db  live_accum.db", C_CEX),
]

bot_pos = {}
for deg, key, l1, l2, l3, col in bots_def:
    bx, by = pol(r_bot, deg)
    bot_pos[key] = (bx, by)
    rbox(ax, bx, by, 3.3, 2.55, col, alpha=0.30, r=0.22, zorder=5, lw=1.8)
    txt(ax, bx, by+0.88, l1, size=10, bold=True, color=col)
    txt(ax, bx, by+0.46, l2, size=8,  color=C_DIM)
    for k, part in enumerate(l3.split("\n")):
        txt(ax, bx, by+0.04-k*0.42, part, size=8, bold=True, color=col)
    # heartbeat spoke → status_collector
    arr(ax, bx, by, SC_X, SC_Y, C_TCP, lw=0.9, ls=(0,(4,3)), ss=1.65, se=1.72)

def eng_to_bot(ek, bk, col, rad=0.0, lw=1.4, ls="-", label=""):
    x0, y0 = eng[ek];  x1, y1 = bot_pos[bk]
    arr(ax, x0, y0, x1, y1, col, lw=lw, rad=rad, ls=ls, label=label, ss=1.42, se=1.65)

eng_to_bot("fd",  "botB",   C_IPC, rad=-0.20, label=":5557 PUB→SUB")
eng_to_bot("fd",  "botA",   C_IPC, rad=0.22,  lw=0.9, ls=(0,(4,2)), label=":5557")
eng_to_bot("ind", "botB",   C_REG, rad=0.20,  label="REQ :5561")
eng_to_bot("ind", "botCEX", C_IPC, rad=-0.22, label=":5559 PUB→SUB")

# ═════════════════════════════════════════════════════════════════════════════
# DATA SOURCES — WebSocket LEFT / REST-RPC RIGHT
#
# WebSocket  (left, ~140°–172°):
#   140° Polymarket WebSocket
#   172° Binance WebSocket
#
# REST / RPC (right, ~283°–64°, clockwise through 0°):
#   283° Polygon RPC
#   305° Fear & Greed
#   328° Deribit REST
#   355° Gamma REST
#    18° CLOB REST
#    45° Binance REST
#    64° Binance Futures REST
# ═════════════════════════════════════════════════════════════════════════════
apis_def = [
    (140, "polywS",  "Polymarket\nWebSocket",   C_POLY),
    (172, "binwS",   "Binance\nWebSocket",      C_CEX),
    (283, "poly",    "Polygon\nRPC",            C_POLY),
    (305, "fg",      "Fear & Greed\nAPI",       C_IND),
    (328, "deribit", "Deribit\nREST",           C_IND),
    (355, "gamma",   "Gamma\nREST",             C_POLY),
    ( 18, "clob",    "CLOB\nREST",              C_POLY),
    ( 45, "brest",   "Binance\nREST",           C_CEX),
    ( 64, "bfut",    "Binance\nFutures REST",   C_CEX),
]

api_pos = {}
for deg, key, name, col in apis_def:
    apx, apy = pol(r_src, deg)
    api_pos[key] = (apx, apy)
    rbox(ax, apx, apy, 2.55, 1.75, col, alpha=0.20, r=0.14, zorder=5, lw=1.2)
    for k, part in enumerate(name.split("\n")):
        txt(ax, apx, apy+0.40-k*0.42, part, size=8.5, bold=(k==0), color=col)

# ── API → component arrows ────────────────────────────────────────────────────
def api_arr(key, target, col, rad=0.05, lw=1.1, ls="-"):
    apx, apy = api_pos[key]
    arr(ax, apx, apy, target[0], target[1], col, lw=lw, rad=rad, ls=ls,
        ss=0.88, se=1.55)

# Polymarket WS → feed (solid) + Bot A (dashed)
api_arr("polywS", (FD_X, FD_Y),     C_POLY, rad=-0.05, lw=1.4)
api_arr("polywS", bot_pos["botA"],   C_POLY, rad=0.12,  lw=0.8, ls=(0,(4,2)))
# Gamma → feed
api_arr("gamma",  (FD_X, FD_Y),     C_POLY, rad=0.10,  lw=1.2)
# CLOB → Bot B (solid) + Bot A (dashed)
api_arr("clob",   bot_pos["botB"],   C_POLY, rad=-0.08, lw=1.2)
api_arr("clob",   bot_pos["botA"],   C_POLY, rad=0.15,  lw=0.7, ls=(0,(4,2)))
# Polygon → Bot B (solid) + Bot A (dashed)
api_arr("poly",   bot_pos["botB"],   C_POLY, rad=0.12,  lw=1.0)
api_arr("poly",   bot_pos["botA"],   C_POLY, rad=0.18,  lw=0.7, ls=(0,(4,2)))
# Binance WS → indicators
api_arr("binwS",  (IND_X, IND_Y),   C_CEX,  rad=-0.08, lw=1.4)
# Binance REST → indicators (solid) + Bot CEX (dashed)
api_arr("brest",  (IND_X, IND_Y),   C_CEX,  rad=0.05,  lw=1.2)
api_arr("brest",  bot_pos["botCEX"], C_CEX,  rad=0.22,  lw=0.7, ls=(0,(4,2)))
# Binance Futures REST → indicators
api_arr("bfut",   (IND_X, IND_Y),   C_CEX,  rad=0.10,  lw=1.2)
# Deribit → indicators
api_arr("deribit",(IND_X, IND_Y),   C_IND,  rad=0.15,  lw=1.0)
# Fear & Greed → indicators
api_arr("fg",     (IND_X, IND_Y),   C_IND,  rad=0.22,  lw=1.0)

# ── Footer ────────────────────────────────────────────────────────────────────
txt(ax, (LCOL+W)/2, 0.28,
    "IPC :5557/:5558/:5559/:5561 (same OS user)  ·  "
    "TCP :5562 heartbeat (cross-user)  ·  systemd --user  ·  loginctl enable-linger",
    size=7.5, color=C_DIM)

# ── Save ─────────────────────────────────────────────────────────────────────
out = "/home/neofutur/tradinebotte/tradinebotte/docs/architecture.png"
plt.tight_layout(pad=0)
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor=BG, edgecolor="none")
plt.close()
print(f"Saved → {out}")
