"""
tools.py — CrewAI tools for web searching and page fetching.

Search backends supported (configure via SEARCH_BACKEND env var):
  - duckduckgo  (default, no API key required)
  - brave       (requires BRAVE_API_KEY; falls back to duckduckgo if missing)
  - serpapi     (requires SERPAPI_KEY; falls back to duckduckgo if missing)
  - langsearch  (requires LANGSEARCH_API_KEY; falls back to duckduckgo if missing)
"""
from __future__ import annotations

import re
import threading
import time
from abc import ABC, abstractmethod
from typing import Type

import requests
from bs4 import BeautifulSoup
from crewai.tools import BaseTool
from ddgs import DDGS
from pydantic import BaseModel, Field

from config import (
    BRAVE_API_KEY,
    CONTEXT_LIMIT_TOKENS,
    LANGSEARCH_API_KEY,
    MAX_PAGE_CONTENT_LENGTH,
    MAX_SEARCH_RESULTS,
    SEARCH_BACKEND,
    SEARCH_CACHE_TTL_SECONDS,
    SERPAPI_KEY,
)
from scratchpad import log
from source_classifier import SourceDiversityTracker, classify

# ── Stream emitter ─────────────────────────────────────────────────────────
# Configured once per worker process by calling set_stream_emitter().
# All tools call _emit_stream() to send real-time events to the UI.

_stream_emitter = None


def set_stream_emitter(emit_fn) -> None:
    """Register a function(event: dict) that forwards events to the .stream file."""
    global _stream_emitter
    _stream_emitter = emit_fn


def _emit_stream(event: dict) -> None:
    if _stream_emitter:
        try:
            _stream_emitter(event)
        except Exception:
            pass

# ── Context budget tracker ─────────────────────────────────────────────────
# Tracks cumulative content fetched per worker process and dynamically
# tightens the per-page limit as the context window fills up.

_CHARS_PER_TOKEN = 4  # rough estimate


class _ContextBudget:
    def __init__(self) -> None:
        self._limit = CONTEXT_LIMIT_TOKENS * _CHARS_PER_TOKEN
        self._used = 0
        self._lock = threading.Lock()

    def record(self, chars: int) -> None:
        with self._lock:
            self._used += chars

    def _fraction_used(self) -> float:
        with self._lock:
            return min(1.0, self._used / self._limit)

    def page_limit(self) -> int:
        """Return a dynamically reduced page limit as context fills up."""
        f = self._fraction_used()
        if f < 0.40:
            return MAX_PAGE_CONTENT_LENGTH
        if f < 0.60:
            return MAX_PAGE_CONTENT_LENGTH // 2
        if f < 0.80:
            return MAX_PAGE_CONTENT_LENGTH // 4
        return 500

    def warning(self) -> str:
        """Return a budget status line to append to tool output."""
        f = self._fraction_used()
        used_k = int(self._used / 1000)
        limit_k = int(self._limit / 1000)
        pct = int(f * 100)
        if f >= 0.80:
            return (
                f"\n\n⚠️ CONTEXT BUDGET CRITICAL: {used_k}K/{limit_k}K chars used ({pct}%). "
                "Stop fetching pages. Synthesise from what you have."
            )
        if f >= 0.60:
            return (
                f"\n\n⚠️ Context budget high: {used_k}K/{limit_k}K chars used ({pct}%). "
                "Be very selective — only fetch pages essential to the query."
            )
        if f >= 0.40:
            return (
                f"\n\nContext budget: {used_k}K/{limit_k}K chars used ({pct}%). "
                "Prioritise the most relevant sources."
            )
        return ""


_budget = _ContextBudget()

# Serialise LangSearch requests — the free tier rejects concurrent calls (429)
_langsearch_lock = threading.Lock()


# ── Search backend abstraction ─────────────────────────────────────────────


class SearchBackend(ABC):
    """Unified interface for all search providers."""

    @abstractmethod
    def search(self, query: str, max_results: int) -> list[dict]:
        """
        Execute a search and return results as a list of dicts with keys:
          - title: str
          - url: str
          - snippet: str
        """


class DuckDuckGoBackend(SearchBackend):
    def search(self, query: str, max_results: int) -> list[dict]:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            }
            for r in results
        ]


