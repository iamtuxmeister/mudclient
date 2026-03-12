#!/usr/bin/env bash
# Create a project-local venv for MUD client Python triggers.
# Run once:   bash setup_venv.sh
# Then install whatever you need:   venv/bin/pip install requests aiohttp psycopg2-binary ...

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/venv"

if [ -d "$VENV" ]; then
    echo "venv already exists at $VENV"
else
    python3 -m venv "$VENV"
    echo "Created venv at $VENV"
fi

"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install requests aiohttp --quiet
echo ""
echo "Done. Installed: requests, aiohttp"
echo ""
echo "To add more packages:"
echo "  venv/bin/pip install <package>"
echo ""
echo "In a trigger body (#python), they're available immediately:"
echo "  import requests"
echo "  r = requests.get('https://example.com')"
