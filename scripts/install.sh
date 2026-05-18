#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  POLYMARKET LIVE BOT v3 — Installation (Linux/Mac)
#
#  Root prerequisites (once per machine, if missing):
#    sudo apt-get install -y python3 python3-venv python3.X-venv sqlite3
#  This script detects missing packages and prints the exact command to run.
#
#  Installation directory (in order of priority):
#    1. Positional argument: bash scripts/install.sh ~/tradinebotte
#    2. Environment variable: TRADINEBOTTE_DIR=~/tradinebotte bash scripts/install.sh
#    3. Default value: ~/tradinebotte
#
#  Options:
#    --lang EN|FR   language (default: interactive prompt)
#    --with-tests   also copies tests/ and scripts/backtest.py
# ═══════════════════════════════════════════════════════════════════
set -eo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

# ── Argument parsing ──────────────────────────────────────────────
WITH_TESTS=0
LANG_ARG=""
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-tests) WITH_TESTS=1; shift ;;
        --lang)       LANG_ARG="${2:-}"; shift 2 ;;
        --lang=*)     LANG_ARG="${1#--lang=}"; shift ;;
        *)            POSITIONAL+=("$1"); shift ;;
    esac
done

INSTALL_DIR="${POSITIONAL[0]:-${TRADINEBOTTE_DIR:-$HOME/tradinebotte}}"
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"   # safe tilde expansion (no eval)

# ── Language selection ────────────────────────────────────────────
# install.sh runs before setup.py so there is no config.json yet;
# the choice is asked interactively unless --lang is passed.
if [ -n "$LANG_ARG" ]; then
    case "$LANG_ARG" in
        [Ff]*) LANG="FR" ;;
        *)     LANG="EN" ;;
    esac
else
    echo "Language / Langue :  [E] English   [F] Français"
    read -r _lang_raw || _lang_raw="E"
    case "$_lang_raw" in
        [Ff]*) LANG="FR" ;;
        *)     LANG="EN" ;;
    esac
fi

# _t "EN text" "FR text" — print the string for the current language (no trailing newline)
_t() { [ "$LANG" = "FR" ] && printf '%s' "$2" || printf '%s' "$1"; }

# ── Helpers ───────────────────────────────────────────────────────
_check_syntax() {
    local file="$1"
    "$INSTALL_DIR/venv/bin/python3" -c \
        "import ast, sys; ast.parse(open(sys.argv[1]).read()); print(sys.argv[2])" \
        "$file" "$(basename "$file") : $(_t 'SYNTAX OK' 'SYNTAXE OK')"
}

_pip_install() {
    local pip="$INSTALL_DIR/venv/bin/pip"
    "$pip" install --quiet --upgrade pip
    "$pip" install --quiet "$@" -r "$REPO_DIR/requirements.txt"
}

# ── System dependencies ───────────────────────────────────────────
echo ""
echo "=== $(_t "Installation directory" "Répertoire d'installation") : $INSTALL_DIR ==="
echo "=== $(_t "Checking system dependencies" "Vérification des dépendances système") ==="

_MISSING=()
if ! command -v python3 &>/dev/null; then
    _MISSING+=("python3")
fi
if command -v python3 &>/dev/null && ! python3 -c "import ensurepip" &>/dev/null 2>&1; then
    _PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
    _MISSING+=("python3-venv" "python3.${_PY_MINOR}-venv")
fi

