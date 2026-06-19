#!/usr/bin/env bash
# install_feed_service.sh — idempotent installer for the systemd --user feed service
# used by the multi-bot ZeroMQ integration test (test_multibot_deploy.sh).
#
# Installs / refreshes tradinebotte-feed.service on the DEDICATED TEST ACCOUNT only,
# bound to a dedicated loopback port (default tcp://127.0.0.1:5567) so it never
# collides with — or silently reads — the production feeds on this shared host
# (feed5m is on :5557, cex_feed on :5563, etc.). Safe to re-run.
#
# What it does on the test account:
#   - pushes feed.py + tradinetools source into ~/tradinebotte
#   - refreshes tradinetools in the .venv site-packages (feed.py imports it)
#   - writes ~/.config/systemd/user/tradinebotte-feed.service (TCP addr via env)
#   - enables user lingering so the service survives between SSH sessions
#   - daemon-reload + enable + restart, then verifies it is active and broadcasting
#
# Usage:
#   bash scripts/install_feed_service.sh
#   TEST_FEED_USER_IDX=6 TEST_FEED_ADDR=tcp://127.0.0.1:5567 bash scripts/install_feed_service.sh
#
# Account resolution mirrors test_multibot_deploy.sh: TEST_FEED_USER_IDX, else the
# dedicated test account TEST_STANDALONE_USER_IDX. Fails closed if the resolved
# account is not the dedicated test account (override: TEST_ALLOW_NONTEST_ACCOUNTS=true).
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

FEED_IDX="${TEST_FEED_USER_IDX:-${TEST_STANDALONE_USER_IDX:-0}}"
FEED_ADDR="${TEST_FEED_ADDR:-tcp://127.0.0.1:5567}"

# Fail-closed: never install a TEST feed unit on a production account (it would bind
# or point at shared loopback ports the production fleet uses). Mirror the guard in
# test_multibot_deploy.sh.
if [[ "${TEST_ALLOW_NONTEST_ACCOUNTS:-false}" != "true" ]]; then
    if [[ -z "${TEST_STANDALONE_USER_IDX:-}" ]]; then
        err "TEST_STANDALONE_USER_IDX unset — cannot confirm a dedicated test account."
        exit 1
    fi
    if [[ "$FEED_IDX" != "$TEST_STANDALONE_USER_IDX" ]]; then
        err "would install on account index $FEED_IDX, not the dedicated test account (index $TEST_STANDALONE_USER_IDX)."
        err "Set TEST_FEED_USER_IDX to the test account, or TEST_ALLOW_NONTEST_ACCOUNTS=true for a deliberate non-prod run."
        exit 1
    fi
fi

U="${USERS[$FEED_IDX]}"; P="${PASSWORDS[$FEED_IDX]}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_OPTS="-p $PORT -o StrictHostKeyChecking=yes -o PreferredAuthentications=password -o ConnectTimeout=20"

_ssh(){ SSHPASS="$P" /usr/bin/sshpass -e ssh $SSH_OPTS "$U@$SERVER" "$@" 2>&1 | grep -v "Warning: Permanently" || true; }
_rsync(){ SSHPASS="$P" /usr/bin/sshpass -e rsync -az --exclude=__pycache__ -e "ssh $SSH_OPTS" "$1" "$U@$SERVER:$2" 2>&1 | grep -vi warning || true; }

echo -e "${YELLOW}▶ feed service install on test account idx $FEED_IDX @ $SERVER (addr $FEED_ADDR)${NC}"

# Remote paths keep a literal ~ — expanded by the REMOTE shell via rsync-over-ssh.
# shellcheck disable=SC2088
R_DIR='~/tradinebotte'

# 1. Push feed code + tradinetools source (the standalone test may have wiped them).
_rsync "$REPO/tradinebotte-polymarket/feed.py" "$R_DIR/feed.py"
_rsync "$REPO/tradinetools/" "$R_DIR/tradinetools/"
ok "pushed feed.py + tradinetools"

# 2. Remote: refresh tradinetools, write the unit, enable linger, (re)start, verify.
_ssh "
set -e
export XDG_RUNTIME_DIR=/run/user/\$(id -u)
VENV=\$HOME/tradinebotte/.venv
[ -x \"\$VENV/bin/python3\" ] || { echo 'MISSING .venv — run the standalone install first'; exit 1; }
PYVER=\$(\$VENV/bin/python3 -c 'import sys;print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')
SITE=\$VENV/lib/python\$PYVER/site-packages
rm -rf \"\$SITE/tradinetools\" && cp -r \"\$HOME/tradinebotte/tradinetools/tradinetools\" \"\$SITE/tradinetools\"
\$VENV/bin/python3 -c 'import tradinetools, websockets, zmq, aiohttp' || { echo 'feed deps missing in .venv'; exit 1; }
echo '  tradinetools refreshed; deps OK'

mkdir -p \$HOME/.config/systemd/user
cat > \$HOME/.config/systemd/user/tradinebotte-feed.service <<UNIT
[Unit]
Description=tradinebotte — integration-test shared feed (ZeroMQ broadcaster)
After=network.target
Wants=network.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
Environment=TRADINEBOTTE_FEED_ADDR=$FEED_ADDR
WorkingDirectory=%h/tradinebotte
ExecStart=%h/tradinebotte/.venv/bin/python3 %h/tradinebotte/feed.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
UNIT
echo '  unit written'

loginctl enable-linger \$(whoami) >/dev/null 2>&1 || echo '  (could not enable linger — service may stop on logout)'
systemctl --user daemon-reload
systemctl --user enable tradinebotte-feed.service >/dev/null 2>&1
systemctl --user reset-failed tradinebotte-feed.service 2>/dev/null || true
systemctl --user restart tradinebotte-feed.service
sleep 6
ST=\$(systemctl --user is-active tradinebotte-feed.service)
echo \"  tradinebotte-feed: \$ST\"
journalctl --user -u tradinebotte-feed.service -n 12 --no-pager 2>/dev/null | sed 's/^/    /' || true
[ \"\$ST\" = active ] || { echo 'feed service not active'; exit 1; }
"
ok "feed service installed and active on test account ($FEED_ADDR)"
