#!/usr/bin/env bash
# run_integration_tests.sh — Lance les tests d'intégration SSH en séquence
#
# Prérequis : ~/.tradinebotte-test.conf configuré (voir test_multibot.conf.example)
#
# Usage :
#   bash scripts/run_integration_tests.sh              # tous les tests
#   bash scripts/run_integration_tests.sh --standalone # seulement test standalone
#   bash scripts/run_integration_tests.sh --multibot   # seulement test multibot
#
# Ces tests requièrent un accès SSH à un serveur de test distant.
# Pour les tests unitaires (sans SSH) : bash scripts/run_tests.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'

RUN_STANDALONE=true
RUN_MULTIBOT=true

while [[ $# -gt 0 ]]; do
    case "$1" in
        --standalone) RUN_MULTIBOT=false ;;
        --multibot)   RUN_STANDALONE=false ;;
        -h|--help)
            grep '^#' "${BASH_SOURCE[0]}" | head -15 | sed 's/^# \?//'
            exit 0 ;;
        *) echo "Argument inconnu : $1"; exit 1 ;;
    esac
    shift
done

PASS=0; FAIL=0; START_TS=$(date +%s)

run_test() {
    local name="$1"; local script="$2"
    echo -e "\n${BOLD}${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  TEST : $name${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
    if bash "$script"; then
        PASS=$((PASS + 1))
        echo -e "\n${GREEN}  ✅ $name : SUCCÈS${NC}"
    else
        FAIL=$((FAIL + 1))
        echo -e "\n${RED}  ❌ $name : ÉCHEC${NC}"
    fi
}

echo -e "${BOLD}tradinebotte — Tests d'intégration SSH${NC}"
echo -e "Config : ${TEST_MULTIBOT_CONF:-$HOME/.tradinebotte-test.conf}"

[[ "$RUN_STANDALONE" == "true" ]] && \
    run_test "Standalone multi-user (Option A)" "$SCRIPT_DIR/test_standalone_deploy.sh"

[[ "$RUN_MULTIBOT" == "true" ]] && \
    run_test "Multi-bot ZeroMQ (Option B)" "$SCRIPT_DIR/test_multibot_deploy.sh"

ELAPSED=$(( $(date +%s) - START_TS ))
echo -e "\n${BOLD}${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "  TOTAL : $((PASS + FAIL)) test(s) | ✅ $PASS réussi(s) | ❌ $FAIL échoué(s) | ${ELAPSED}s"
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

[[ $FAIL -eq 0 ]] && exit 0 || exit 1
