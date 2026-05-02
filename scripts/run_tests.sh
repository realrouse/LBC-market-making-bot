#!/usr/bin/env bash
# Run the automated test suite using the project virtual environment.
#
# Usage:
#   bash scripts/run_tests.sh
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Prefer the project .venv created with `uv venv`.
# Fall back to the production venv at TRADINEBOTTE_DIR if .venv is absent.
if [ -d "$PROJECT_DIR/.venv" ]; then
    PYTHON="$PROJECT_DIR/.venv/bin/python3"
elif [ -d "${TRADINEBOTTE_DIR:-$HOME/tradinebotte}/venv" ]; then
    PYTHON="${TRADINEBOTTE_DIR:-$HOME/tradinebotte}/venv/bin/python3"
else
    echo "ERROR: no virtual environment found."
    echo "Create one with:"
    echo "  uv venv .venv && uv pip install aiohttp websockets"
    exit 1
fi

cd "$PROJECT_DIR"

# Redirect bot I/O to ~/tmp so tests never touch /opt or write credentials.
export TRADINEBOTTE_DIR="${HOME}/tmp/tradinebotte-test"

echo "Python : $PYTHON ($("$PYTHON" --version))"
echo "Tests  : $PROJECT_DIR/tests/"
echo ""
"$PYTHON" -W ignore::ResourceWarning -m unittest discover -s tests/ -p "test_*.py" -v
