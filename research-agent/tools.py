"""
tools.py — CrewAI tools for web searching and page fetching.

Search backends supported (configure via SEARCH_BACKEND env var):
  - duckduckgo  (default, no API key required)
  - brave       (requires BRAVE_API_KEY; falls back to duckduckgo if missing)
  - serpapi     (requires SERPAPI_KEY; falls back to duckduckgo if missing)
  - langsearch  (requires LANGSEARCH_API_KEY; falls back to duckduckgo if missing)
"""
from __future__ import annotations

import json as _json
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
    LM_STUDIO_API_KEY,
    LM_STUDIO_BASE_URL,
    LM_STUDIO_MODEL,
    MAX_PAGE_CONTENT_LENGTH,
    MAX_SEARCH_RESULTS,
    SEARCH_BACKEND,
    SEARCH_CACHE_TTL_SECONDS,
    SEARXNG_URL,
    SERPAPI_KEY,
    THOROUGH_MODE,
)
from scratchpad import log
from source_classifier import SourceDiversityTracker, classify

# ── Stream emitter ─────────────────────────────────────────────────────────
# Configured once per worker process by calling set_stream_emitter().
# All tools call _emit_stream() to send real-time events to the UI.

_stream_emitter = None

# Serialises the sink callbacks (stream-event append + fetched-content persist).
# The batched-round executor fetches/evaluates K results in worker threads, so
# these callbacks fire concurrently; both append to per-job files and would
# interleave/corrupt without a lock. The lock only guards the (fast) sink call,
# not the network fetch, so it doesn't serialise the actual I/O.
_sink_lock = threading.Lock()


def set_stream_emitter(emit_fn) -> None:
    """Register a function(event: dict) that forwards events to the .stream file."""
    global _stream_emitter
    _stream_emitter = emit_fn


def _emit_stream(event: dict) -> None:
    if _stream_emitter:
        try:
            with _sink_lock:
                _stream_emitter(event)
        except Exception:
            pass


# ── Fetched-content persister ─────────────────────────────────────────────
# The worker registers a callback that writes full-text fetches to a per-job
# JSONL cache; grounding.py reads from this cache at post-synth time to
# verify citations against what was actually fetched. Without this, only
# 400-char previews are persisted (in stream events) which isn't enough to
# ground claims.

_fetch_persister = None              # fn(url, title, category, text)
_subject_query: str | None = None    # set by worker; used by thorough-mode classifier
_workspace_reader = None             # fn() -> dict, set by worker


def set_fetch_persister(persist_fn) -> None:
    global _fetch_persister
    _fetch_persister = persist_fn


def set_subject_query(query: str) -> None:
    """Store the top-level research query so per-page classification can
    judge 'does this page actually discuss the subject?' rather than just
    'is this page relevant to the current sub-query?'."""
    global _subject_query
    _subject_query = (query or "").strip()


def set_workspace_reader(reader_fn) -> None:
    """Register a zero-arg callable that returns the current workspace state
    as {"plan": str, "notes": [str], "draft": str, "sources": [str]}.
    Used by ReadWorkspaceTool so downstream stages can get the canonical
    research record even if the previous stage's handoff string was garbage.
    """
    global _workspace_reader
    _workspace_reader = reader_fn


def _persist_fetched(url: str, title: str, category: str, text: str) -> None:
    if _fetch_persister:
        try:
            with _sink_lock:
                _fetch_persister(url, title, category, text)
        except Exception:
            pass

# ── Thorough-mode classifier ───────────────────────────────────────────────
# When THOROUGH_MODE is on, every search result is judged by a tight LLM
# call: "would this page help answer <query>? yes/no + one-line reason".
# Rejected URLs are dropped before the researcher ever sees them; verdicts
# are streamed so the branch tree surfaces the full audit trail.

_CLASSIFY_SYSTEM = (
    "You judge whether a web search result is likely to help answer a specific research query. "
    "Output STRICT JSON and nothing else: "
    '{"verdict":"useful","reason":"<one short sentence>"} '
    'OR {"verdict":"reject","reason":"<one short sentence>"}. '
    "Be strict — if the title and snippet do not clearly indicate relevant primary information, say reject."
)


