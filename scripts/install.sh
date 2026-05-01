#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  POLYMARKET LIVE BOT v3 — Installation (Linux/Mac)
#
#  Prérequis root (une seule fois par machine, si absents) :
#    sudo apt-get install -y python3 python3-venv python3.X-venv sqlite3
#  Ce script détecte les manquants et affiche la commande exacte à lancer.
#
#  Répertoire d'installation (par ordre de priorité) :
#    1. Argument positionnel : bash scripts/install.sh ~/tradinebotte
#    2. Variable d'environnement : TRADINEBOTTE_DIR=~/tradinebotte bash scripts/install.sh
#    3. Valeur par défaut : ~/tradinebotte
#
#  Options :
#    --with-tests   copie aussi tests/ et scripts/backtest.py
# ═══════════════════════════════════════════════════════════════════
set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

WITH_TESTS=0
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--with-tests" ]; then
        WITH_TESTS=1
    else
        ARGS+=("$arg")
    fi
done

INSTALL_DIR="${ARGS[0]:-${TRADINEBOTTE_DIR:-$HOME/tradinebotte}}"
INSTALL_DIR="$(eval echo "$INSTALL_DIR")"   # développe ~ si présent

echo "=== Répertoire d'installation : $INSTALL_DIR ==="

echo "=== Vérification des dépendances système ==="

_MISSING=()

if ! command -v python3 &>/dev/null; then
    _MISSING+=("python3")
fi

# python3 -m venv nécessite ensurepip (paquet python3-venv + python3.X-venv sur Ubuntu)
if command -v python3 &>/dev/null && ! python3 -c "import ensurepip" &>/dev/null 2>&1; then
    _PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
    _MISSING+=("python3-venv" "python3.${_PY_MINOR}-venv")
fi

if [ ${#_MISSING[@]} -gt 0 ]; then
    echo ""
    echo "ERREUR : paquets système manquants. Lance cette commande en root (une seule fois par machine) :"
    echo ""
    echo "  sudo apt-get install -y ${_MISSING[*]}"
    echo ""
    exit 1
fi

# sqlite3 CLI — optionnel : uniquement nécessaire pour monitor.sh (requêtes
# manuelles). Le bot utilise le module Python sqlite3 intégré, toujours dispo.
if ! command -v sqlite3 &>/dev/null; then
    echo "Avertissement : sqlite3 CLI absent — monitor.sh ne fonctionnera pas."
    echo "  Pour l'installer : sudo apt-get install -y sqlite3"
fi

echo "Dépendances système OK."

echo "=== Création des répertoires ==="
mkdir -p "$INSTALL_DIR"

echo "=== Copie du bot ==="
cp bot/live_bot.py       "$INSTALL_DIR/live_bot.py"
cp bot/api_polymarket.py "$INSTALL_DIR/api_polymarket.py"
mkdir -p "$INSTALL_DIR/strategies"
cp strategies/*.json     "$INSTALL_DIR/strategies/"

echo "=== Création de l'environnement virtuel ==="
python3 -m venv "$INSTALL_DIR/venv"

echo "=== Installation des packages Python ==="
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet aiohttp websockets web3 py-clob-client

# Wrapper d'exécution avec TRADINEBOTTE_DIR exportée
cat > "$INSTALL_DIR/run.sh" << EOF
#!/bin/bash
export TRADINEBOTTE_DIR="$INSTALL_DIR"
source "$INSTALL_DIR/venv/bin/activate"
python3 "$INSTALL_DIR/live_bot.py"
EOF
chmod +x "$INSTALL_DIR/run.sh"

echo "=== Vérification syntaxe ==="
"$INSTALL_DIR/venv/bin/python3" -c "import ast; ast.parse(open('$INSTALL_DIR/live_bot.py').read()); print('live_bot.py : SYNTAXE OK')"
"$INSTALL_DIR/venv/bin/python3" -c "import ast; ast.parse(open('$INSTALL_DIR/api_polymarket.py').read()); print('api_polymarket.py : SYNTAXE OK')"

if [ "$WITH_TESTS" = "1" ]; then
    echo "=== Copie des fichiers de test ==="
    mkdir -p "$INSTALL_DIR/tests" "$INSTALL_DIR/scripts"
    cp tests/test_bot.py      "$INSTALL_DIR/tests/test_bot.py"
    cp tests/test_backtest.py "$INSTALL_DIR/tests/test_backtest.py"
    cp scripts/backtest.py    "$INSTALL_DIR/scripts/backtest.py"
    cp scripts/run_tests.sh   "$INSTALL_DIR/scripts/run_tests.sh"
    mkdir -p "$INSTALL_DIR/data"
    cp data/backtest_sample_btc5m_range_2026.db "$INSTALL_DIR/data/backtest_sample_btc5m_range_2026.db"
    echo "=== Lancement des tests ==="
    cd "$INSTALL_DIR"
    TRADINEBOTTE_DIR="$INSTALL_DIR" "$INSTALL_DIR/venv/bin/python3" \
        -W ignore::ResourceWarning -m unittest discover tests/ -v
    cd - > /dev/null
fi

# Prefix TRADINEBOTTE_DIR=... only when the user chose a non-default dir.
if [ "$INSTALL_DIR" = "$HOME/tradinebotte" ]; then
    _TD=""
else
    _TD="TRADINEBOTTE_DIR=\"$INSTALL_DIR\" "
fi

echo ""
echo "=== Installation terminée dans $INSTALL_DIR ==="
echo ""
echo "ÉTAPES SUIVANTES :"
echo "1. Configurer        : ${_TD}python3 \"$REPO_DIR/scripts/setup.py\""
echo "   (saisir la clé privée du wallet, ou Entrée sans clé pour le mode simulation)"
echo "2. Lance le bot      : ${_TD}bash \"$REPO_DIR/scripts/start_bot.sh\""
if [ "$WITH_TESTS" = "1" ]; then
echo ""
echo "Tests   : cd \"$INSTALL_DIR\" && ${_TD}venv/bin/python3 -W ignore::ResourceWarning -m unittest discover tests/ -v"
echo "Backtest: cd \"$INSTALL_DIR\" && ${_TD}venv/bin/python3 \"$REPO_DIR/scripts/backtest.py\""
fi
