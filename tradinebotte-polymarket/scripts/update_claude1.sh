#!/usr/bin/env bash
# update_claude1.sh — Push a code update to the BTC 15M Polymarket account and verify services.
#
# This account runs three systemd user services (NOT a standalone live_bot):
#   tradinebotte-indicators        — shared indicator pipeline
#   tradinebotte-feed              — shared WebSocket ZeroMQ broadcaster
#   tradinebotte-account-<user>    — account_bot subscribing to the feed
#
# Targets TEST_USERS[0] (15M Polymarket collector, tag=102467).
# Retrieve live.db BEFORE any update if you plan to wipe the install.
#
# Usage:
#   bash scripts/update_claude1.sh                    # rsync only (no standalone live_bot on this account)
#   bash scripts/update_claude1.sh --skip-restart     # rsync only, nothing restarted
#   bash scripts/update_claude1.sh --verify-only      # check status of all 3 services
#   bash scripts/update_claude1.sh --restart-indicators  # rsync + restart tradinebotte-indicators
#   bash scripts/update_claude1.sh --restart-feed        # rsync + restart tradinebotte-feed
#   bash scripts/update_claude1.sh --restart-account     # rsync + restart tradinebotte-account-<user>
#   bash scripts/update_claude1.sh --restart-indicators --restart-feed --restart-account  # restart all three services

set -uo pipefail

LOCAL_REPO_C1="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GIT_HASH=$(git -C "$LOCAL_REPO_C1" rev-parse --short HEAD 2>/dev/null || echo "unknown")

RESTART_INDICATORS=false
RESTART_FEED=false
RESTART_ACCOUNT=false
FORWARD_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --restart-indicators) RESTART_INDICATORS=true ;;
        --restart-feed)       RESTART_FEED=true ;;
        --restart-account)    RESTART_ACCOUNT=true ;;
        *) FORWARD_ARGS+=("$1") ;;
    esac
    shift
done

# --restart-* flags imply --skip-restart (don't touch live_bot)
# unless the caller explicitly passed --skip-restart themselves.
if [[ "$RESTART_INDICATORS" == "true" || "$RESTART_FEED" == "true" || "$RESTART_ACCOUNT" == "true" ]]; then
    # Only add --skip-restart if it wasn't already in FORWARD_ARGS
    if ! printf '%s\n' "${FORWARD_ARGS[@]}" | grep -q -- '--skip-restart\|--verify-only'; then
        FORWARD_ARGS+=(--skip-restart)
    fi
fi