if [ ${#_MISSING[@]} -gt 0 ]; then
    echo ""
    echo "$(_t "ERROR: missing system packages. Run this as root (once per machine):" \
              "ERREUR : paquets système manquants. Lance en root (une seule fois par machine) :")"
    echo ""
    echo "  sudo apt-get install -y ${_MISSING[*]}"
    echo ""
    exit 1
fi

# sqlite3 CLI — optional: only needed by monitor.sh for manual queries.
if ! command -v sqlite3 &>/dev/null; then
    echo "$(_t "Warning: sqlite3 CLI missing — monitor.sh will not work." \
              "Avertissement : sqlite3 CLI absent — monitor.sh ne fonctionnera pas.")"
    echo "  $(_t "To install:" "Pour l'installer :") sudo apt-get install -y sqlite3"
fi

echo "$(_t "System dependencies OK." "Dépendances système OK.")"

# ── Directories and bot files ─────────────────────────────────────
echo "=== $(_t "Creating directories" "Création des répertoires") ==="
mkdir -p "$INSTALL_DIR"

echo "=== $(_t "Copying bot files" "Copie du bot") ==="
for _f in live_bot.py api_polymarket.py api_binance.py api_mexc.py bot_utils.py; do
    cp "bot/$_f" "$INSTALL_DIR/$_f"
done

mkdir -p "$INSTALL_DIR/connectors"
cp bot/connectors/__init__.py "$INSTALL_DIR/connectors/__init__.py"

mkdir -p "$INSTALL_DIR/strategies"
cp bot/strategies/__init__.py "$INSTALL_DIR/strategies/__init__.py"
cp bot/strategies/grid.py     "$INSTALL_DIR/strategies/grid.py"
_STRAT_SRC="$(cd strategies && pwd)"
_STRAT_DST="$(cd "$INSTALL_DIR/strategies" && pwd)"
if [ "$_STRAT_SRC" != "$_STRAT_DST" ]; then
    cp strategies/*.json "$INSTALL_DIR/strategies/"
fi

# ── Python virtual environment ────────────────────────────────────
if [ -d "$INSTALL_DIR/venv" ]; then
    echo "=== $(_t "Updating Python packages (existing venv)" "Mise à jour des packages Python (venv existant)") ==="
    _pip_install --upgrade
else
    echo "=== $(_t "Creating virtual environment" "Création de l'environnement virtuel") ==="
    python3 -m venv "$INSTALL_DIR/venv"
    echo "=== $(_t "Installing Python packages" "Installation des packages Python") ==="
    _pip_install
fi

# Convenience wrapper that exports TRADINEBOTTE_DIR and activates the venv
cat > "$INSTALL_DIR/run.sh" << EOF
#!/bin/bash
export TRADINEBOTTE_DIR="$INSTALL_DIR"
source "$INSTALL_DIR/venv/bin/activate"
python3 "$INSTALL_DIR/live_bot.py"
EOF
chmod +x "$INSTALL_DIR/run.sh"

# ── Syntax check ──────────────────────────────────────────────────
echo "=== $(_t "Checking syntax" "Vérification syntaxe") ==="
for _f in live_bot.py api_polymarket.py api_binance.py api_mexc.py bot_utils.py \
          connectors/__init__.py strategies/__init__.py strategies/grid.py; do
    _check_syntax "$INSTALL_DIR/$_f"
done

# ── Optional: tests ───────────────────────────────────────────────
if [ "$WITH_TESTS" = "1" ]; then
    echo "=== $(_t "Copying test files" "Copie des fichiers de test") ==="
    mkdir -p "$INSTALL_DIR/tests" "$INSTALL_DIR/scripts" "$INSTALL_DIR/data"
    if [ "$REPO_DIR" != "$INSTALL_DIR" ]; then
        cp tests/test_bot.py      "$INSTALL_DIR/tests/test_bot.py"
        cp tests/test_backtest.py "$INSTALL_DIR/tests/test_backtest.py"
        cp scripts/backtest.py    "$INSTALL_DIR/scripts/backtest.py"
        cp scripts/run_tests.sh   "$INSTALL_DIR/scripts/run_tests.sh"
        cp data/backtest_sample_btc5m_range_2026.db \
           "$INSTALL_DIR/data/backtest_sample_btc5m_range_2026.db"
    fi
    echo "=== $(_t "Running tests" "Lancement des tests") ==="
    TRADINEBOTTE_DIR="$INSTALL_DIR" "$INSTALL_DIR/venv/bin/python3" \
        -W ignore::ResourceWarning -m unittest discover "$INSTALL_DIR/tests/" -v
fi

# ── Next steps ────────────────────────────────────────────────────
if [ "$INSTALL_DIR" = "$HOME/tradinebotte" ]; then
    _TD=""
else
    _TD="TRADINEBOTTE_DIR=\"$INSTALL_DIR\" "
fi

echo ""
echo "=== $(_t "Installation complete in" "Installation terminée dans") $INSTALL_DIR ==="
echo ""
echo "$(_t "NEXT STEPS:" "ÉTAPES SUIVANTES :")"
echo "1. $(_t "Configure    :" "Configurer       :") ${_TD}python3 \"$REPO_DIR/scripts/setup.py\""
echo "   ($(_t "enter the wallet private key, or Enter without key for simulation mode" \
               "saisir la clé privée du wallet, ou Entrée sans clé pour le mode simulation"))"
echo "2. $(_t "Start the bot:" "Lancer le bot    :") ${_TD}bash \"$REPO_DIR/scripts/start_bot.sh\""
if [ "$WITH_TESTS" = "1" ]; then
    echo ""
    echo "$(_t "Tests   " "Tests   ") : cd \"$INSTALL_DIR\" && ${_TD}venv/bin/python3 -W ignore::ResourceWarning -m unittest discover tests/ -v"
    echo "Backtest: cd \"$INSTALL_DIR\" && ${_TD}venv/bin/python3 \"$REPO_DIR/scripts/backtest.py\""
fi
