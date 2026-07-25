#!/usr/bin/env bash
# shellcheck disable=SC1090  # source "$CONF" path is determined at runtime
# update_claude1.sh — Push a code update to the BTC 15M Polymarket account and verify services.
#
# This account is the fleet's data plane (infra only, no trading bot) — four systemd user services:
#   tradinebotte-indicators        — shared indicator pipeline
#   tradinebotte-feed              — shared Polymarket WebSocket ZeroMQ broadcaster
#   tradinebotte-feed5m            — the 5-minute-window feed variant (same feed.py)
#   tradinebotte-cexfeed           — shared CEX book broadcaster (cex_feed.py, port 5563);
#                                    DATA SOURCE for the real-money LBC accumulation bot
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
#   bash scripts/update_claude1.sh --restart-feed5m      # rsync + restart tradinebotte-feed5m
#   bash scripts/update_claude1.sh --restart-cexfeed     # rsync + restart tradinebotte-cexfeed (LBC bot's data source)
#   bash scripts/update_claude1.sh --restart-all-infra   # rsync + restart ALL FOUR infra services
#
# NOTE: the restart flags are the ONLY way version.stamp advances on this account — it is
# written immediately before each restart, so a rsync-only run leaves it (correctly) showing
# the still-running commit. Restart a service to bring both its code AND its stamp current.

set -uo pipefail

LOCAL_REPO_C1="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GIT_HASH=$(git -C "$LOCAL_REPO_C1" rev-parse --short HEAD 2>/dev/null || echo "unknown")
source "$LOCAL_REPO_C1/tradinebotte-status/scripts/record_deploy.sh"

# ControlMaster socket dir — this script chains many ssh/rsync calls to the same acct-1
# infra account; created once up front so every call path (verify-only, restarts, rsync) finds it.
mkdir -p ~/.ssh/cm-sockets && chmod 700 ~/.ssh/cm-sockets

RESTART_INDICATORS=false
RESTART_FEED=false
RESTART_FEED5M=false
RESTART_CEXFEED=false
FORWARD_ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --restart-indicators) RESTART_INDICATORS=true ;;
        --restart-feed)       RESTART_FEED=true ;;
        --restart-feed5m)     RESTART_FEED5M=true ;;
        --restart-cexfeed)    RESTART_CEXFEED=true ;;
        --restart-all-infra)  RESTART_INDICATORS=true; RESTART_FEED=true
                              RESTART_FEED5M=true; RESTART_CEXFEED=true ;;
        *) FORWARD_ARGS+=("$1") ;;
    esac
    shift
done

# Any restart flag set → we are restarting infra services on this account.
_ANY_RESTART=$([[ "$RESTART_INDICATORS" == "true" || "$RESTART_FEED" == "true" \
    || "$RESTART_FEED5M" == "true" || "$RESTART_CEXFEED" == "true" ]] && echo true || echo false)

