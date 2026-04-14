#!/usr/bin/env bash
# setup.sh — Create the virtual environment and install all dependencies.
#
# Usage:
#   chmod +x setup.sh
#   ./setup.sh
#
# After this runs, activate the venv with:
#   source .venv/bin/activate

set -euo pipefail

PYTHON=${PYTHON:-python3}
VENV_DIR=".venv"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Research Agent — Environment Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Check Python version ───────────────────────────────────────────────────
PY_VERSION=$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "→ Python version: $PY_VERSION"

REQUIRED_MAJOR=3
REQUIRED_MINOR=10

MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$MAJOR" -lt "$REQUIRED_MAJOR" ] || { [ "$MAJOR" -eq "$REQUIRED_MAJOR" ] && [ "$MINOR" -lt "$REQUIRED_MINOR" ]; }; then
  echo "✗  Python $REQUIRED_MAJOR.$REQUIRED_MINOR+ is required (found $PY_VERSION)."
  echo "   Install it from https://www.python.org/downloads/"
  exit 1
fi

# ── Create virtual environment ─────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
  echo "→ Creating virtual environment at $VENV_DIR …"
  "$PYTHON" -m venv "$VENV_DIR"
else
  echo "→ Virtual environment already exists at $VENV_DIR — skipping creation."
fi

# ── Activate venv ──────────────────────────────────────────────────────────
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
echo "→ Virtual environment activated."

# ── Upgrade pip ────────────────────────────────────────────────────────────
echo "→ Upgrading pip …"
pip install --quiet --upgrade pip

# ── Install dependencies ───────────────────────────────────────────────────
echo "→ Installing dependencies from requirements.txt …"
pip install --quiet -r requirements.txt

# ── Copy .env if not present ───────────────────────────────────────────────
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "→ Created .env from .env.example — edit it to set your LM_STUDIO_MODEL."
else
  echo "→ .env already exists — skipping copy."
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ Setup complete!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Find your LM Studio model name:"
echo "       curl http://localhost:1234/v1/models"
echo ""
echo "  2. Edit .env and set LM_STUDIO_MODEL= to that name."
echo ""
echo "  3. Activate the venv (each new terminal session):"
echo "       source .venv/bin/activate"
echo ""
echo "  4. Run a research query:"
echo "       python run.py query \"What is the latest in fusion energy?\""
echo ""
echo "  5. Start the MCP server (for use with Claude Desktop etc.):"
echo "       python run.py mcp"
echo ""
echo "  6. Start the REST API server (for use with other programs):"
echo "       python run.py api"
echo ""
