#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  POLYMARKET LIVE BOT v3 — Installation Ubuntu 24.04
#
#  Répertoire d'installation (par ordre de priorité) :
#    1. Argument positionnel : bash scripts/install.sh ~/polymarket
#    2. Variable d'environnement : POLYMARKET_DIR=~/polymarket bash scripts/install.sh
#    3. Valeur par défaut : ~/polymarket (aucun accès root requis)
#
#  Options :
#    --with-tests   copie aussi tests/ et scripts/backtest.py
# ═══════════════════════════════════════════════════════════════════
set -e

WITH_TESTS=0
ARGS=()
for arg in "$@"; do
    if [ "$arg" = "--with-tests" ]; then
        WITH_TESTS=1
    else
        ARGS+=("$arg")
    fi
done

INSTALL_DIR="${ARGS[0]:-${POLYMARKET_DIR:-$HOME/polymarket}}"
INSTALL_DIR="$(eval echo "$INSTALL_DIR")"   # développe ~ si présent

echo "=== Répertoire d'installation : $INSTALL_DIR ==="

echo "=== Installation des dépendances système ==="
apt-get update -q
apt-get install -y python3 python3-pip python3-venv sqlite3

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
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install aiohttp websockets web3 py-clob-client

# Wrapper d'exécution avec POLYMARKET_DIR exportée
cat > "$INSTALL_DIR/run.sh" << EOF
#!/bin/bash
export POLYMARKET_DIR="$INSTALL_DIR"
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
    POLYMARKET_DIR="$INSTALL_DIR" "$INSTALL_DIR/venv/bin/python3" \
        -W ignore::ResourceWarning -m unittest discover tests/ -v
    cd - > /dev/null
fi

echo ""
echo "=== Installation terminée dans $INSTALL_DIR ==="
echo ""
echo "ÉTAPES SUIVANTES :"
echo "1. Prépare ton wallet : POLYMARKET_DIR=\"$INSTALL_DIR\" python3 scripts/setup.py"
echo "2. Lance le bot      : POLYMARKET_DIR=\"$INSTALL_DIR\" bash scripts/start_bot.sh"
if [ "$WITH_TESTS" = "1" ]; then
echo ""
echo "Tests : cd \"$INSTALL_DIR\" && POLYMARKET_DIR=\"$INSTALL_DIR\" venv/bin/python3 -W ignore::ResourceWarning -m unittest discover tests/ -v"
echo "Backtest : cd \"$INSTALL_DIR\" && POLYMARKET_DIR=\"$INSTALL_DIR\" venv/bin/python3 scripts/backtest.py"
fi