def _classify_usefulness(query: str, result: dict) -> dict:
    """Return {"verdict": "useful"|"reject", "reason": str}. Never raises.

    Fails *open* (useful, reason="verdict unavailable") if the classifier
    can't be reached — better to let a run continue than block every search
    because LM Studio glitched.
    """
    title = (result.get("title") or "").strip()[:300]
    url = (result.get("url") or "").strip()[:500]
    snippet = (result.get("snippet") or "").strip()[:600]
    user = (
        f"Research query: \"{query}\"\n\n"
        f"Result:\n  Title: {title}\n  URL: {url}\n  Snippet: {snippet}\n\n"
        "Is this result likely to help answer the query? Output only the JSON verdict."
    )
    payload = {
        "model": LM_STUDIO_MODEL,
        "messages": [
            {"role": "system", "content": _CLASSIFY_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 120,
    }
    try:
        resp = requests.post(
            LM_STUDIO_BASE_URL.rstrip("/") + "/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LM_STUDIO_API_KEY}"},
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    except Exception:
        return {"verdict": "useful", "reason": "verdict unavailable (classifier error)"}

    # Strip fences, then try direct JSON; fall back to regex-extracted object.
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw, flags=re.IGNORECASE).strip()
    parsed: dict | None = None
    try:
        parsed = _json.loads(cleaned)
    except Exception:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                parsed = _json.loads(m.group(0))
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        return {"verdict": "useful", "reason": "verdict unavailable (parse failed)"}
    verdict = str(parsed.get("verdict", "")).strip().lower()
    reason = str(parsed.get("reason", "")).strip() or "no reason given"
    if verdict not in ("useful", "reject"):
        return {"verdict": "useful", "reason": f"verdict unavailable (got '{verdict}')"}
    return {"verdict": verdict, "reason": reason[:300]}


_PAGE_VERDICT_SYSTEM = (
    "You audit whether a web page actually discusses a specific research subject. "
    "Output STRICT JSON and nothing else: "
    '{"verdict":"on_topic","reason":"<short sentence summarising what the page says about the subject>"} '
    'OR {"verdict":"off_topic","reason":"<short sentence explaining what the page is about and why it does NOT discuss the subject>"}. '
    "Be strict: topical adjacency is not enough — the page must contain information ABOUT the subject itself."
)


def _classify_page_about_subject(subject: str, url: str, page_text: str) -> dict:
    """Does this fetched page actually discuss <subject>? Never raises; fails
    open with 'on_topic' so a classifier glitch doesn't derail research."""
    body = (page_text or "").strip()
    if len(body) > 5000:
        body = body[:5000] + "\n…[truncated for classifier]"
    user = (
        f"SUBJECT: {subject}\n\nURL: {url}\n\nPAGE CONTENT:\n{body}\n\n"
        "Does this page contain information ABOUT the subject? Output only the JSON verdict."
    )
    payload = {
        "model": LM_STUDIO_MODEL,
        "messages": [
            {"role": "system", "content": _PAGE_VERDICT_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 150,
    }
    try:
        resp = requests.post(
            LM_STUDIO_BASE_URL.rstrip("/") + "/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LM_STUDIO_API_KEY}"},
            timeout=45,
        )
        resp.raise_for_status()
        raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "") or ""
    except Exception:
        return {"verdict": "on_topic", "reason": "verdict unavailable (classifier error)"}

    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw, flags=re.IGNORECASE).strip()
    parsed: dict | None = None
    try:
        parsed = _json.loads(cleaned)
    except Exception:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                parsed = _json.loads(m.group(0))
            except Exception:
                parsed = None
    if not isinstance(parsed, dict):
        return {"verdict": "on_topic", "reason": "verdict unavailable (parse failed)"}
    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in ("on_topic", "off_topic"):
        return {"verdict": "on_topic", "reason": f"verdict unavailable (got '{verdict}')"}
    return {"verdict": verdict, "reason": str(parsed.get("reason", "")).strip()[:300]}


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

