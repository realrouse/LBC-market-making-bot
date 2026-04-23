#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  POLYMARKET LIVE BOT v3 — Installation Ubuntu 24.04
# ═══════════════════════════════════════════════════════════════════
set -e

echo "=== Installation des dépendances système ==="
apt-get update -q
apt-get install -y python3 python3-pip python3-venv sqlite3

echo "=== Création des répertoires ==="
mkdir -p /opt/polymarket-live

echo "=== Copie du bot ==="
cp bot/live_bot.py /opt/polymarket-live/live_bot.py

echo "=== Création de l'environnement virtuel ==="
python3 -m venv /opt/polymarket-live/venv

echo "=== Installation des packages Python ==="
/opt/polymarket-live/venv/bin/pip install --upgrade pip
/opt/polymarket-live/venv/bin/pip install aiohttp websockets web3 py-clob-client

# Crée un wrapper python3 qui utilise le venv
cat > /opt/polymarket-live/run.sh << 'EOF'
#!/bin/bash
source /opt/polymarket-live/venv/bin/activate
python3 /opt/polymarket-live/live_bot.py
EOF
chmod +x /opt/polymarket-live/run.sh

echo "=== Vérification syntaxe ==="
/opt/polymarket-live/venv/bin/python3 -c "import ast; ast.parse(open('/opt/polymarket-live/live_bot.py').read()); print('SYNTAXE OK')"

echo ""
echo "=== Installation terminée ==="
echo ""
echo "ÉTAPES SUIVANTES :"
echo "1. Prépare ton wallet : python3 scripts/setup.py 0xTA_PRIVATE_KEY"
echo "2. Exporte les variables affichées par setup.py"
echo "3. Lance le bot : bash scripts/start_bot.sh"

