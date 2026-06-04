"""
config.py — Central configuration loaded from environment / .env file.
All other modules import from here; never read os.environ directly.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── LM Studio ──────────────────────────────────────────────────────────────
LM_STUDIO_BASE_URL: str = os.getenv("LM_STUDIO_BASE_URL", "http://localhost:1234/v1")
LM_STUDIO_MODEL: str = os.getenv("LM_STUDIO_MODEL", "local-model")

# ── Search ─────────────────────────────────────────────────────────────────
SEARCH_BACKEND: str = os.getenv("SEARCH_BACKEND", "duckduckgo").lower()
LANGSEARCH_API_KEY: str = os.getenv("LANGSEARCH_API_KEY", "")
BRAVE_API_KEY: str | None = os.getenv("BRAVE_API_KEY") or None
SERPAPI_KEY: str | None = os.getenv("SERPAPI_KEY") or None
# Self-hosted SearXNG metasearch (privacy-respecting, local, no API key).
SEARXNG_URL: str = os.getenv("SEARXNG_URL", "http://localhost:8888")

# ── REST API ───────────────────────────────────────────────────────────────
API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
API_PORT: int = int(os.getenv("API_PORT", "8765"))
API_KEY: str = os.getenv("API_KEY", "")
CORS_ORIGINS: list[str] = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]

# ── MCP ────────────────────────────────────────────────────────────────────
MCP_SERVER_NAME: str = os.getenv("MCP_SERVER_NAME", "research-agent")

# ── Research behaviour ─────────────────────────────────────────────────────
MAX_SEARCH_RESULTS: int = int(os.getenv("MAX_SEARCH_RESULTS", "5"))
MAX_PAGE_CONTENT_LENGTH: int = int(os.getenv("MAX_PAGE_CONTENT_LENGTH", "4000"))
CONTEXT_LIMIT_TOKENS: int = int(os.getenv("CONTEXT_LIMIT_TOKENS", "256000"))
SEARCH_CACHE_TTL_SECONDS: int = int(os.getenv("SEARCH_CACHE_TTL_SECONDS", "300"))
JOB_TIMEOUT_SECONDS: int = int(os.getenv("JOB_TIMEOUT_SECONDS", "10800"))

# ── Depth presets ──────────────────────────────────────────────────────────
# The preset table lives in depth_presets.py so the worker can consult it
# BEFORE importing config — config reads MAX_SEARCH_RESULTS from env at
# module-load, and the preset must already have written that env first.
from depth_presets import DEPTH_PRESETS, DEFAULT_DEPTH  # re-export

# Thorough mode: every URL the searcher surfaces is LLM-classified for
# usefulness and emitted as a resource_verdict stream event. Auto-enabled
# for depth=ultra; opt-in otherwise. Read via env so the subprocess
# worker can flip it per-job.
THOROUGH_MODE: bool = os.getenv("THOROUGH_MODE", "0") in ("1", "true", "True", "yes")