# SearXNG scrapes upstream engines (Google/DDG/Brave/Startpage) that CAPTCHA / suspend
# the scraper under bursty load — a deep run firing 10-40 searches quickly knocks most
# engines offline, collapsing results to a single engine. A global min-interval throttle
# keeps the request rate low enough that the engines stay responsive across a whole run.
_searxng_lock = threading.Lock()
_searxng_next_ok = [0.0]          # earliest epoch time the next SearXNG request may go out
_SEARXNG_MIN_INTERVAL = 2.5       # seconds enforced between SearXNG requests


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


class SearXNGBackend(SearchBackend):
    """Self-hosted SearXNG metasearch — privacy-respecting, fully local, no API key.

    Throttled + retried: a global min-interval throttle (see _SEARXNG_MIN_INTERVAL)
    spaces requests so SearXNG's upstream engines don't rate-limit/CAPTCHA the scraper
    during a deep run. On a degraded/empty response we back off and retry, giving any
    transiently-suspended engines a chance to recover.
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def _throttle(self) -> None:
        with _searxng_lock:
            now = time.time()
            wait = max(0.0, _searxng_next_ok[0] - now)
            _searxng_next_ok[0] = now + wait + _SEARXNG_MIN_INTERVAL
        if wait > 0:
            time.sleep(wait)

    def search(self, query: str, max_results: int) -> list[dict]:
        last_err = "unknown"
        for attempt in range(3):
            self._throttle()
            try:
                response = requests.get(
                    f"{self._base_url}/search",
                    params={"q": query, "format": "json", "categories": "general"},
                    headers={"Accept": "application/json"},
                    timeout=20,
                )
                response.raise_for_status()
                data = response.json()
                results = data.get("results", [])
                if results:
                    unresponsive = data.get("unresponsive_engines") or []
                    if unresponsive:
                        log(f"SearXNG: {len(unresponsive)} engine(s) unresponsive this query")
                    return [
                        {
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "snippet": r.get("content", ""),
                        }
                        for r in results[:max_results]
                    ]
                last_err = "0 results (engines likely suspended)"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(2 ** attempt)  # backoff 1s, 2s, 4s — let suspended engines recover
        log(f"WARNING: SearXNG search failed after 3 attempts ({last_err})")
        return []


# Domains we never fetch or cite — AI-generated wiki mirrors and content farms
# that present as reference sources but aren't editorially accountable.
# Matched on the registered domain (suffix), so subdomains are covered too.
LOW_CREDIBILITY_DOMAINS: frozenset[str] = frozenset({
    "grokipedia.com",      # AI-generated Wikipedia mirror
    "ask.com",
    "answers.com",
})


def _is_low_credibility(url: str) -> bool:
    """True if `url`'s host is on the low-credibility denylist (suffix match,
    so `www.` and other subdomains are covered)."""
    try:
        from urllib.parse import urlsplit
        host = urlsplit(url.strip()).netloc.lower().split(":")[0]
    except Exception:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in LOW_CREDIBILITY_DOMAINS)


def _get_backend() -> SearchBackend:
    """Return the configured search backend, falling back to DuckDuckGo if the required key is missing."""
    if SEARCH_BACKEND == "searxng":
        if SEARXNG_URL:
            return SearXNGBackend(SEARXNG_URL)
        log("WARNING: SEARXNG_URL not set — falling back to DuckDuckGo")
        return DuckDuckGoBackend()
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
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Referer": "https://www.google.com/",
})

# Precompiled regex for collapsing blank lines in fetched page content
_BLANK_LINES_RE = re.compile(r"\n{3,}")

# ── Trafilatura (optional, higher-quality extractor) ───────────────────────
try:
    import trafilatura as _trafilatura
    _TRAFILATURA_OK = True
except ImportError:
    _trafilatura = None  # type: ignore[assignment]
    _TRAFILATURA_OK = False

# ── Gate / paywall detection ───────────────────────────────────────────────
# Phrases that indicate a login wall or subscription gate.
_GATE_RE = re.compile(
    r"sign[\s-]?in to (view|continue|access|read)|log[\s-]?in (to|required)|login required|please log in"
    r"|create an account|join (with email|linkedin|researchgate|academia)"
    r"|subscription required|subscribe to (read|access|continue|view)"
    r"|access denied|members only|purchase required|premium (content|access|article)"
    r"|to continue reading|unlock this (article|content|page)"
    r"|register (for free|to (read|access))|you (must|need to) sign in"
    r"|this content is (for|only|behind|exclusively)"
    r"|ieee account|change username|update address|purchase details"
    r"|500\+ connections|2k followers|sign in to view.*full profile"
    r"|already on linkedin|join linkedin|connect with professionals"
    r"|researchgate.*sign up|academia\.edu.*sign up"
    r"|by clicking continue.*agree|user agreement.*privacy policy",
    re.IGNORECASE | re.DOTALL,
)


def _is_gated(text: str) -> bool:
    """Return True when extracted text looks like a login wall or paywall."""
    if len(text.strip()) < 300:
        return True
    hits = len(_GATE_RE.findall(text[:1200]))
    return hits >= 2


# ── Fetch helpers ──────────────────────────────────────────────────────────

def _extract_bs4(html_bytes: bytes) -> str:
    """Extract readable text from raw HTML using BeautifulSoup."""
    soup = BeautifulSoup(html_bytes, "lxml")
    for tag in soup(
        ["script", "style", "nav", "footer", "header",
         "aside", "form", "noscript", "iframe", "svg"]
    ):
        tag.decompose()
    body = soup.find("article") or soup.find("main") or soup.body
    raw = body.get_text(separator="\n", strip=True) if body else ""
    return _BLANK_LINES_RE.sub("\n\n", raw).strip()


def _extract_pdf(content_bytes: bytes, max_chars: int = 40_000) -> str | None:
    """Extract plain text from a PDF byte string using pdfplumber.

    Returns None on failure or if the result is empty / too short to be useful.
    """
    try:
        import io
        import pdfplumber
        parts: list[str] = []
        with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text(x_tolerance=2, y_tolerance=3) or ""
                text = text.strip()
                if text:
                    parts.append(text)
                if sum(len(p) for p in parts) >= max_chars:
                    break
        result = "\n\n".join(parts).strip()
        return result if len(result) >= 100 else None
    except Exception:
        return None


def _fetch_with_trafilatura(url: str) -> str | None:
    """Fetch and extract via trafilatura — handles many paywalls and JS-heavy pages."""
    if not _TRAFILATURA_OK:
        return None
    try:
        downloaded = _trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = _trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            favor_recall=True,
        )
        return text or None
    except Exception:
        return None


def _fetch_wayback(url: str, limit: int) -> str | None:
    """
    Fall back to the Wayback Machine (archive.org) for gated or blocked pages.
    Returns extracted text prefixed with the snapshot URL, or None if unavailable.
    """
    try:
        avail = requests.get(
            "https://archive.org/wayback/available",
            params={"url": url},
            timeout=8,
        )
        snapshot = avail.json().get("archived_snapshots", {}).get("closest", {})
        if not snapshot.get("available"):
            return None
        snap_url = snapshot["url"]
        resp = _http_session.get(snap_url, timeout=18)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "lxml")
        # Remove Wayback Machine toolbar injected into every page
        for tag in soup.find_all(id=lambda i: i and "wm-ipp" in i):
            tag.decompose()
        text = _extract_bs4(resp.content)
        if not text or len(text) < 200:
            return None
        if len(text) > limit:
            text = text[:limit] + f"\n\n[... truncated at {limit} chars ...]"
        return f"[Wayback Machine snapshot: {snap_url}]\n\n{text}"
    except Exception:
        return None


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

            # Source-credibility filter: drop known low-credibility domains
            # (AI-generated wiki mirrors, content farms) before they can be
            # fetched or cited. Keeps the report's sources defensible.
            before = len(results)
            results = [r for r in results if not _is_low_credibility(r.get("url", ""))]
            dropped = before - len(results)
            if dropped:
                log(f"Dropped {dropped} low-credibility source(s) for: \"{query}\"")

            already_fetched = _fetched.filter([r["url"] for r in results])
            results = [r for r in results if r["url"] not in already_fetched]
            skipped = num_results - len(results)
            log(f"Found {len(results)} results for: \"{query}\""
                + (f" ({skipped} already-fetched URLs removed)" if skipped else ""))

            # Thorough mode: LLM-classify every result for usefulness before
            # it reaches the researcher. Rejected URLs are dropped from the
            # list entirely — the researcher cannot fetch them. Every verdict
            # (accept AND reject) is streamed so the branch tree is complete.
            verdicts: dict[str, dict] = {}
            if THOROUGH_MODE and results:
                log(f"Thorough mode: classifying {len(results)} result(s) for usefulness…")
                kept: list[dict] = []
                for r in results:
                    v = _classify_usefulness(query, r)
                    verdicts[r.get("url", "")] = v
                    _emit_stream({
                        "type": "resource_verdict",
                        "query": query,
                        "url": r.get("url", ""),
                        "title": r.get("title", ""),
                        "verdict": v["verdict"],
                        "reason": v["reason"],
                    })
                    if v["verdict"] == "useful":
                        kept.append(r)
                dropped = len(results) - len(kept)
                if dropped:
                    log(f"Thorough mode: rejected {dropped} of {len(results)} result(s) as unlikely to help.")
                results = kept
                if not results:
                    return (
                        f"Search results for: '{query}'\n"
                        "(all results were classified as unlikely to help — try a broader or differently-worded query)"
                    )

            _emit_stream({
                "type": "search_result",
                "query": query,
                "count": len(results),
                "results": [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "category": classify(r.get("url", "")),
                        "snippet": r.get("snippet", ""),
                    }
                    for r in results[:8]
                ],
            })

            lines: list[str] = [f"Search results for: '{query}'\n"]
            for i, r in enumerate(results, 1):
                url = r["url"] or "N/A"
                lines.append(f"{i}. **{r['title'] or 'No title'}** {classify(url)}")
                lines.append(f"   URL: {url}")
                lines.append(f"   {r['snippet'] or 'No snippet available.'}")
                verdict = verdicts.get(url)
                if verdict:
                    lines.append(f"   [Thorough verdict: USEFUL — {verdict['reason']}]")
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

        limit = _budget.page_limit()
        text: str | None = None
        source_tag = ""

        # ── Strategy 1: trafilatura (best at handling paywalls + JS pages) ──
        text = _fetch_with_trafilatura(url)
        if text and not _is_gated(text):
            source_tag = "[trafilatura]"
            log(f"trafilatura extracted {len(text)} chars from {url}")
        else:
            traf_text = text  # keep trafilatura output as last-resort fallback

            # ── Strategy 2: direct requests + BeautifulSoup (or PDF) ─────
            text = None
            try:
                resp = _http_session.get(url, timeout=14)
                resp.raise_for_status()
                content_type = resp.headers.get("Content-Type", "").lower()
                is_pdf = "application/pdf" in content_type or resp.content[:5] == b"%PDF-"
                if is_pdf:
                    pdf_text = _extract_pdf(resp.content, max_chars=limit)
                    if pdf_text:
                        text = pdf_text
                        source_tag = "[PDF]"
                        log(f"PDF extracted {len(text)} chars from {url}")
                    else:
                        log(f"PDF extraction returned no text for {url}")
                else:
                    bs_text = _extract_bs4(resp.content)
                    if bs_text and not _is_gated(bs_text):
                        text = bs_text
                        source_tag = ""
                        log(f"BeautifulSoup extracted {len(text)} chars from {url}")
                    elif bs_text:
                        log(f"BeautifulSoup got gated content ({len(bs_text)} chars) for {url}")
            except requests.HTTPError as exc:
                log(f"HTTP error for {url}: {exc}")
            except Exception as exc:
                log(f"Request failed for {url}: {exc}")

            # ── Strategy 3: Wayback Machine fallback (HTML only) ─────────
            # Skip Wayback for PDFs — it would serve the same binary file.
            if not text or (not is_pdf and _is_gated(text)):
                log(f"Trying Wayback Machine for {url}")
                wb = _fetch_wayback(url, limit)
                if wb and not _is_gated(wb):
                    text = wb
                    source_tag = "[Wayback Machine archive]"
                    log(f"Wayback Machine returned {len(text)} chars for {url}")
                else:
                    # All strategies returned gated content — do NOT pass login walls to the LLM.
                    # Returning an explicit error prevents the agent from writing notes on login pages.
                    log(f"All strategies returned gated content for {url} — skipping")
                    return (
                        f"⚠ Gated/paywalled page — no content extracted: {url}\n"
                        f"This page requires login or a subscription. "
                        f"Skip this source and focus on other results."
                    )

        if not text:
            return f"No readable content found at {url}"

        if len(text) > limit:
            reason = "context budget" if limit < MAX_PAGE_CONTENT_LENGTH else "page size limit"
            text = text[:limit] + f"\n\n[... truncated at {limit} chars due to {reason} ...]"

        # Shared-tracker mutations — guarded so concurrent batched fetches don't
        # lose-update these counters. Fast critical section; the network I/O above
        # ran unlocked, so fetches still overlap.
        with _sink_lock:
            _budget.record(len(text))
            _fetched.mark(url)
            _diversity.record(category)

        # Persist the full extracted text to a per-job cache so the
        # post-synth grounding validator can check whether citations to
        # this URL are actually supported by the page content.
        _persist_fetched(url, url, category, text)

        preview = " ".join(text[:600].split())[:400]
        _emit_stream({"type": "fetch_content", "url": url, "category": category, "preview": preview})

        # Thorough mode (per-page): judge whether this page actually
        # discusses the top-level research subject. If the classifier says
        # no, we leave the content in the fetch cache (for audit trail)
        # but tell the agent outright so it won't write notes off a page
        # that doesn't discuss the subject. Fails open on classifier error.
        verdict_footer = ""
        if THOROUGH_MODE and _subject_query and text.strip():
            page_verdict = _classify_page_about_subject(_subject_query, url, text)
            _emit_stream({
                "type": "page_verdict",
                "url": url,
                "subject": _subject_query,
                "verdict": page_verdict["verdict"],
                "reason": page_verdict["reason"],
            })
            if page_verdict["verdict"] == "off_topic":
                verdict_footer = (
                    f"\n\n⚠ THOROUGH-MODE PAGE VERDICT: This page does not appear to discuss "
                    f"'{_subject_query}' ({page_verdict['reason']}). "
                    "Do NOT write notes attributing claims about the subject to this page."
                )

        footer = f"\n\nSource type: {category} | {_diversity.summary()}"
        if source_tag:
            footer = f"\n\n{source_tag}" + footer
        footer += _budget.warning()
        footer += _diversity.nudge()
        footer += verdict_footer
        return f"Content from {url}:\n\n{text}" + footer


# ── Plan / Notes / Draft tools ─────────────────────────────────────────────
# Agents call these to keep a live research workspace visible in the UI.


class _PlanInput(BaseModel):
    content: str = Field(description="Your current step-by-step research plan.")


class _NoteInput(BaseModel):
    content: str = Field(description="A key finding, fact, or observation to record.")
    source_url: str = Field(
        default="",
        description="The URL of the page this note was extracted from (must be a URL you actually fetched this run).",
    )
    quote: str = Field(
        default="",
        description="A short verbatim quote (under 300 chars) from the source page that supports this note. Required when citing a URL.",
    )


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

    def _run(self, content: str, source_url: str = "", quote: str = "") -> str:
        event: dict = {"type": "note_add", "content": content}
        if source_url:
            event["source_url"] = source_url
        if quote:
            event["quote"] = quote[:600]
        _emit_stream(event)
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


# ── Workspace reader tool ─────────────────────────────────────────────────
# Downstream agents (Critical Analyst, Gap Analyst, Synthesizer) can call
# this to read the canonical research record the Researcher actually built,
# regardless of whatever garbage the previous task's raw output string
# contains. This is the belt to #1's braces: even if the stage handoff
# passes degenerate text, the agent can request the workspace and recover.


class _WorkspaceReadInput(BaseModel):
    sections: str = Field(
        default="plan,notes,draft,sources",
        description="Comma-separated list of sections to read: plan, notes, draft, sources (default: all).",
    )


class ReadWorkspaceTool(BaseTool):
    """Read the live research workspace (plan, notes, draft, sources)."""

    name: str = "read_workspace"
    description: str = (
        "Read the current research workspace — the Researcher's plan, all recorded notes "
        "with their source URLs, the working draft, and the list of confirmed-fetched sources. "
        "ALWAYS call this at the start of your task to see what the Researcher actually found, "
        "rather than relying solely on the prior stage's summary text (which may be truncated "
        "or corrupted). Accepts a `sections` argument to limit output; default returns everything."
    )
    args_schema: Type[BaseModel] = _WorkspaceReadInput

    def _run(self, sections: str = "plan,notes,draft,sources") -> str:
        if not _workspace_reader:
            return "Workspace reader not configured. The pipeline is running without persistent workspace access."
        try:
            ws = _workspace_reader() or {}
        except Exception as exc:
            return f"Workspace read failed: {exc}"

        wanted = {s.strip().lower() for s in (sections or "").split(",") if s.strip()}
        if not wanted:
            wanted = {"plan", "notes", "draft", "sources"}

        parts: list[str] = []
        if "plan" in wanted:
            plan = (ws.get("plan") or "").strip()
            parts.append("## PLAN\n" + (plan or "_(no plan recorded yet)_"))
        if "notes" in wanted:
            notes = ws.get("notes") or []
            if notes:
                body = "\n\n".join(f"### Note {i + 1}\n{n}" for i, n in enumerate(notes))
            else:
                body = "_(no notes recorded yet)_"
            parts.append("## NOTES\n" + body)
        if "draft" in wanted:
            draft = (ws.get("draft") or "").strip()
            parts.append("## WORKING DRAFT\n" + (draft or "_(no draft yet)_"))
        if "sources" in wanted:
            sources = ws.get("sources") or []
            if sources:
                lines = [f"- {s}" for s in sources]
                body = "\n".join(lines)
            else:
                body = "_(no sources confirmed-fetched)_"
            parts.append("## FETCHED SOURCES (confirmed-read URLs only)\n" + body)
        return "\n\n".join(parts)


# ── Thought narration tool ─────────────────────────────────────────────────


class _ThoughtInput(BaseModel):
    label: str = Field(
        description="Short phrase (5–15 words) describing the current research thread or insight being pursued."
    )
    rationale: str = Field(
        default="",
        description="One sentence explaining why you are pursuing this angle or what you just discovered.",
    )


class ThoughtNodeTool(BaseTool):
    """Narrate reasoning pivots as they happen — builds a reasoning trail visible in the UI."""

    name: str = "record_thought"
    description: str = (
        "Record your current reasoning thread — what you are investigating and why. "
        "Call this before starting each new search angle, when you make a surprising discovery, "
        "or when you pivot direction. Each call creates a labelled branch in the reasoning trail."
    )
    args_schema: Type[BaseModel] = _ThoughtInput

    def _run(self, label: str, rationale: str = "") -> str:
        import uuid
        _emit_stream({
            "type":      "thought_node",
            "id":        str(uuid.uuid4())[:8],
            "label":     label.strip(),
            "rationale": rationale.strip(),
        })
        return "Thought recorded."
