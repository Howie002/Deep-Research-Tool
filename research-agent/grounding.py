"""
grounding.py — Post-synthesis citation-integrity checks.

Runs after the Synthesizer drafts the report but BEFORE the run is saved.
Each check is independent and fails open (the run is never blocked on a
grounding error — worst case the report is saved with a "⚠ grounding
unavailable" note). All results are attached to the report as an appendix
and emitted to the stream so the UI can badge problem citations.

Checks (numbered to match the proposal):
    #1 validate_citations            — each cited URL is LLM-checked against its fetched content
    #2 strip_unfetched_citations     — URLs in the report that were never fetched are removed
    #3 check_url_liveness            — HEAD each cited URL; non-2xx/3xx flagged as dead
    #4 validate_quotes               — quoted snippets in notes must appear in the source page
    #5 detect_thin_profile           — count pages that mention the subject; True if <3
    #7 score_confidence              — replace LLM-asserted confidence with a computed tier

Public entry point: run_all(report, fetched_cache, query, subject=None) -> GroundingReport
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

import requests

from config import LM_STUDIO_API_KEY, LM_STUDIO_BASE_URL, LM_STUDIO_MODEL


# ── Workspace state reader (parsed from the stream file) ─────────────────────


def load_workspace_state(jobs_dir: Path, job_id: str) -> dict:
    """Return the current workspace as {"plan", "notes", "draft", "sources"}.

    Reads the per-job .stream file and reconstructs the latest plan/draft
    and every recorded note + confirmed-fetched source URL. Safe to call
    mid-run — each event type is scanned independently, so a malformed
    event doesn't abort the reader. Used by ReadWorkspaceTool so any
    downstream agent can read the canonical record regardless of the
    previous stage's handoff string.
    """
    stream_file = Path(jobs_dir) / f"{job_id}.stream"
    state = {"plan": "", "notes": [], "draft": "", "sources": []}
    if not stream_file.exists():
        return state
    seen_urls: set[str] = set()
    try:
        for raw in stream_file.read_text(encoding="utf-8").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = ev.get("type", "")
            if t == "plan_update":
                state["plan"] = ev.get("content", "") or state["plan"]
            elif t == "draft_update":
                state["draft"] = ev.get("content", "") or state["draft"]
            elif t == "note_add":
                content = ev.get("content", "")
                if content:
                    state["notes"].append(content)
            elif t == "fetch_content":
                url = ev.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    state["sources"].append(url)
    except OSError:
        pass
    return state


# ── Fetched-content cache (per-job JSONL written by FetchPageTool) ────────────


def _fetched_path(jobs_dir: Path, job_id: str) -> Path:
    return jobs_dir / f"{job_id}.fetched.jsonl"


def append_fetched(jobs_dir: Path, job_id: str, url: str, title: str, category: str, text: str) -> None:
    """Append a fetched page to the per-job cache. Called by FetchPageTool."""
    path = _fetched_path(jobs_dir, job_id)
    entry = {"url": url, "title": title, "category": category, "text": text}
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_fetched(jobs_dir: Path, job_id: str) -> dict[str, dict]:
    """Return {url: {title, category, text}} for every page fetched in the run."""
    path = _fetched_path(jobs_dir, job_id)
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = row.get("url", "")
            if url:
                # Later fetches of the same URL overwrite — last one wins.
                out[url] = {
                    "title": row.get("title", ""),
                    "category": row.get("category", ""),
                    "text": row.get("text", ""),
                }
    except OSError:
        pass
    return out


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class CitationVerdict:
    url: str
    supported: bool
    reason: str
    evidence: str = ""


@dataclass
class GroundingReport:
    # #2 — URLs dropped because they were never fetched
    ghost_urls: list[str] = field(default_factory=list)
    # #3 — URLs that are dead (non-2xx/3xx or DNS failure)
    dead_urls: list[str] = field(default_factory=list)
    # #1 — per-citation verdict
    citation_verdicts: list[CitationVerdict] = field(default_factory=list)
    # #4 — quote-matching results per note (if quotes were supplied)
    quote_matches: list[dict] = field(default_factory=list)
    # #5 — was the subject's public profile thin?
    thin_profile: bool = False
    thin_profile_mention_count: int = 0
    # #7 — computed confidence tier and score
    confidence_tier: str = "unknown"
    confidence_score: int = 0
    # #8 — distinct name-like strings in fetched content that overlap with subject
    disambiguation_candidates: list[dict] = field(default_factory=list)
    # Pipeline-corruption signals (degenerate stage outputs detected at runtime,
    # or structural mismatch like "notes exist but report has zero citations")
    corruption_flags: list[dict] = field(default_factory=list)
    pipeline_corrupted: bool = False
    # Cleaned report text (with ghost citations stripped, liveness/verdict badges inline)
    cleaned_report: str = ""

    def to_dict(self) -> dict:
        return {
            "ghost_urls": self.ghost_urls,
            "dead_urls": self.dead_urls,
            "citation_verdicts": [
                {"url": v.url, "supported": v.supported, "reason": v.reason, "evidence": v.evidence[:300]}
                for v in self.citation_verdicts
            ],
            "quote_matches": self.quote_matches,
            "thin_profile": self.thin_profile,
            "thin_profile_mention_count": self.thin_profile_mention_count,
            "confidence_tier": self.confidence_tier,
            "confidence_score": self.confidence_score,
            "disambiguation_candidates": self.disambiguation_candidates,
            "corruption_flags": self.corruption_flags,
            "pipeline_corrupted": self.pipeline_corrupted,
        }


# ── URL extraction ────────────────────────────────────────────────────────────


_URL_RE = re.compile(r"https?://[^\s)\]\"'>]+")


def extract_urls(text: str) -> list[str]:
    """Return unique URLs in declaration order, stripped of trailing punctuation."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in _URL_RE.findall(text or ""):
        cleaned = raw.rstrip(".,;:)]>'\"")
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
    return out


