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
# Bearer token for the LLM endpoint. LM Studio needs none; the cluster's
# LiteLLM router requires a key (its master_key is the literal "none").
LM_STUDIO_API_KEY: str = os.getenv("LM_STUDIO_API_KEY", "none")

# ── Cross-tool usage telemetry (dashboard Telemetry & Usage tab) ─────────────
# Shared HMAC secret with the dashboard; blank = telemetry off. Set
# TELEMETRY_HMAC_SECRET in .env to enable.
TELEMETRY_HMAC_SECRET: str = os.getenv("TELEMETRY_HMAC_SECRET", "")
DASHBOARD_INTERNAL_URL: str = os.getenv("DASHBOARD_INTERNAL_URL", "http://127.0.0.1:3010")

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

# ── Foundation research scope ────────────────────────────────────────────────
# This tool is operated by the Texas A&M Foundation. Per Dr. G (2026-07-27), any
# query about RESEARCHERS should report Texas A&M people ~exclusively — a
# nutrition-researcher search had returned only non-TAMU names because no
# institution was stated and the tool went national. So this is a NEAR-EXCLUSIVE
# scope, not a soft default: when a query involves finding/reporting researchers,
# experts, faculty, labs, or programs, the people REPORTED must be Texas A&M.
# The only carve-outs are (a) the user explicitly names another institution / asks
# for a national comparison, and (b) donor-prospect research on an external named
# subject (which is not a researcher query at all). Off via FOUNDATION_SCOPE_ENABLED=0.
FOUNDATION_SCOPE_ENABLED: bool = os.getenv("FOUNDATION_SCOPE_ENABLED", "1") in ("1", "true", "True", "yes")
FOUNDATION_INSTITUTION: str = os.getenv(
    "FOUNDATION_INSTITUTION", "Texas A&M University (the flagship main campus in College Station)"
)
_FOUNDATION_SCOPE_TEXT = (
    "MISSION CONTEXT — this research assistant is operated by the Texas A&M "
    f"Foundation to support {FOUNDATION_INSTITUTION}. Its job is to surface Texas "
    "A&M people and work, not a national field survey.\n"
    "TEXAS A&M SCOPE (near-exclusive): whenever the query involves finding, "
    "identifying, recommending, or reporting on researchers, experts, faculty, "
    "scientists, labs, centers, or programs, the researchers and work you REPORT "
    f"must be at {FOUNDATION_INSTITUTION} — essentially exclusively. Scope every "
    "claim and search query to Texas A&M, prefer tamu.edu and official Texas A&M "
    "sources, and name only Texas A&M people/programs as the answer. Do NOT "
    "present non-Texas-A&M researchers as recommendations or as the result, even "
    "if they are more prominent in the field. A non-Texas-A&M name may appear "
    "ONLY as incidental context (e.g. a named external collaborator on a Texas "
    "A&M project), never as a recommended researcher. If few Texas A&M "
    "researchers are found, report those and note the gap rather than padding "
    "with outside names.\n"
    "NARROW EXCEPTIONS — only when: (a) the user EXPLICITLY names a different "
    "institution or explicitly asks for a national / cross-institution "
    "comparison (then honor exactly what they asked); or (b) the query's subject "
    "is a specific external individual, company, donor, or foundation being "
    "profiled (a donor-prospect brief, e.g. a businessperson or a grantmaking "
    "foundation) — that is NOT a researcher-finding query, so research the named "
    "subject directly and do not force a Texas A&M scope onto them. In every "
    "other case, stay on Texas A&M.\n\n"
)
FOUNDATION_SCOPE_PREAMBLE: str = _FOUNDATION_SCOPE_TEXT if FOUNDATION_SCOPE_ENABLED else ""