# ─── Verify multi-service status for account 0 (replaces update_standalone verify) ──
_verify_claude1_multiservice() {
    local CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
    source "$CONF"
    local server="${TEST_SERVER:?}" port="${TEST_PORT:-22}"
    local c1_user="${TEST_USERS[0]}" c1_pass="${TEST_PASSWORDS[0]}"
    local install_dir="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"

    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
    echo -e "\n${BOLD}${YELLOW}═══ VERIFY ${c1_user} (indicators + feed + account_bot) ═══${NC}"

    local out
    out=$(SSHPASS="$c1_pass" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$port" "${c1_user}@${server}" \
        "export XDG_RUNTIME_DIR=/run/user/\$(id -u)
         echo '=== services ==='
         for svc in tradinebotte-indicators tradinebotte-feed tradinebotte-account-${c1_user}; do
             state=\$(systemctl --user is-active \"\$svc\" 2>/dev/null)
             pid=\$(systemctl --user show \"\$svc\" --property=MainPID --value 2>/dev/null)
             echo \"\$svc: \$state (PID=\$pid)\"
         done
         echo '=== account.log ==='
         tail -15 ${install_dir}/account.log 2>/dev/null || echo '(no account.log)'" 2>&1)

    echo "$out"

    local issues=0
    for svc in tradinebotte-indicators tradinebotte-feed "tradinebotte-account-${c1_user}"; do
        if echo "$out" | grep "^${svc}:" | grep -qvE 'active'; then
            echo -e "${RED}  ✗ ${svc}: not running${NC}"
            (( issues++ )) || true
        else
            echo -e "${GREEN}  ✓ ${svc}: running${NC}"
        fi
    done

    if echo "$out" | grep -qiE '\[ERROR\]|\[CRIT\]'; then
        echo -e "${RED}  ✗ errors found in account.log${NC}"
        (( issues++ )) || true
    else
        echo -e "${GREEN}  ✓ no errors in account.log${NC}"
    fi

    if echo "$out" | grep -q "Connected to feed"; then
        echo -e "${GREEN}  ✓ account_bot connected to feed${NC}"
    fi

    echo -e "\n${BOLD}${YELLOW}═══ RESULT ═══${NC}"
    if [[ $issues -eq 0 ]]; then
        echo -e "${BOLD}${GREEN}  SUCCESS — ${c1_user} services running${NC}"
        return 0
    else
        echo -e "${BOLD}${RED}  FAILURE — ${issues} issue(s) found${NC}"
        return 1
    fi
}

# ─── --verify-only: skip update_standalone entirely, run multi-service verify directly
if printf '%s\n' "${FORWARD_ARGS[@]+"${FORWARD_ARGS[@]}"}" | grep -q -- '--verify-only'; then
    _verify_claude1_multiservice
    exit $?
fi

# ─── Run the standard update (rsync + optional live_bot restart) ──────────────
# Note: update_standalone's VERIFY step reports a false negative on this account
# (no standalone live_bot on this account — services are managed separately below).
TEST_STANDALONE_USER_IDX=0 bash "$(dirname "$0")/update_standalone.sh" --skip-verify "${FORWARD_ARGS[@]}"
UPDATE_EXIT=$?

# Abort on rsync failure only; the verify false-negative is expected and ignored.
if [[ "$UPDATE_EXIT" -ne 0 ]] && \
   [[ "$RESTART_INDICATORS" == "false" && "$RESTART_FEED" == "false" && "$RESTART_ACCOUNT" == "false" ]]; then
    exit "$UPDATE_EXIT"
fi

# ─── Shared helper: restart a systemd service on the deployment account ────────
_restart_service() {
    local svc="$1" label="$2" grep_pat="$3"

    CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
    source "$CONF"
    local server="${TEST_SERVER:?}" port="${TEST_PORT:-22}"
    local c1_user="${TEST_USERS[0]}" c1_pass="${TEST_PASSWORDS[0]}"

    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
    echo -e "\n${BOLD}${YELLOW}═══ RESTART ${label} ═══${NC}"

    local out
    local _install_dir="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
    out=$(SSHPASS="$c1_pass" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$port" "$c1_user@$server" \
        "echo '${GIT_HASH}' > ${_install_dir}/version.stamp; \
         export XDG_RUNTIME_DIR=/run/user/\$(id -u); \
         systemctl --user reset-failed ${svc} 2>/dev/null; \
         systemctl --user restart ${svc} 2>/dev/null \
         && echo 'restarted' \
         && sleep 4 \
         && journalctl --user -u ${svc} --no-pager -n 8 2>/dev/null | grep -E '${grep_pat}'" 2>&1)

    echo "$out"
    if echo "$out" | grep -q "restarted"; then
        echo -e "${GREEN}  ✓ ${svc} restarted${NC}"
    else
        echo -e "${RED}  ✗ ${svc} restart failed — check manually${NC}"
        return 1
    fi
}

# ─── Restart tradinebotte-indicators if requested ──────────────────────────────
if [[ "$RESTART_INDICATORS" == "true" ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
    LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
    source "$CONF"
    _c1_user="${TEST_USERS[0]}"
    _c1_pass="${TEST_PASSWORDS[0]}"
    _server="${TEST_SERVER:?}"
    _port="${TEST_PORT:-22}"
    _install_dir="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
    _ssh_opts="-p $_port -o StrictHostKeyChecking=yes -o PreferredAuthentications=password"

    echo -e "\n${BOLD}${YELLOW}═══ RSYNC indicators + tradinetools ═══${NC}"

    # Push the new indicators.py to the flat install directory
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinebotte-indicators/indicators.py" \
        "$_c1_user@$_server:$_install_dir/indicators.py" 2>&1 \
        && echo -e "${GREEN}  ✓ indicators.py synced${NC}" \
        || { echo -e "${RED}  ✗ rsync indicators.py failed${NC}"; exit 1; }

    # Push tradinetools (service uses .venv, not venv — install separately)
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinetools/" \
        "$_c1_user@$_server:$_install_dir/tradinetools/" 2>&1 \
        && echo -e "${GREEN}  ✓ tradinetools synced${NC}" \
        || { echo -e "${RED}  ✗ rsync tradinetools failed${NC}"; exit 1; }

    # Install tradinetools in the .venv used by the systemd service.
    # .venv may have no pip script — fall back to direct site-packages copy.
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$_port" "$_c1_user@$_server" "
VENV=$_install_dir/.venv
PYVER=\$(\$VENV/bin/python3 -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')
SITE=\$VENV/lib/python\${PYVER}/site-packages
mkdir -p \$SITE
rm -rf \$SITE/tradinetools
cp -r $_install_dir/tradinetools/tradinetools \$SITE/tradinetools
echo 'tradinetools ok'
\$VENV/bin/python3 -c 'from tradinetools.zmq import ipc_socket_dir, make_pub; print(\"import check ok\")' 2>&1
" 2>&1 \
        && echo -e "${GREEN}  ✓ tradinetools installed in .venv${NC}" \
        || echo -e "${RED}  ✗ tradinetools install failed${NC}"

    _restart_service "tradinebotte-indicators" "INDICATORS" "PUB bind|scalping|ERROR"
fi

# ─── Restart tradinebotte-feed if requested ────────────────────────────────────
if [[ "$RESTART_FEED" == "true" ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
    LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
    source "$CONF"
    _c1_user="${TEST_USERS[0]}"
    _c1_pass="${TEST_PASSWORDS[0]}"
    _server="${TEST_SERVER:?}"
    _port="${TEST_PORT:-22}"
    _install_dir="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
    _ssh_opts="-p $_port -o StrictHostKeyChecking=yes -o PreferredAuthentications=password"

    echo -e "\n${BOLD}${YELLOW}═══ RSYNC feed + tradinetools ═══${NC}"

    # Push the new feed.py to the flat install directory
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/feed.py" \
        "$_c1_user@$_server:$_install_dir/feed.py" 2>&1 \
        && echo -e "${GREEN}  ✓ feed.py synced${NC}" \
        || { echo -e "${RED}  ✗ rsync feed.py failed${NC}"; exit 1; }

    # Push tradinetools
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinetools/" \
        "$_c1_user@$_server:$_install_dir/tradinetools/" 2>&1 \
        && echo -e "${GREEN}  ✓ tradinetools synced${NC}" \
        || { echo -e "${RED}  ✗ rsync tradinetools failed${NC}"; exit 1; }

    # Install tradinetools in .venv; fall back to direct copy if pip is absent
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$_port" "$_c1_user@$_server" "
VENV=$_install_dir/.venv
PYVER=\$(\$VENV/bin/python3 -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')
SITE=\$VENV/lib/python\${PYVER}/site-packages
mkdir -p \$SITE
rm -rf \$SITE/tradinetools
cp -r $_install_dir/tradinetools/tradinetools \$SITE/tradinetools
echo 'tradinetools ok'
\$VENV/bin/python3 -c 'from tradinetools.zmq import ipc_socket_dir, make_pub; print(\"import check ok\")' 2>&1
" 2>&1 \
        && echo -e "${GREEN}  ✓ tradinetools installed in .venv${NC}" \
        || echo -e "${RED}  ✗ tradinetools install failed${NC}"

    _restart_service "tradinebotte-feed.service" "FEED" "connected|bind|ERROR"
fi

# ─── Restart tradinebotte-account-tradinebotte if requested ───────────────────
if [[ "$RESTART_ACCOUNT" == "true" ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
    LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
    source "$CONF"
    _c1_user="${TEST_USERS[0]}"
    _c1_pass="${TEST_PASSWORDS[0]}"
    _server="${TEST_SERVER:?}"
    _port="${TEST_PORT:-22}"
    _install_dir="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
    _ssh_opts="-p $_port -o StrictHostKeyChecking=yes -o PreferredAuthentications=password"

    echo -e "\n${BOLD}${YELLOW}═══ RSYNC account_bot + tradinetools ═══${NC}"

    # Push account_bot.py to both the flat install dir and bot/ subdir (service uses bot/)
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/account_bot.py" \
        "$_c1_user@$_server:$_install_dir/account_bot.py" 2>&1 \
        && echo -e "${GREEN}  ✓ account_bot.py synced (flat)${NC}" \
        || { echo -e "${RED}  ✗ rsync account_bot.py failed${NC}"; exit 1; }

    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/account_bot.py" \
        "$_c1_user@$_server:$_install_dir/bot/account_bot.py" 2>&1 \
        && echo -e "${GREEN}  ✓ account_bot.py synced (bot/)${NC}" \
        || echo -e "${YELLOW}  ! bot/ subdir not present — skipping${NC}"

    # Push live_bot.py to bot/ — account_bot imports it from there (sys.path includes bot/)
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/live_bot.py" \
        "$_c1_user@$_server:$_install_dir/bot/live_bot.py" 2>&1 \
        && echo -e "${GREEN}  ✓ live_bot.py synced (bot/)${NC}" \
        || echo -e "${YELLOW}  ! bot/live_bot.py sync skipped${NC}"

    # Push tradinetools
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        --exclude='__pycache__' --exclude='*.pyc' --exclude='*.egg-info' \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinetools/" \
        "$_c1_user@$_server:$_install_dir/tradinetools/" 2>&1 \
        && echo -e "${GREEN}  ✓ tradinetools synced${NC}" \
        || { echo -e "${RED}  ✗ rsync tradinetools failed${NC}"; exit 1; }

    # Install tradinetools in .venv; fall back to direct copy if pip is absent
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -p "$_port" "$_c1_user@$_server" "
VENV=$_install_dir/.venv
PYVER=\$(\$VENV/bin/python3 -c 'import sys; print(f\"{sys.version_info.major}.{sys.version_info.minor}\")')
SITE=\$VENV/lib/python\${PYVER}/site-packages
mkdir -p \$SITE
rm -rf \$SITE/tradinetools
cp -r $_install_dir/tradinetools/tradinetools \$SITE/tradinetools
echo 'tradinetools ok'
\$VENV/bin/python3 -c 'from tradinetools.zmq import ipc_socket_dir, make_pub; print(\"import check ok\")' 2>&1
" 2>&1 \
        && echo -e "${GREEN}  ✓ tradinetools installed in .venv${NC}" \
        || echo -e "${RED}  ✗ tradinetools install failed${NC}"

    _restart_service "tradinebotte-account-${_c1_user}.service" "ACCOUNT BOT" "ACCOUNT BOT|Connected to feed|ERROR"
fi

# ─── Final verify: always run after any service restart ───────────────────────
if [[ "$RESTART_INDICATORS" == "true" || "$RESTART_FEED" == "true" || "$RESTART_ACCOUNT" == "true" ]]; then
    sleep 5
    _verify_claude1_multiservice
    exit $?
fi