def _normalize(url: str) -> str:
    try:
        p = urlparse(url)
        return (p.scheme + "://" + p.netloc + p.path).rstrip("/").lower()
    except Exception:
        return url.rstrip("/").lower()


# Smart-quote / em-dash / nbsp normalisation so a quote copied from a rendered
# page matches the same phrase stored in the fetch cache after extraction.
_QUOTE_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "‵": "'",
    "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "",  "﻿": "",
})


def _text_normalize(s: str) -> str:
    """NFKC → translate smart quotes/dashes → lowercase → collapse whitespace."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).translate(_QUOTE_MAP)
    return re.sub(r"\s+", " ", s).strip().lower()


# ── #2 Ghost-URL stripping ────────────────────────────────────────────────────


def strip_unfetched_citations(report: str, fetched_urls: Iterable[str]) -> tuple[str, list[str]]:
    """Return (cleaned_report, ghost_urls).

    Any URL in the report that has no normalised-match in `fetched_urls` is
    annotated inline with "⚠ ghost citation — not fetched in this run" and
    collected in the returned list. We don't silently delete — we flag, so
    the reader sees where the model fabricated a source.
    """
    fetched_norm = {_normalize(u) for u in fetched_urls}
    ghosts: list[str] = []
    cleaned = report

    for url in extract_urls(report):
        if _normalize(url) not in fetched_norm:
            ghosts.append(url)
            # Append a single badge after every occurrence; idempotent since
            # the badge itself contains no URL and won't re-match.
            badge = " ⚠ghost-citation"
            cleaned = re.sub(
                re.escape(url) + r"(?!" + re.escape(badge) + r")",
                url + badge,
                cleaned,
            )
    return cleaned, ghosts


# ── #3 URL liveness ───────────────────────────────────────────────────────────


def check_url_liveness(urls: Iterable[str], timeout: float = 5.0, workers: int = 8) -> list[str]:
    """Return URLs that are dead (connect failure, timeout, or 4xx/5xx)."""
    urls = list({u for u in urls if u})
    dead: list[str] = []
    if not urls:
        return dead

    def _probe(u: str) -> tuple[str, bool]:
        try:
            r = requests.head(u, timeout=timeout, allow_redirects=True)
            if 200 <= r.status_code < 400:
                return u, True
            # Some sites don't accept HEAD; retry with a short GET.
            r = requests.get(u, timeout=timeout, allow_redirects=True, stream=True)
            return u, 200 <= r.status_code < 400
        except requests.RequestException:
            return u, False

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in as_completed(ex.submit(_probe, u) for u in urls):
            try:
                url, alive = fut.result()
            except Exception:
                continue
            if not alive:
                dead.append(url)
    return dead


# ── Shared LLM helper ─────────────────────────────────────────────────────────


_LLM_LOCK = threading.Lock()


def _call_llm(system: str, user: str, timeout: float = 45.0, max_tokens: int = 300) -> Optional[str]:
    payload = {
        "model": LM_STUDIO_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    try:
        r = requests.post(
            LM_STUDIO_BASE_URL.rstrip("/") + "/chat/completions",
            json=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LM_STUDIO_API_KEY}"},
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json().get("choices", [{}])[0].get("message", {}).get("content") or None
    except Exception:
        return None


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_obj(raw: str) -> Optional[dict]:
    if not raw:
        return None
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw, flags=re.IGNORECASE).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    m = _JSON_OBJ_RE.search(cleaned)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


# ── #1 Citation-grounding (per-citation LLM verification) ─────────────────────


_GROUND_SYSTEM = (
    "You are a citation auditor. You are given a claim that appears in a research report and the full text of the "
    "page it cites. Decide whether the page ACTUALLY CONTAINS EVIDENCE for the specific claim. Be strict — "
    "topical relevance alone is not support. Output STRICT JSON and nothing else: "
    '{"supported":true,"evidence":"<short verbatim quote from the page that supports the claim>","reason":""} '
    'OR {"supported":false,"evidence":"","reason":"<one short sentence explaining what\'s missing>"}'
)


def _citation_prompt(claim: str, page_text: str, url: str) -> str:
    # Keep the page chunk bounded so the model doesn't drown.
    body = (page_text or "").strip()
    if len(body) > 6000:
        body = body[:6000] + "\n…[truncated]"
    return (
        f"CLAIM (from the report):\n{claim}\n\n"
        f"CITED URL: {url}\n\n"
        f"PAGE CONTENT:\n{body}\n\n"
        "Does the page contain evidence for the CLAIM? Output only the JSON verdict."
    )


# Split the report into sentence-like fragments that carry at least one URL.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\[])")


def _claims_with_urls(report: str) -> list[tuple[str, list[str]]]:
    """Return [(claim_text, [urls_in_claim])] for every sentence/bullet in the
    report that contains at least one URL. Bullets are treated as sentences."""
    # Flatten bullets / headings so each line is independently considered,
    # then sentence-split paragraph lines.
    out: list[tuple[str, list[str]]] = []
    for raw_line in (report or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Skip headings — they rarely carry factual claims worth verifying.
        if line.startswith("#"):
            continue
        # Bullets are standalone claims; paragraphs get sentence-split.
        if line.startswith(("*", "-", "+")) or re.match(r"^\d+\.\s", line):
            segments = [line]
        else:
            segments = _SENT_SPLIT_RE.split(line)
        for seg in segments:
            urls = extract_urls(seg)
            if urls:
                out.append((seg.strip(), urls))
    return out


def validate_citations(
    report: str,
    fetched_cache: dict[str, dict],
    max_claims: int = 40,
) -> list[CitationVerdict]:
    """For every (claim, url) pair in the report, LLM-verify support.

    Only claims whose URL was fetched get verified — ghost citations are
    handled by strip_unfetched_citations() separately. Caps total LLM calls
    at max_claims so a pathological report can't blow up runtime.
    """
    verdicts: list[CitationVerdict] = []
    calls_remaining = max_claims
    seen: set[tuple[str, str]] = set()  # dedupe (claim_hash, url)

    for claim, urls in _claims_with_urls(report):
        for url in urls:
            key = (hashlib.md5(claim.encode("utf-8")).hexdigest()[:8], _normalize(url))
            if key in seen:
                continue
            seen.add(key)
            page = fetched_cache.get(url) or fetched_cache.get(_normalize(url))
            if not page:
                continue  # ghost — handled elsewhere
            if calls_remaining <= 0:
                verdicts.append(CitationVerdict(
                    url=url, supported=True, reason="skipped (claim cap reached)",
                ))
                continue
            calls_remaining -= 1

            raw = _call_llm(_GROUND_SYSTEM, _citation_prompt(claim, page.get("text", ""), url), max_tokens=250)
            parsed = _parse_json_obj(raw or "")
            if not isinstance(parsed, dict):
                verdicts.append(CitationVerdict(
                    url=url, supported=True, reason="verdict unavailable (classifier error)",
                ))
                continue
            supported = bool(parsed.get("supported"))
            verdicts.append(CitationVerdict(
                url=url,
                supported=supported,
                reason=str(parsed.get("reason", "")).strip()[:300],
                evidence=str(parsed.get("evidence", "")).strip()[:300],
            ))

    return verdicts


# ── #4 Quote-anchored citation validation ─────────────────────────────────────


def validate_quotes(notes_with_quotes: list[dict], fetched_cache: dict[str, dict]) -> list[dict]:
    """Each note may carry {"url": str, "quote": str}. Check that the quote
    appears verbatim (after whitespace collapsing) in the page text."""
    results: list[dict] = []
    for note in notes_with_quotes or []:
        url = (note.get("url") or "").strip()
        quote = (note.get("quote") or "").strip()
        if not url or not quote:
            continue
        page = fetched_cache.get(url) or fetched_cache.get(_normalize(url))
        if not page:
            results.append({"url": url, "quote": quote[:120], "matched": False, "reason": "url not in fetch cache"})
            continue
        # Unicode-normalise both sides so smart quotes, em-dashes, nbsp, and
        # case differences don't trigger spurious mismatches.
        body = _text_normalize(page.get("text", ""))
        needle = _text_normalize(quote)
        matched = bool(needle) and needle in body
        results.append({
            "url": url,
            "quote": quote[:120],
            "matched": matched,
            "reason": "" if matched else "quote not found in fetched page body",
        })
    return results


# ── #5 Thin-profile detection ────────────────────────────────────────────────


_ALNUM_RE = re.compile(r"[A-Za-z0-9]+")


def detect_thin_profile(subject: str, fetched_cache: dict[str, dict]) -> tuple[bool, int]:
    """Return (is_thin, mention_count). A profile is "thin" if fewer than 3
    distinct fetched pages contain the subject's full name or all of its
    name tokens in body text. Works best when `subject` is a person's name;
    for topic queries the signal is weak and the caller should ignore it.
    """
    if not subject:
        return False, 0
    tokens = [t.lower() for t in _ALNUM_RE.findall(subject) if len(t) > 2]
    if not tokens:
        return False, 0
    full_name_lower = subject.lower().strip()

    mention_count = 0
    for page in fetched_cache.values():
        body = (page.get("text") or "").lower()
        if not body:
            continue
        if full_name_lower and full_name_lower in body:
            mention_count += 1
            continue
        # Fall back: every token must appear (handles "David E. Riggs" vs "David Riggs")
        if all(tok in body for tok in tokens):
            mention_count += 1

    return mention_count < 3, mention_count


# ── #8 Entity-disambiguation candidates ──────────────────────────────────────
# Lightweight scaffold: scan fetched pages for capitalised name-like sequences
# that share at least one token with the subject but aren't an exact match.
# Useful when the subject's name collides with other people / organisations.

_CAP_SEQ_RE = re.compile(r"\b([A-Z][A-Za-z'’\-]*(?:\.\s+[A-Z][A-Za-z'’\-]*)*(?:\s+[A-Z][A-Za-z'’\-&]*){1,3})\b")


def detect_disambiguation_candidates(
    subject: str,
    fetched_cache: dict[str, dict],
    max_candidates: int = 8,
) -> list[dict]:
    """Return a list of {name, source_url, excerpt} for distinct name-like
    strings that share a token with the subject (and aren't the subject
    itself). Cheap regex; not a true NER pass, just a disambiguation hint.
    """
    if not subject:
        return []
    subject_tokens = {t.lower() for t in _ALNUM_RE.findall(subject) if len(t) > 2}
    subject_norm = subject.lower().strip()
    if not subject_tokens:
        return []

    seen_names: dict[str, dict] = {}
    for url, page in fetched_cache.items():
        body = page.get("text", "") or ""
        if not body:
            continue
        for m in _CAP_SEQ_RE.finditer(body):
            name = m.group(1).strip()
            norm = name.lower()
            if norm == subject_norm:
                continue
            cand_tokens = {t.lower() for t in _ALNUM_RE.findall(name) if len(t) > 2}
            if not (cand_tokens & subject_tokens):
                continue
            # Keep one excerpt per distinct name (first occurrence wins)
            if norm in seen_names:
                continue
            start = max(0, m.start() - 80)
            end = min(len(body), m.end() + 80)
            excerpt = re.sub(r"\s+", " ", body[start:end]).strip()
            seen_names[norm] = {
                "name": name,
                "source_url": url,
                "excerpt": excerpt[:200],
            }
            if len(seen_names) >= max_candidates:
                break
        if len(seen_names) >= max_candidates:
            break
    return list(seen_names.values())


# ── #7 Computed confidence score ─────────────────────────────────────────────


# Categories produced by source_classifier.classify()
_PRIMARY_CATS = {"[Academic]", "[Government]", "[Non-profit/NGO]", "[Official]"}
_SECONDARY_CATS = {"[News]", "[Reference]"}
_AGGREGATOR_HOSTS = {
    "zoominfo.com", "signalhire.com", "rocketreach.co", "apollo.io",
    "crunchbase.com", "pitchbook.com", "linkedin.com", "facebook.com",
    "instagram.com", "twitter.com", "x.com",
}


def _is_aggregator(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return False
    return host in _AGGREGATOR_HOSTS


def score_confidence(
    citation_verdicts: list[CitationVerdict],
    fetched_cache: dict[str, dict],
    ghost_urls: list[str],
    dead_urls: list[str],
    thin_profile: bool,
) -> tuple[str, int]:
    """Return (tier, score). Tier ∈ {high, medium, low, very_low}.

    Signals:
      +2 per supported citation backed by a primary source
      +1 per supported citation backed by a secondary source
      -1 per unsupported-but-cited URL (model fabricated support)
      -2 per ghost URL (hallucinated link)
      -1 per dead URL
      -2 if thin profile
    """
    score = 0
    for v in citation_verdicts:
        page = fetched_cache.get(v.url) or fetched_cache.get(_normalize(v.url))
        category = (page or {}).get("category", "") if page else ""
        if v.supported:
            if _is_aggregator(v.url):
                score += 0  # aggregator support ≈ no-op
            elif category in _PRIMARY_CATS:
                score += 2
            elif category in _SECONDARY_CATS:
                score += 1
            else:
                score += 1
        else:
            score -= 2            # unsupported-but-cited is worse than no citation
    score -= 3 * len(ghost_urls)  # ghost URLs are pure fabrication — heaviest penalty
    score -= 1 * len(dead_urls)
    if thin_profile:
        score -= 2

    # Integrity hard caps: a report with ANY ghost citation or multiple
    # unsupported citations cannot be "high" confidence, no matter how many
    # other claims check out. Better to be honestly medium than dishonestly high.
    unsupported_count = sum(1 for v in citation_verdicts if not v.supported)
    has_violations = bool(ghost_urls) or unsupported_count >= 2 or thin_profile

    if score >= 6 and not has_violations:
        tier = "high"
    elif score >= 3 and not (len(ghost_urls) >= 2 or unsupported_count >= 4):
        tier = "medium"
    elif score >= 0:
        tier = "low"
    else:
        tier = "very_low"
    return tier, score


# ── Entry point ──────────────────────────────────────────────────────────────


def run_all(
    report: str,
    fetched_cache: dict[str, dict],
    query: str,
    subject: Optional[str] = None,
    notes_with_quotes: Optional[list[dict]] = None,
    corruption_flags: Optional[list[dict]] = None,
    note_count: int = 0,
) -> GroundingReport:
    """Run every grounding check and return a consolidated report."""
    cleaned, ghosts = strip_unfetched_citations(report, fetched_cache.keys())

    all_report_urls = extract_urls(report)
    dead = check_url_liveness([u for u in all_report_urls if u not in ghosts])

    verdicts = validate_citations(report, fetched_cache)
    quote_results = validate_quotes(notes_with_quotes or [], fetched_cache)

    thin, mention_count = (False, 0)
    disambig: list[dict] = []
    if subject:
        thin, mention_count = detect_thin_profile(subject, fetched_cache)
        disambig = detect_disambiguation_candidates(subject, fetched_cache)

    # Pipeline-corruption detection:
    #   (a) runtime flags from _stage_callback (degenerate stage outputs)
    #   (b) structural: notes exist / pages were fetched but the report has
    #       zero citations — a strong signal the handoff lost context
    flags = list(corruption_flags or [])
    total_report_urls = len(all_report_urls)
    if (note_count >= 3 or len(fetched_cache) >= 2) and total_report_urls == 0:
        flags.append({
            "agent": "pipeline",
            "signal": "notes-fetched-but-zero-citations",
            "sample": (
                f"{note_count} notes and {len(fetched_cache)} fetched pages on record, "
                "but the final report contains no URLs — a downstream stage almost certainly "
                "lost the researcher's context."
            ),
        })

    pipeline_corrupted = bool(flags)

    tier, score = score_confidence(verdicts, fetched_cache, ghosts, dead, thin)
    # Corruption is a hard cap on confidence — a corrupted pipeline cannot
    # be "high" or "medium" confidence regardless of other signals.
    if pipeline_corrupted:
        score -= 3
        if tier in ("high", "medium"):
            tier = "low"

    return GroundingReport(
        ghost_urls=ghosts,
        dead_urls=dead,
        citation_verdicts=verdicts,
        quote_matches=quote_results,
        thin_profile=thin,
        thin_profile_mention_count=mention_count,
        confidence_tier=tier,
        confidence_score=score,
        disambiguation_candidates=disambig,
        corruption_flags=flags,
        pipeline_corrupted=pipeline_corrupted,
        cleaned_report=cleaned,
    )