class BraveBackend(SearchBackend):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def search(self, query: str, max_results: int) -> list[dict]:
        response = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": self._api_key,
            },
            params={"q": query, "count": max_results},
            timeout=15,
        )
        response.raise_for_status()
        items = response.json().get("web", {}).get("results", [])
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("description", ""),
            }
            for r in items
        ]


class SerpApiBackend(SearchBackend):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def search(self, query: str, max_results: int) -> list[dict]:
        response = requests.get(
            "https://serpapi.com/search",
            params={
                "q": query,
                "api_key": self._api_key,
                "num": max_results,
                "engine": "google",
            },
            timeout=15,
        )
        response.raise_for_status()
        results = response.json().get("organic_results", [])
        return [
            {
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "snippet": r.get("snippet", ""),
            }
            for r in results[:max_results]
        ]


class LangSearchBackend(SearchBackend):
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def search(self, query: str, max_results: int) -> list[dict]:
        with _langsearch_lock:
            time.sleep(1)  # ensure at least 1 s between requests on the free tier
            return self._request(query, max_results)

    def _request(self, query: str, max_results: int) -> list[dict]:
        response = requests.post(
            "https://api.langsearch.com/v1/web-search",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "summary": True, "count": max_results},
            timeout=15,
        )
        response.raise_for_status()
        items = response.json().get("data", {}).get("webPages", {}).get("value", [])
        return [
            {
                "title": r.get("name", ""),
                "url": r.get("url", ""),
                "snippet": r.get("summary") or r.get("snippet", ""),
            }
            for r in items
        ]


def _get_backend() -> SearchBackend:
    """Return the configured search backend, falling back to DuckDuckGo if the required key is missing."""
    if SEARCH_BACKEND == "brave":
        if BRAVE_API_KEY:
            return BraveBackend(BRAVE_API_KEY)
        log("WARNING: BRAVE_API_KEY not set — falling back to DuckDuckGo")
        return DuckDuckGoBackend()
    if SEARCH_BACKEND == "serpapi":
        if SERPAPI_KEY:
            return SerpApiBackend(SERPAPI_KEY)
        log("WARNING: SERPAPI_KEY not set — falling back to DuckDuckGo")
        return DuckDuckGoBackend()
    if SEARCH_BACKEND == "langsearch":
        if LANGSEARCH_API_KEY:
            return LangSearchBackend(LANGSEARCH_API_KEY)
        log("WARNING: LANGSEARCH_API_KEY not set — falling back to DuckDuckGo")
        return DuckDuckGoBackend()
    return DuckDuckGoBackend()

# Shared HTTP session — connection pooling across all page fetches in a job
_http_session = requests.Session()
_http_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
})

# Precompiled regex for collapsing blank lines in fetched page content
_BLANK_LINES_RE = re.compile(r"\n{3,}")


# ── Fetched URL tracker ────────────────────────────────────────────────────
# Per-process singleton. Tracks which URLs have already been fetched so that:
#   - Search results silently drop already-fetched URLs (agent gets fresh options)
#   - fetch_webpage returns early on duplicates (safety net, no iter wasted)

class _FetchedURLs:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def already_fetched(self, url: str) -> bool:
        with self._lock:
            return url in self._seen

    def mark(self, url: str) -> None:
        with self._lock:
            self._seen.add(url)

    def filter(self, urls: list[str]) -> set[str]:
        """Return the subset of urls that have already been fetched."""
        with self._lock:
            return self._seen.intersection(urls)


_fetched = _FetchedURLs()
_diversity = SourceDiversityTracker()


# ── Search query dedup tracker ─────────────────────────────────────────────
# Detects when the agent is running near-identical queries in a loop.
# "Near-identical" = one query is a subset of another after stripping
# common exclusion operators (-site:...) and normalising whitespace.

_MAX_RECENT_QUERIES = 20
_SITE_EXCL_RE = re.compile(r"-site:\S+", re.IGNORECASE)


