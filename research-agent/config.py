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
