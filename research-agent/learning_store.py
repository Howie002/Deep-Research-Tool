"""
learning_store.py — Persistent, cross-run research memory.

After each completed run, run_reflection() asks the local LLM to read the
query / plan / notes / gaps and extract 3–7 PROCESS-level lessons, each
phrased as "<lesson> — Why it matters: <one-sentence justification>".
Insights are appended to learning_store.json and retrieved on future runs
via keyword overlap with the new query.

Public API (called by api_server.py and research_worker.py):
    run_reflection(query, plan, notes, gaps, meta, store_path, source_run) -> dict | None
    get_relevant_insights(query, store_path, limit=5) -> list[dict]
    get_all_insights(store_path) -> list[dict]                # newest first
    delete_insight(insight_id, store_path) -> bool
    update_insight(insight_id, changes, store_path) -> bool
"""
from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import LM_STUDIO_API_KEY, LM_STUDIO_BASE_URL, LM_STUDIO_MODEL

_LOCK = threading.Lock()

_MUTABLE_FIELDS = {"lessons", "tags", "topic_domain", "keywords"}

_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "then", "of", "to", "in", "on",
    "for", "from", "with", "at", "by", "about", "as", "is", "are", "was", "were",
    "be", "been", "being", "it", "its", "this", "that", "these", "those", "what",
    "which", "who", "whom", "why", "how", "do", "does", "did", "have", "has",
    "had", "you", "he", "she", "we", "they", "me", "us", "them", "my", "your",
    "his", "her", "their", "our", "can", "could", "should", "would", "will",
    "just", "also", "not", "than",
}


# ── Internal helpers ──────────────────────────────────────────────────────────


