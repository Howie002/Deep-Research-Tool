"""
source_classifier.py — Domain-based source quality classification and diversity tracking.

Classifies URLs into source categories so agents can make informed decisions
about credibility and coverage without being restricted to specific domains.

Categories (shown inline in search results):
  [Academic]       — peer-reviewed journals, university sites, research databases
  [Government]     — .gov / .mil / official public-sector sites
  [Non-profit/NGO] — .org and known non-profit organisations
  [News]           — established journalistic outlets
  [Professional]   — career/business networks and company filings
  [Reference]      — encyclopaedias and curated reference works
  [Social/UGC]     — user-generated content (lower inherent credibility)
  [Web]            — everything else (neutral)
"""
from __future__ import annotations

import threading
from urllib.parse import urlparse

# ── Domain lists ────────────────────────────────────────────────────────────

_ACADEMIC = {
    "researchgate.net", "academia.edu", "arxiv.org", "biorxiv.org",
    "medrxiv.org", "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov",
    "jstor.org", "semanticscholar.org", "ssrn.com", "plos.org",
    "nature.com", "sciencedirect.com", "springer.com", "springerlink.com",
    "wiley.com", "tandfonline.com", "oup.com", "cambridge.org",
    "journals.plos.org", "scholar.google.com", "scholar.google.co.uk",
    "cell.com", "thelancet.com", "nejm.org", "bmj.com",
    "jamanetwork.com", "frontiersin.org", "mdpi.com", "hindawi.com",
}

_NEWS = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk",
    "nytimes.com", "washingtonpost.com", "theguardian.com", "guardian.com",
    "npr.org", "cnn.com", "wsj.com", "ft.com", "bloomberg.com",
    "economist.com", "theatlantic.com", "politico.com", "axios.com",
    "forbes.com", "techcrunch.com", "wired.com", "arstechnica.com",
    "engadget.com", "theverge.com", "nbcnews.com", "abcnews.go.com",
    "cbsnews.com", "usatoday.com", "time.com", "newsweek.com",
    "thehill.com", "rollcall.com", "statesman.com",
}

_REFERENCE = {
    "wikipedia.org", "britannica.com", "investopedia.com",
    "merriam-webster.com", "dictionary.com", "wolframalpha.com",
}

_SOCIAL_UGC = {
    "reddit.com", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "tiktok.com", "youtube.com", "quora.com",
    "medium.com", "substack.com", "tumblr.com", "pinterest.com",
    "linkedin.com/posts", "threads.net",
}

_PROFESSIONAL = {
    "linkedin.com", "glassdoor.com", "crunchbase.com",
    "bloomberg.com/profile", "sec.gov/cgi-bin",
}

# ── Classifier ──────────────────────────────────────────────────────────────


def classify(url: str) -> str:
    """Return a short category tag for the given URL."""
    try:
        hostname = (urlparse(url).hostname or "").lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]
    except Exception:
        return "[Web]"

    # TLD-based — most reliable signal
    if hostname.endswith(".gov") or hostname.endswith(".mil"):
        return "[Government]"
    if hostname.endswith(".edu"):
        return "[Academic]"

    # Known-domain matching (exact or subdomain)
    def _matches(domain_set: set[str]) -> bool:
        return any(
            hostname == d or hostname.endswith("." + d)
            for d in domain_set
        )

    if _matches(_ACADEMIC):
        return "[Academic]"
    if _matches(_SOCIAL_UGC):
        return "[Social/UGC]"
    if _matches(_PROFESSIONAL):
        return "[Professional]"
    if _matches(_NEWS):
        return "[News]"
    if _matches(_REFERENCE):
        return "[Reference]"

    if hostname.endswith(".org"):
        return "[Non-profit/NGO]"

    return "[Web]"


# ── Diversity tracker ───────────────────────────────────────────────────────

_HIGH_QUALITY = {"[Academic]", "[Government]", "[Non-profit/NGO]"}
_LOW_QUALITY  = {"[Social/UGC]"}


class SourceDiversityTracker:
    """
    Tracks the category breakdown of fetched sources and returns
    actionable diversity nudges to help agents self-correct.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._total: int = 0
        self._lock = threading.Lock()

    def record(self, category: str) -> None:
        with self._lock:
            self._counts[category] = self._counts.get(category, 0) + 1
            self._total += 1

    def summary(self) -> str:
        """One-line breakdown for appending to fetch output."""
        with self._lock:
            if self._total == 0:
                return ""
            parts = [f"{cat}: {n}" for cat, n in sorted(self._counts.items())]
            return f"Sources fetched so far — {', '.join(parts)}"

    def nudge(self) -> str:
        """
        Return an actionable suggestion if the source mix looks imbalanced.
        Empty string if the mix looks fine.
        """
        with self._lock:
            total = self._total
            counts = dict(self._counts)

        if total < 2:
            return ""

        suggestions: list[str] = []

        high_q = sum(counts.get(c, 0) for c in _HIGH_QUALITY)
        low_q  = sum(counts.get(c, 0) for c in _LOW_QUALITY)

        if high_q == 0 and total >= 3:
            suggestions.append(
                "No academic, government, or NGO sources fetched yet. "
                "Try searching on Google Scholar, PubMed, arXiv, or adding "
                "'site:.gov' or 'site:.edu' to your next query for more authoritative results."
            )
        elif high_q < total * 0.2 and total >= 5:
            suggestions.append(
                f"Only {high_q}/{total} sources are academic/government. "
                "Consider targeting more authoritative sources."
            )

        if low_q > total * 0.4:
            suggestions.append(
                f"{low_q}/{total} sources are social/UGC (Reddit, Twitter, etc.). "
                "These carry lower inherent credibility — seek primary or peer-reviewed sources to corroborate."
            )

        if "[News]" in counts and counts["[News]"] == total and total >= 3:
            suggestions.append(
                "All sources are news articles. "
                "Consider finding primary sources (official reports, academic papers, official statements)."
            )

        if not suggestions:
            return ""

        return "\n\n📊 Source diversity note: " + " ".join(suggestions)