class _QueryTracker:
    def __init__(self) -> None:
        self._recent: list[str] = []
        self._lock = threading.Lock()

    @staticmethod
    def _normalise(q: str) -> str:
        """Strip -site: exclusions and collapse whitespace for comparison."""
        q = _SITE_EXCL_RE.sub("", q)
        return " ".join(q.lower().split())

    def check_and_record(self, query: str) -> str:
        """
        Record the query. If it looks like a near-duplicate of a recent one,
        return a warning string to append to the tool output; else return "".
        """
        norm = self._normalise(query)
        with self._lock:
            similar = [q for q in self._recent if norm in q or q in norm]
            self._recent.append(norm)
            if len(self._recent) > _MAX_RECENT_QUERIES:
                self._recent.pop(0)

        if similar:
            return (
                "\n\n⚠️ SEARCH LOOP DETECTED: This query is very similar to a recent one "
                f"(\"{similar[-1][:80]}\"). You are likely in a loop. "
                "Stop repeating variations of the same query. Instead: (1) fetch one of "
                "the URLs already returned, (2) try a completely different angle, or "
                "(3) proceed to the next pipeline stage with what you have."
            )
        return ""


_query_tracker = _QueryTracker()


# ── Search result cache ────────────────────────────────────────────────────
# In-process TTL cache keyed by (query, num_results) to avoid redundant
# network calls for identical queries within a session.

