#!/usr/bin/env bash
# boot.sh — headless, PRODUCTION launcher for the Deep Research Agent.
#
# Migrated to the fleet's Next.js-frontend + FastAPI-backend architecture. Starts
# the FastAPI backend (research-agent/run.py api) on an INTERNAL port
# (127.0.0.1:8765) in the background, then the Next.js frontend (frontend/,
# :3015, `next start` — PRODUCTION) in the foreground. The dashboard routing
# plane proxies /DeepResearch -> :3015 (strip_prefix=0, the Next app owns the
# basePath); the frontend rewrites /api/* and /health to the backend. Live runs
# stream over SSE (EventSource) through that /api/* path. No interactive prompts.
#
# Do NOT use research-agent/start.sh for boot (interactive setup wizard).
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
AGENT_DIR="$SCRIPT_DIR/research-agent"
VENV="$AGENT_DIR/.venv"
BACKEND_PORT="${API_PORT:-8765}"
FRONTEND_PORT="${PORT:-3015}"
LOG_DIR="${LOG_DIR:-/tmp}"

if [ -s "$HOME/.nvm/nvm.sh" ]; then . "$HOME/.nvm/nvm.sh"; fi
if [ -f "$AGENT_DIR/.env" ]; then set -a; . "$AGENT_DIR/.env"; set +a; fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "ERROR: $VENV/bin/python not found — set up the research-agent venv first." >&2
  exit 1
fi

free_port() {
  local port="$1" pids
  pids=$(ss -tlnpH "sport = :${port}" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u)
  if [ -n "$pids" ]; then
    echo "Port $port in use by pid(s): $pids — stopping them first."
    kill $pids 2>/dev/null || true; sleep 1
    if ss -tln "sport = :${port}" 2>/dev/null | grep -q LISTEN; then kill -9 $pids 2>/dev/null || true; sleep 1; fi
  fi
}
free_port "$BACKEND_PORT"
free_port "$FRONTEND_PORT"

# Backend — FastAPI research API, INTERNAL only (127.0.0.1).
echo "Starting Deep Research backend (FastAPI) on 127.0.0.1:${BACKEND_PORT} ..."
( cd "$AGENT_DIR" && API_HOST=127.0.0.1 API_PORT="$BACKEND_PORT" \
    nohup "$VENV/bin/python" run.py api > "$LOG_DIR/deep-research-backend.log" 2>&1 & )

# Frontend — Next.js production. Build if missing.
if [ ! -d "frontend/.next" ]; then
  echo "No frontend build found — running next build ..."
  npm --prefix frontend run build
fi
echo "Starting Deep Research frontend (Next.js production) on :${FRONTEND_PORT}/DeepResearch ..."
exec env PORT="$FRONTEND_PORT" npm --prefix frontend run start