def _tokenize(text: str) -> set[str]:
    return {
        tok for tok in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


def _load(store_path: Path) -> list[dict]:
    p = Path(store_path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(store_path: Path, insights: list[dict]) -> None:
    p = Path(store_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replace so a crash mid-write can't corrupt the store.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(insights, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _truncate(text: str, max_chars: int) -> str:
    if text and len(text) > max_chars:
        return text[:max_chars] + "\n…[truncated]"
    return text or ""


# ── Retrieval ─────────────────────────────────────────────────────────────────


def get_all_insights(store_path) -> list[dict]:
    with _LOCK:
        insights = _load(Path(store_path))
    insights.sort(key=lambda i: i.get("created_at", ""), reverse=True)
    return insights


def get_relevant_insights(query: str, store_path, limit: int = 5) -> list[dict]:
    """Top-N insights ranked by keyword overlap with `query`.

    Score = |q_tokens ∩ stored_terms| + small recency tiebreak (30-day half-life).
    Zero-overlap insights are dropped.
    """
    with _LOCK:
        insights = _load(Path(store_path))
    if not insights:
        return []

    q_tokens = _tokenize(query)
    if not q_tokens:
        return []

    now = datetime.now(timezone.utc)
    scored: list[tuple[float, dict]] = []
    for ins in insights:
        stored_terms: set[str] = set()
        for k in ins.get("keywords", []):
            if isinstance(k, str):
                stored_terms.add(k.lower())
        stored_terms |= _tokenize(ins.get("query", ""))
        stored_terms |= _tokenize(ins.get("topic_domain", ""))
        stored_terms |= _tokenize(" ".join(ins.get("tags", []) if isinstance(ins.get("tags"), list) else []))

        overlap = len(q_tokens & stored_terms)
        if overlap == 0:
            continue

        recency = 0.0
        created = ins.get("created_at", "")
        try:
            ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
            age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
            recency = 1.0 / (1.0 + age_days / 30.0)
        except (ValueError, TypeError):
            pass

        scored.append((overlap + 0.1 * recency, ins))

    scored.sort(key=lambda t: t[0], reverse=True)
    return [ins for _, ins in scored[:limit]]


# ── Mutations ─────────────────────────────────────────────────────────────────


def delete_insight(insight_id: str, store_path) -> bool:
    with _LOCK:
        insights = _load(Path(store_path))
        remaining = [i for i in insights if i.get("id") != insight_id]
        if len(remaining) == len(insights):
            return False
        _save(Path(store_path), remaining)
    return True


def update_insight(insight_id: str, changes: dict, store_path) -> bool:
    sanitized = {k: v for k, v in (changes or {}).items() if k in _MUTABLE_FIELDS}
    if not sanitized:
        return False
    with _LOCK:
        insights = _load(Path(store_path))
        for ins in insights:
            if ins.get("id") == insight_id:
                ins.update(sanitized)
                _save(Path(store_path), insights)
                return True
    return False


# ── Reflection (LLM pass after a run) ─────────────────────────────────────────


_REFLECTION_SYSTEM = """You are a research-process reviewer. After each research run you read the query, the plan, the collected notes, and any gap analysis, and extract SHORT, PROCESS-LEVEL lessons that would help the agent do a BETTER JOB on future runs about similar topics.

Focus on the *how*, not the subject matter:
  - Search strategies that worked (or didn't)
  - Source types that were reliable (or not)
  - Query formulations that returned good results
  - Pitfalls, dead ends, or wasted branches
  - Framings that made notes easier to synthesise later

Every lesson MUST be phrased as:
  "<the lesson> — Why it matters: <one-sentence justification tied to this run or future runs>"

Return ONLY valid JSON. No prose outside the JSON, no code fences."""


_REFLECTION_USER_TEMPLATE = """QUERY:
{query}

PLAN:
{plan}

NOTES:
{notes}

GAPS (unresolved questions at end of run):
{gaps}

Produce 3–7 lessons. Respond with JSON of exactly this shape:

{{
  "topic_domain": "<2–4 word label, e.g. 'academic philanthropy', 'fusion energy', 'corporate finance'>",
  "keywords": ["<3–8 lowercase topical keywords used to retrieve this insight on similar future queries>"],
  "lessons": [
    "<lesson 1> — Why it matters: <one sentence>",
    "<lesson 2> — Why it matters: <one sentence>"
  ]
}}"""


def _call_llm(system: str, user: str, timeout: float = 120.0) -> Optional[str]:
    url = LM_STUDIO_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": LM_STUDIO_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 1500,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {LM_STUDIO_API_KEY}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body.get("choices", [{}])[0].get("message", {}).get("content") or None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, IndexError):
        return None


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_reflection(raw: str) -> Optional[dict]:
    if not raw:
        return None
    stripped = re.sub(r"```(?:json)?\s*|\s*```", "", raw, flags=re.IGNORECASE).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    m = _JSON_OBJ_RE.search(stripped)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def run_reflection(
    query: str,
    plan: str = "",
    notes: str = "",
    gaps: str = "",
    meta: Optional[dict] = None,           # accepted for API compatibility; unused
    store_path: Optional[Path] = None,
    source_run: str = "",
) -> Optional[dict]:
    """Reflect on a completed run and append a new insight.

    Returns the saved insight on success, or None on any failure (LLM
    unavailable, empty response, parse error, no store_path). Callers wrap
    this in try/except — never raises.
    """
    if store_path is None:
        return None

    user = _REFLECTION_USER_TEMPLATE.format(
        query=query or "(none)",
        plan=_truncate(plan, 6000),
        notes=_truncate(notes, 12000),
        gaps=_truncate(gaps, 3000),
    )
    raw = _call_llm(_REFLECTION_SYSTEM, user)
    parsed = _parse_reflection(raw or "")
    if not isinstance(parsed, dict):
        return None

    lessons = [str(x).strip() for x in parsed.get("lessons", []) if str(x).strip()]
    if not lessons:
        return None

    topic_domain = str(parsed.get("topic_domain", "")).strip()
    keywords = [
        str(k).strip().lower()
        for k in parsed.get("keywords", [])
        if isinstance(k, (str, int, float)) and str(k).strip()
    ]

    insight = {
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_run": source_run or "",
        "query": query or "",
        "topic_domain": topic_domain,
        "keywords": keywords,
        "lessons": lessons,
        "tags": [],
    }

    with _LOCK:
        insights = _load(Path(store_path))
        insights.append(insight)
        _save(Path(store_path), insights)
    return insight