class _SearchCache:
    def __init__(self, ttl: int = SEARCH_CACHE_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._store: dict[tuple[str, int], tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def get(self, query: str, num_results: int = MAX_SEARCH_RESULTS) -> str | None:
        key = (query, num_results)
        with self._lock:
            entry = self._store.get(key)
            if entry is None or (time.time() - entry[1]) >= self._ttl:
                self._misses += 1
                return None
            self._hits += 1
            return entry[0]

    def set(self, query: str, num_results: int, result: str) -> None:
        key = (query, num_results)
        with self._lock:
            self._store[key] = (result, time.time())

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    def _cache_stats(self) -> dict[str, int]:
        with self._lock:
            return {"hits": self._hits, "misses": self._misses}


_search_cache = _SearchCache()


# ── Input schemas ──────────────────────────────────────────────────────────


class SearchInput(BaseModel):
    query: str = Field(description="The search query to look up on the web.")
    num_results: int = Field(
        default=MAX_SEARCH_RESULTS,
        description="How many results to return (default: 5).",
    )


class FetchPageInput(BaseModel):
    url: str = Field(description="The full URL of the webpage to fetch and read.")


# ── WebSearchTool ──────────────────────────────────────────────────────────


class WebSearchTool(BaseTool):
    """Search the internet for current information on any topic."""

    name: str = "web_search"
    description: str = (
        "Search the internet for information on any topic. "
        "Returns a list of results with titles, URLs, and summaries. "
        "Use this to find facts, news, and research from multiple angles."
    )
    args_schema: Type[BaseModel] = SearchInput

    def _run(self, query: str, num_results: int = MAX_SEARCH_RESULTS) -> str:
        log(f"Searching: \"{query}\"")
        _emit_stream({"type": "search", "query": query})
        loop_warning = _query_tracker.check_and_record(query)

        cached = _search_cache.get(query, num_results)
        if cached is not None:
            log(f"Cache hit for: \"{query}\"")
            return cached.replace("Search results for:", "[Cached] Search results for:", 1) + loop_warning

        result = self._execute_search(_get_backend(), query, num_results)
        _search_cache.set(query, num_results, result)
        return result + loop_warning

    def _execute_search(self, backend: SearchBackend, query: str, num_results: int) -> str:
        try:
            results = backend.search(query, num_results)

            if not results:
                log(f"No results found for: \"{query}\"")
                return f"No results found for: {query}"

            already_fetched = _fetched.filter([r["url"] for r in results])
            results = [r for r in results if r["url"] not in already_fetched]
            skipped = num_results - len(results)
            log(f"Found {len(results)} results for: \"{query}\""
                + (f" ({skipped} already-fetched URLs removed)" if skipped else ""))

            _emit_stream({
                "type": "search_result",
                "query": query,
                "count": len(results),
                "results": [
                    {"title": r.get("title", ""), "url": r.get("url", ""), "category": classify(r.get("url", ""))}
                    for r in results[:8]
                ],
            })

            lines: list[str] = [f"Search results for: '{query}'\n"]
            for i, r in enumerate(results, 1):
                url = r["url"] or "N/A"
                lines.append(f"{i}. **{r['title'] or 'No title'}** {classify(url)}")
                lines.append(f"   URL: {url}")
                lines.append(f"   {r['snippet'] or 'No snippet available.'}")
                lines.append("")
            return "\n".join(lines)

        except Exception as exc:
            log(f"Search error: {exc}")
            return f"Search failed ({type(backend).__name__}): {exc}"


# ── FetchPageTool ──────────────────────────────────────────────────────────


class FetchPageTool(BaseTool):
    """Fetch and read the full text content of a specific webpage URL."""

    name: str = "fetch_webpage"
    description: str = (
        "Fetch and extract the readable text content from a specific URL. "
        "Use this after web_search to read the full content of a promising source. "
        "Returns the main body text, stripped of ads and navigation."
    )
    args_schema: Type[BaseModel] = FetchPageInput

    def _run(self, url: str) -> str:
        if _fetched.already_fetched(url):
            return f"Already fetched: {url} — use the content from earlier in this conversation."
        category = classify(url)
        log(f"Reading page: {url}")
        _emit_stream({"type": "fetch", "url": url, "category": category})
        try:
            resp = _http_session.get(url, timeout=12)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.content, "lxml")

            # Strip noisy elements
            for tag in soup(
                ["script", "style", "nav", "footer", "header",
                 "aside", "form", "noscript", "iframe", "svg"]
            ):
                tag.decompose()

            # Prefer <article> or <main> if available
            body = soup.find("article") or soup.find("main") or soup.body
            raw = body.get_text(separator="\n", strip=True) if body else ""

            # Collapse runs of blank lines
            text = _BLANK_LINES_RE.sub("\n\n", raw).strip()

            if not text:
                return f"No readable content found at {url}"

            limit = _budget.page_limit()
            if len(text) > limit:
                reason = "context budget" if limit < MAX_PAGE_CONTENT_LENGTH else "page size limit"
                text = text[:limit] + f"\n\n[... truncated at {limit} chars due to {reason} ...]"

            _budget.record(len(text))
            _fetched.mark(url)
            _diversity.record(category)

            # Emit a preview of the page content (first 400 chars of clean text)
            preview = " ".join(text[:600].split())[:400]
            _emit_stream({"type": "fetch_content", "url": url, "category": category, "preview": preview})

            footer = f"\n\nSource type: {category} | {_diversity.summary()}"
            footer += _budget.warning()
            footer += _diversity.nudge()
            return f"Content from {url}:\n\n{text}" + footer

        except requests.HTTPError as exc:
            return f"HTTP error fetching {url}: {exc}"
        except Exception as exc:
            return f"Failed to fetch {url}: {exc}"


# ── Plan / Notes / Draft tools ─────────────────────────────────────────────
# Agents call these to keep a live research workspace visible in the UI.


class _PlanInput(BaseModel):
    content: str = Field(description="Your current step-by-step research plan.")


class _NoteInput(BaseModel):
    content: str = Field(description="A key finding, fact, or observation to record.")


class _DraftInput(BaseModel):
    content: str = Field(description="The current working draft answer to the research query.")


class UpdatePlanTool(BaseTool):
    """Update the live research plan shown in the UI."""
    name: str = "update_plan"
    description: str = (
        "Update your research plan with your current strategy and next steps. "
        "Call this at the start of your research and whenever your strategy changes."
    )
    args_schema: Type[BaseModel] = _PlanInput

    def _run(self, content: str) -> str:
        _emit_stream({"type": "plan_update", "content": content})
        return "Plan updated."


class AddNoteTool(BaseTool):
    """Record a key finding or fact in the live notes panel."""
    name: str = "add_note"
    description: str = (
        "Record a key finding, verified fact, or important observation. "
        "Call this after reading each significant source to build a cumulative notes record."
    )
    args_schema: Type[BaseModel] = _NoteInput

    def _run(self, content: str) -> str:
        _emit_stream({"type": "note_add", "content": content})
        return "Note recorded."


class UpdateDraftTool(BaseTool):
    """Update the working draft answer shown live in the UI."""
    name: str = "update_draft"
    description: str = (
        "Update the working draft answer to the research query based on what you know so far. "
        "Call this periodically as your understanding develops."
    )
    args_schema: Type[BaseModel] = _DraftInput

    def _run(self, content: str) -> str:
        _emit_stream({"type": "draft_update", "content": content})
        return "Draft updated."
