#!/usr/bin/env bash
# install-lbcmm.sh — one-command install for LBC-market-making-bot
# Usage:
#   bash install-lbcmm.sh
#   ./install-lbcmm.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  LBC-market-making-bot — installer"
echo "════════════════════════════════════════════════════════════"
echo ""

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 is required. Install Python 3.10+ and re-run."
  exit 1
fi

PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
echo "• Python: $(command -v python3) ($PY_VER)"

if [[ ! -f "$ROOT/requirements-lbcmm.txt" ]]; then
  echo "ERROR: requirements-lbcmm.txt not found in $ROOT"
  exit 1
fi

echo "• Creating virtualenv at .venv/ …"
python3 -m venv "$ROOT/.venv"
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

echo "• Upgrading pip …"
python -m pip install -q --upgrade pip

echo "• Installing dependencies …"
python -m pip install -q -r "$ROOT/requirements-lbcmm.txt"

mkdir -p "$ROOT/bin"

# Main launcher — no PYTHONPATH juggling for the user
cat > "$ROOT/bin/lbcmm" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "Missing $ROOT/.venv — run: bash install-lbcmm.sh"
  exit 1
fi
exec "$ROOT/.venv/bin/python" -m lbcmm "$@"
EOF
chmod +x "$ROOT/bin/lbcmm"

# Friendly aliases
cat > "$ROOT/bin/lbcmm-gui" << 'EOF'
#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/bin/lbcmm" gui "$@"
EOF
chmod +x "$ROOT/bin/lbcmm-gui"

# Optional: drop a symlink into ~/.local/bin if available
LOCAL_BIN="${HOME}/.local/bin"
if mkdir -p "$LOCAL_BIN" 2>/dev/null; then
  ln -sfn "$ROOT/bin/lbcmm" "$LOCAL_BIN/lbcmm"
  ln -sfn "$ROOT/bin/lbcmm-gui" "$LOCAL_BIN/lbcmm-gui"
  if echo ":$PATH:" | grep -q ":$LOCAL_BIN:"; then
    PATH_NOTE="You can run:  lbcmm gui"
  else
    PATH_NOTE="Add to PATH once:  export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
else
  PATH_NOTE="Run via:  $ROOT/bin/lbcmm gui"
fi

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  Install complete ✓"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "  Start the browser control panel:"
echo ""
echo "      $ROOT/bin/lbcmm gui"
echo ""
echo "  Then open:  http://127.0.0.1:8787/"
echo ""
echo "  Run in the background (keeps running after you close the terminal):"
echo ""
echo "      mkdir -p logs"
echo "      nohup $ROOT/bin/lbcmm gui > logs/gui.log 2>&1 &"
echo "      echo \$! > logs/gui.pid"
echo "      # stop later:  kill \"\$(cat logs/gui.pid)\""
echo ""
echo "  Or use systemd user units (see QUICKSTART-LBCMM.md):"
echo "      systemd/lbcmm-gui.service   # control panel"
echo "      systemd/lbcmm.service       # headless bot"
echo ""
echo "  Other useful commands:"
echo "      $ROOT/bin/lbcmm depth     # public ±2% depth"
echo "      $ROOT/bin/lbcmm status"
echo "      $ROOT/bin/lbcmm run --paper"
echo ""
echo "  $PATH_NOTE"
echo ""
echo "  First launch opens a setup wizard (paper or live)."
echo "  Paper mode = safe practice, no real money."
echo ""
echo "  Docs: QUICKSTART-LBCMM.md"
echo "  Attribution: forked neofutur's multibot design (GPL-3.0)"
echo "════════════════════════════════════════════════════════════"
echo ""

# Offer to start GUI if interactive TTY
if [[ -t 0 && -t 1 ]]; then
  read -r -p "Start the GUI now? [Y/n] " ans || ans=n
  ans="${ans:-Y}"
  if [[ "$ans" =~ ^[Yy]$ ]]; then
    exec "$ROOT/bin/lbcmm" gui
  fi
fi