# --restart-* flags imply --skip-restart (don't touch live_bot)
# unless the caller explicitly passed --skip-restart themselves.
if [[ "$_ANY_RESTART" == "true" ]]; then
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

    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
    echo -e "\n${BOLD}${YELLOW}═══ VERIFY ${c1_user} (indicators + feed) ═══${NC}"

    local out
    out=$(SSHPASS="$c1_pass" /usr/bin/sshpass -e \
        ssh -o StrictHostKeyChecking=yes -o ConnectTimeout=15 -o BatchMode=no \
        -o ServerAliveInterval=10 -o ServerAliveCountMax=3 \
        -o PreferredAuthentications=password \
        -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-sockets/%C -o ControlPersist=10m \
        -p "$port" "${c1_user}@${server}" \
        "export XDG_RUNTIME_DIR=/run/user/\$(id -u)
         echo '=== services ==='
         for svc in tradinebotte-indicators tradinebotte-feed; do
             state=\$(systemctl --user is-active \"\$svc\" 2>/dev/null)
             pid=\$(systemctl --user show \"\$svc\" --property=MainPID --value 2>/dev/null)
             echo \"\$svc: \$state (PID=\$pid)\"
         done" 2>&1)

    echo "$out"

    local issues=0
    for svc in tradinebotte-indicators tradinebotte-feed; do
        if echo "$out" | grep "^${svc}:" | grep -qvE 'active'; then
            echo -e "${RED}  ✗ ${svc}: not running${NC}"
            (( issues++ )) || true
        else
            echo -e "${GREEN}  ✓ ${svc}: running${NC}"
        fi
    done

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
TBNT_SKIP_JOURNAL=1 TEST_STANDALONE_USER_IDX=0 bash "$(dirname "$0")/update_standalone.sh" --skip-verify "${FORWARD_ARGS[@]}"
UPDATE_EXIT=$?

# Abort on rsync failure only; the verify false-negative is expected and ignored.
if [[ "$UPDATE_EXIT" -ne 0 ]] && [[ "$_ANY_RESTART" == "false" ]]; then
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
        -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-sockets/%C -o ControlPersist=10m \
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

# ─── Shared tradinetools rsync+install (runs once for all requested restarts) ──
if [[ "$_ANY_RESTART" == "true" ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
    LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
    source "$CONF"
    _c1_user="${TEST_USERS[0]}"
    _c1_pass="${TEST_PASSWORDS[0]}"
    _server="${TEST_SERVER:?}"
    _port="${TEST_PORT:-22}"
    _install_dir="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
    # ControlMaster: this script chains many ssh/rsync calls to the SAME acct-1 infra
    # account — reuse one authenticated connection instead of the ~13s password-auth
    # cost each time (measured on apollo; same fix as scripts/deploy_actions.py). Socket
    # dir created once near the top of this script.
    _ssh_opts="-p $_port -o StrictHostKeyChecking=yes -o PreferredAuthentications=password -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-sockets/%C -o ControlPersist=10m"

    echo -e "\n${BOLD}${YELLOW}═══ RSYNC tradinetools ═══${NC}"

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
        -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-sockets/%C -o ControlPersist=10m \
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
fi

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
    # ControlMaster: this script chains many ssh/rsync calls to the SAME acct-1 infra
    # account — reuse one authenticated connection instead of the ~13s password-auth
    # cost each time (measured on apollo; same fix as scripts/deploy_actions.py). Socket
    # dir created once near the top of this script.
    _ssh_opts="-p $_port -o StrictHostKeyChecking=yes -o PreferredAuthentications=password -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-sockets/%C -o ControlPersist=10m"

    echo -e "\n${BOLD}${YELLOW}═══ RSYNC indicators ═══${NC}"

    # Push the new indicators.py to the flat install directory
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinebotte-indicators/indicators.py" \
        "$_c1_user@$_server:$_install_dir/indicators.py" 2>&1 \
        && echo -e "${GREEN}  ✓ indicators.py synced${NC}" \
        || { echo -e "${RED}  ✗ rsync indicators.py failed${NC}"; exit 1; }

    # Push the indicator-service configs → remote strategies/indicators/ (the --config
    # path baked into the unit is $_install_dir/strategies/indicators/indicators_all.json).
    # These configs were previously NOT in any deploy step (silent drift — the deployed
    # copy could lag the repo indefinitely); pushing them here closes that pipeline gap.
    echo -e "\n${BOLD}${YELLOW}═══ RSYNC indicators config ═══${NC}"
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        --filter='+ **/' --filter='+ *.json' --filter='- *' \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinebotte-indicators/strategies/" \
        "$_c1_user@$_server:$_install_dir/strategies/indicators/" 2>&1 \
        && echo -e "${GREEN}  ✓ indicators configs synced${NC}" \
        || { echo -e "${RED}  ✗ rsync indicators configs failed${NC}"; exit 1; }

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
    # ControlMaster: this script chains many ssh/rsync calls to the SAME acct-1 infra
    # account — reuse one authenticated connection instead of the ~13s password-auth
    # cost each time (measured on apollo; same fix as scripts/deploy_actions.py). Socket
    # dir created once near the top of this script.
    _ssh_opts="-p $_port -o StrictHostKeyChecking=yes -o PreferredAuthentications=password -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-sockets/%C -o ControlPersist=10m"

    echo -e "\n${BOLD}${YELLOW}═══ RSYNC feed ═══${NC}"

    # Push the new feed.py to the flat install directory
    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/feed.py" \
        "$_c1_user@$_server:$_install_dir/feed.py" 2>&1 \
        && echo -e "${GREEN}  ✓ feed.py synced${NC}" \
        || { echo -e "${RED}  ✗ rsync feed.py failed${NC}"; exit 1; }

    _restart_service "tradinebotte-feed.service" "FEED" "connected|bind|ERROR"
fi

# ─── Restart tradinebotte-feed5m if requested ──────────────────────────────────
# feed5m runs the SAME feed.py as tradinebotte-feed (different unit/args, 5-minute
# window), so it ships the same source file; it just needs its own restart.
if [[ "$RESTART_FEED5M" == "true" ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
    LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
    source "$CONF"
    _c1_user="${TEST_USERS[0]}"
    _c1_pass="${TEST_PASSWORDS[0]}"
    _server="${TEST_SERVER:?}"
    _port="${TEST_PORT:-22}"
    _install_dir="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
    # ControlMaster: this script chains many ssh/rsync calls to the SAME acct-1 infra
    # account — reuse one authenticated connection instead of the ~13s password-auth
    # cost each time (measured on apollo; same fix as scripts/deploy_actions.py). Socket
    # dir created once near the top of this script.
    _ssh_opts="-p $_port -o StrictHostKeyChecking=yes -o PreferredAuthentications=password -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-sockets/%C -o ControlPersist=10m"

    echo -e "\n${BOLD}${YELLOW}═══ RSYNC feed5m ═══${NC}"

    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinebotte-polymarket/feed.py" \
        "$_c1_user@$_server:$_install_dir/feed.py" 2>&1 \
        && echo -e "${GREEN}  ✓ feed.py synced${NC}" \
        || { echo -e "${RED}  ✗ rsync feed.py failed${NC}"; exit 1; }

    _restart_service "tradinebotte-feed5m.service" "FEED5M" "connected|bind|ERROR"
fi

# ─── Restart tradinebotte-cexfeed if requested ─────────────────────────────────
# cexfeed is the shared CEX book broadcaster (cex_feed.py, port 5563). It is the DATA
# SOURCE for the real-money LBC accumulation bot ([[project_lbc_realmoney_bot]]), so a
# restart briefly interrupts its ticks — verify LBCUSDT books flow again afterwards.
if [[ "$RESTART_CEXFEED" == "true" ]]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
    LOCAL_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
    source "$CONF"
    _c1_user="${TEST_USERS[0]}"
    _c1_pass="${TEST_PASSWORDS[0]}"
    _server="${TEST_SERVER:?}"
    _port="${TEST_PORT:-22}"
    _install_dir="${TEST_REMOTE_INSTALL_DIR:-~/tradinebotte}"
    # ControlMaster: this script chains many ssh/rsync calls to the SAME acct-1 infra
    # account — reuse one authenticated connection instead of the ~13s password-auth
    # cost each time (measured on apollo; same fix as scripts/deploy_actions.py). Socket
    # dir created once near the top of this script.
    _ssh_opts="-p $_port -o StrictHostKeyChecking=yes -o PreferredAuthentications=password -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-sockets/%C -o ControlPersist=10m"

    echo -e "\n${BOLD}${YELLOW}═══ RSYNC cexfeed ═══${NC}"

    SSHPASS="$_c1_pass" /usr/bin/sshpass -e \
        rsync -az \
        -e "ssh $_ssh_opts" \
        "$LOCAL_REPO/tradinebotte-cex/cex_feed.py" \
        "$_c1_user@$_server:$_install_dir/cex_feed.py" 2>&1 \
        && echo -e "${GREEN}  ✓ cex_feed.py synced${NC}" \
        || { echo -e "${RED}  ✗ rsync cex_feed.py failed${NC}"; exit 1; }

    _restart_service "tradinebotte-cexfeed.service" "CEXFEED" "PUB on|connected|ERROR"
fi

# ─── Deploy journal: record the account-1 infra units (indicators + feed) (both exit paths) ──
_C1_CONF="${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"
# shellcheck disable=SC1090
source "$_C1_CONF"
_c1_acct="${TEST_USERS[0]}"
_c1_mode=$([[ "$_ANY_RESTART" == "true" ]] && echo restart || echo rsync)
_c1_result=$([[ "${UPDATE_EXIT:-0}" -eq 0 ]] && echo OK || echo FAILED)
for _b in indicators feed; do
    tbnt_record_deploy "$_c1_acct" "$_b" "$_c1_result" "$_c1_mode"
done

# ─── Final verify: always run after any service restart ───────────────────────
if [[ "$_ANY_RESTART" == "true" ]]; then
    sleep 5
    _verify_claude1_multiservice
    exit $?
fi
