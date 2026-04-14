#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
source .venv/bin/activate

if [ $# -gt 0 ]; then
    python run.py "$@"
    exit $?
fi

echo "==============================="
echo "     Research Agent Launcher"
echo "==============================="
echo ""
echo "  1) REST API server  (http://localhost:8000)"
echo "  2) MCP server       (stdio)"
echo "  3) Run a query"
echo ""
read -rp "Choose [1-3]: " choice

case "$choice" in
    1)
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
