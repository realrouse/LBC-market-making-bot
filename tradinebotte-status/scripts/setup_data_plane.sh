#!/usr/bin/env bash
# setup_data_plane.sh — idempotent installer for the shared data-plane services that
# run on the infra account (acct-1, index 0 in TEST_USERS). Recreates from git everything that was
# previously hand-created on the host, so a rebuild / reinstall is one command:
#
#   - 15M Polymarket feed env on tradinebotte-feed.service (TRADINEBOTTE_MARKET_TAG_ID)
#   - tradinebotte-feed5m.service  (5M Polymarket feed, TCP :5557)
#   - tradinebotte-cexfeed.service (Binance + MEXC CEX feed, TCP :5563)
#   - tradinetools refreshed in the venv site-packages (cex_feed needs PORT_CEX_FEED)
#   - feed_auto_start=false in the bot config (don't spawn a duplicate feed)
#
# Safe to re-run. Restarts the feeds (consumers reconnect via ZMQ, ~seconds).
#
#   bash tradinebotte-status/scripts/setup_data_plane.sh
#   TRADINEBOTTE_INFRA_IDX=0 bash ...   # infra account index in TEST_USERS (default 0)
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok(){ echo -e "${GREEN}✓ $*${NC}"; }; warn(){ echo -e "${YELLOW}! $*${NC}"; }
err(){ echo -e "${RED}✗ $*${NC}" >&2; }

CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
[[ -f "$CONF" ]] || { err "missing config: $CONF"; exit 2; }
# shellcheck source=/dev/null
source "$CONF"
SERVER="${TEST_SERVER:?TEST_SERVER missing}"; PORT="${TEST_PORT:-22}"
USERS=("${TEST_USERS[@]:?TEST_USERS missing}"); PASSWORDS=("${TEST_PASSWORDS[@]:?}")
IDX="${TRADINEBOTTE_INFRA_IDX:-0}"
U="${USERS[$IDX]}"; P="${PASSWORDS[$IDX]}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SSH_OPTS="-p $PORT -o StrictHostKeyChecking=yes -o PreferredAuthentications=password -o ConnectTimeout=20"

_ssh(){ SSHPASS="$P" /usr/bin/sshpass -e ssh $SSH_OPTS "$U@$SERVER" "$@" 2>&1 | grep -v "Warning: Permanently" || true; }
_rsync(){ SSHPASS="$P" /usr/bin/sshpass -e rsync -az --exclude=__pycache__ -e "ssh $SSH_OPTS" "$1" "$U@$SERVER:$2" 2>&1 | grep -vi warning || true; }

echo -e "${YELLOW}▶ data-plane setup on infra account idx $IDX @ $SERVER${NC}"

# Remote paths keep a literal ~ — expanded by the REMOTE shell via rsync-over-ssh.
# shellcheck disable=SC2088
R_DIR='~/tradinebotte'
# shellcheck disable=SC2088
R_UNIT='~/.config/systemd/user'

# 1. Push code + unit templates + tradinetools source
_rsync "$REPO/tradinebotte-polymarket/feed.py" "$R_DIR/feed.py"
_rsync "$REPO/tradinebotte-cex/cex_feed.py" "$R_DIR/cex_feed.py"
# cex_feed connector closure: it loads binance / mexc / mexc_futures. mexc spot is a
# protobuf WS, so api_mexc needs mexc_spot_depth_pb2 (+ the protobuf runtime, installed
# in the remote block below).
for _f in api_common.py api_binance.py api_mexc.py api_mexc_futures.py mexc_spot_depth_pb2.py; do
    _rsync "$REPO/tradinebotte-cex/$_f" "$R_DIR/$_f"
done
_rsync "$REPO/tradinebotte-core/botcore/" "$R_DIR/botcore/"
_rsync "$REPO/tradinebotte-cex/connectors/" "$R_DIR/connectors/"
_rsync "$REPO/systemd/tradinebotte-feed5m.service" "$R_UNIT/tradinebotte-feed5m.service"
_rsync "$REPO/systemd/tradinebotte-cexfeed.service" "$R_UNIT/tradinebotte-cexfeed.service"
_rsync "$REPO/tradinetools/" "$R_DIR/tradinetools/"
ok "pushed feed.py, cex_feed.py + connectors (+ mexc spot pb), unit templates, tradinetools"

# 2. Remote: refresh tradinetools site-packages, env/config, (re)start services
_ssh '
set -e
export XDG_RUNTIME_DIR=/run/user/$(id -u)
VENV=$HOME/tradinebotte/.venv
PYVER=$($VENV/bin/python3 -c "import sys;print(f\"{sys.version_info.major}.{sys.version_info.minor}\")")
SITE=$VENV/lib/python$PYVER/site-packages
rm -rf "$SITE/tradinetools" && cp -r "$HOME/tradinebotte/tradinetools/tradinetools" "$SITE/tradinetools"
$VENV/bin/python3 -c "from tradinetools.zmq import PORT_CEX_FEED" || { echo "tradinetools still stale"; exit 1; }
echo "  tradinetools refreshed (PORT_CEX_FEED importable)"

# protobuf runtime for MEXC spot decode (cex_feed). The venv may lack pip → ensurepip.
if ! $VENV/bin/python3 -c "import google.protobuf" 2>/dev/null; then
  $VENV/bin/python3 -m pip --version >/dev/null 2>&1 || $VENV/bin/python3 -m ensurepip --upgrade >/dev/null 2>&1 || true
  $VENV/bin/python3 -m pip install --quiet "protobuf>=5,<6" 2>&1 | tail -1 || true
fi
( cd "$HOME/tradinebotte" && $VENV/bin/python3 -c "import google.protobuf, mexc_spot_depth_pb2" ) 2>/dev/null \
  && echo "  protobuf + mexc_spot_depth_pb2 OK (mexc spot decode ready)" \
  || echo "  WARN: protobuf/_pb2 not importable — mexc spot will not decode"

UNIT=$HOME/.config/systemd/user/tradinebotte-feed.service
if [ -f "$UNIT" ] && ! grep -q TRADINEBOTTE_MARKET_TAG_ID "$UNIT"; then
  sed -i "/^\[Service\]/a Environment=TRADINEBOTTE_MARKET_TAG_ID=102467" "$UNIT"
  echo "  15M env added to feed.service"
fi
python3 - <<PY
import json, os
p = os.path.expanduser("~/tradinebotte/config.json")
c = json.load(open(p)) if os.path.exists(p) else {}
c["feed_auto_start"] = False
json.dump(c, open(p, "w"), indent=2)
print("  feed_auto_start=false")
PY
mkdir -p "$HOME/feed5m"

systemctl --user daemon-reload
systemctl --user enable tradinebotte-feed5m.service tradinebotte-cexfeed.service >/dev/null 2>&1
systemctl --user reset-failed tradinebotte-feed5m.service tradinebotte-cexfeed.service 2>/dev/null || true
systemctl --user restart tradinebotte-feed.service tradinebotte-feed5m.service tradinebotte-cexfeed.service
sleep 6
for s in feed feed5m cexfeed; do
  echo "  tradinebotte-$s: $(systemctl --user is-active tradinebotte-$s.service)"
done
'
ok "data-plane setup complete"
warn "verify consumers reconnected: bash tradinebotte-status/scripts/bot_status.sh"
warn "a feed needs up to one market window to resume after restart; the feed-watchdog"
warn "(tradinebotte-feedwatchdog.timer) auto-restarts it if it stays alive-but-silent."
