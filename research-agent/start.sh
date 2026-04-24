#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# First-run setup: create venv and install deps if missing.
if [ ! -f ".venv/bin/activate" ]; then
    echo "Virtual environment not found. Setting up now..."
    echo ""
    # Prefer a Python in the supported range (3.10–3.13). crewai and some
    # other deps do not yet support 3.14+, so a bare `python3` can fail.
    find_compatible_python() {
        for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
            if command -v "$candidate" >/dev/null 2>&1; then
                VER=$("$candidate" -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "")
                case "$VER" in
                    3.10|3.11|3.12|3.13)
                        echo "$candidate"
                        return 0
                        ;;
                esac
            fi
        done
        return 1
    }
    PYBOOT=$(find_compatible_python || true)
    if [ -z "$PYBOOT" ]; then
        echo "No compatible Python (3.10–3.13) found."
        echo "  (crewai and other dependencies do not yet support Python 3.14+.)"
        if command -v brew >/dev/null 2>&1; then
            echo ""
            read -rp "Install python@3.13 via Homebrew now? [Y/n]: " install_ans
            if [[ ! "$install_ans" =~ ^[Nn]$ ]]; then
                brew install python@3.13 || {
                    echo "ERROR: brew install python@3.13 failed."
                    read -rp "Press Enter to close..."
                    exit 1
                }
                # brew doesn't always link keg-only pythons; point at the cellar directly.
                if ! command -v python3.13 >/dev/null 2>&1; then
                    BREW_PREFIX=$(brew --prefix python@3.13 2>/dev/null || echo "")
                    if [ -n "$BREW_PREFIX" ] && [ -x "$BREW_PREFIX/bin/python3.13" ]; then
                        export PATH="$BREW_PREFIX/bin:$PATH"
                    fi
                fi
                PYBOOT=$(find_compatible_python || true)
            fi
        else
            echo "  Homebrew not found. Install a supported Python from"
            echo "  https://www.python.org/downloads/ and re-run this script."
        fi
    fi
    if [ -z "$PYBOOT" ]; then
        echo "ERROR: Still no compatible Python on PATH. Aborting."
        read -rp "Press Enter to close..."
        exit 1
    fi
    echo "Using $PYBOOT ($("$PYBOOT" --version 2>&1))"
    "$PYBOOT" -m venv .venv || {
        echo "ERROR: Failed to create virtual environment."
        read -rp "Press Enter to close..."
        exit 1
    }
    # shellcheck disable=SC1091
    source .venv/bin/activate
    echo "Installing dependencies..."
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt || {
        echo "ERROR: Failed to install dependencies."
        read -rp "Press Enter to close..."
        exit 1
    }
    if [ ! -f ".env" ] && [ -f ".env.example" ]; then
        cp .env.example .env
        echo "Created .env from .env.example — edit it to set your LM_STUDIO_MODEL."
    fi
    echo "Setup complete!"
    echo ""
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if [ $# -gt 0 ]; then
    python run.py "$@"
    exit $?
fi

# Read API_PORT from .env (if present) so the URL we display matches the
# port api_server.py will actually bind to. config.py's default is 8765
# but .env.example ships API_PORT=8000, so the two can drift.
if [ -z "${API_PORT:-}" ] && [ -f ".env" ]; then
    API_PORT=$(grep -E '^[[:space:]]*API_PORT[[:space:]]*=' .env | tail -n 1 | cut -d= -f2- | tr -d '[:space:]"'"'")
fi
API_PORT="${API_PORT:-8765}"

echo "==============================="
echo "     Research Agent Launcher"
echo "==============================="
echo ""
echo "  1) REST API server  (http://localhost:${API_PORT})"
echo "  2) MCP server       (stdio)"
echo "  3) Run a query"
echo ""
read -rp "Choose [1-3]: " choice

case "$choice" in
    1)
        # Kill any existing API server process on the same port before starting
        EXISTING_PID=$(lsof -ti tcp:"${API_PORT}" 2>/dev/null)
        if [ -n "$EXISTING_PID" ]; then
            echo "Stopping existing server (PID $EXISTING_PID)…"
            kill "$EXISTING_PID" 2>/dev/null
            sleep 1
        fi
        python run.py api
        ;;
    2)
        python run.py mcp
        ;;
    3)
        echo ""
        read -rp "Enter your research question: " question
        echo ""
        read -rp "Verbose output? [y/N]: " verbose
        if [[ "$verbose" =~ ^[Yy]$ ]]; then
            python run.py query "$question" --verbose
        else
            python run.py query "$question"
        fi
        ;;
    *)
        echo "Invalid choice. Exiting."
        exit 1
        ;;
esac

echo ""
read -rp "Press Enter to close..."
