#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source .venv/bin/activate

if [ $# -gt 0 ]; then
    python run.py "$@"
    exit $?
fi

# Read port from config (default 8765)
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
