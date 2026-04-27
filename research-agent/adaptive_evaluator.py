"""
adaptive_evaluator.py — Integrate action results back into the claims model.

After the loop executes an action (search / fetch), this module:
  1. Reads the result text against every unresolved claim.
  2. For each claim, decides: does this evidence support, refute, or not
     touch it? Extracts a quote when possible.
  3. Raises new claims when the evidence surfaces facts worth investigating
     (e.g. "subject's LinkedIn shows JD → open new claim: verify bar admission").

The evaluator updates the ClaimsModel in place and returns an update
summary for stream events / logging.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

from claims import ClaimsModel, Evidence, ClaimStatus

LLMFn = Callable[[str, str], Optional[str]]


_EVAL_SYSTEM = (
    "You are a research evaluator. You are given:\n"
    "  (a) the research SUBJECT (the person, organisation, or topic being investigated),\n"
    "  (b) a list of open claims about that subject, and\n"
    "  (c) new evidence that just came in (a search result list or fetched page content).\n\n"
    "For each claim, decide whether the evidence supports, refutes, or doesn't touch it. "
    "If it does, extract a short verbatim quote (max 200 chars) from the evidence that "
    "establishes this. Also raise NEW claims when the evidence reveals facts worth "
    "verifying that aren't in the current claims list — but only if those new claims are "
    "ABOUT THE SUBJECT.\n\n"
    "Output STRICT JSON and nothing else:\n"
    "{\n"
    '  "updates": [\n'
    '    {"claim_id": "<id>", "supports": true|false, "quote": "<short verbatim>",\n'
    '     "source_url": "<the EXACT URL of the source the quote came from>",\n'
    '     "confidence_delta": 0.05-0.5,\n'
    '     "tension": true|false}\n'
    "  ],\n"
    '  "new_claims": [\n'
    '    {"text": "<short verifiable claim>", "priority": 0.0-1.0, "parent_claim_id": "<id or null>"}\n'
    "  ]\n"
    "}\n\n"
    "Rules:\n"
    " - Be STRICT. 'Evidence' means the page actually contains the claim — topical "
    "adjacency is not support.\n"
    " - **`source_url` is REQUIRED on every update.** When the evidence is a search-result "
    "list, each result has its OWN `URL: <link>` line — copy the URL of the SPECIFIC "
    "result the quote came from. When the evidence is a fetched page, the page's URL is "
    "given as 'SOURCE URL: …' in the input — use that.\n"
    " - **Quote rules:** at least 10 characters, must appear verbatim (or near-verbatim) "
    "in the evidence text. NEVER output 'None', 'null', 'N/A' or any other placeholder "
    "string as a quote — if no real quote exists, omit the update entirely.\n"
    " - **Subject focus on new_claims:** new_claims must be ABOUT the research subject. "
    "If the evidence mentions other people, places, organisations, or events that "
    "co-occur with the subject (e.g. an article about the subject names colleagues, or a "
    "company page lists other employees), DO NOT raise those as new claims. They are "
    "tangential. Only raise new claims that describe an attribute of THE SUBJECT THEMSELVES "
    "(employer, credential, education, role, affiliation, project).\n"
    " - **Refutation vs. tension.** Set `supports: false` ONLY when the evidence "
    "directly negates a claim — e.g. the source explicitly says 'X is NOT employed at Y' "
    "or names a different person as the holder of the claimed role. If two sources merely "
    "give DIFFERENT facts that could both be true at different times (e.g. 'platoon leader, "
    "A Company' in 2014 and 'platoon leader, B Company' in 2016), set `supports: true` "
    "with `tension: true` — this records that sources disagree without claiming the prior "
    "evidence was wrong.\n"
    " - confidence_delta: 0.5 for strong primary-source confirmation, 0.3 for solid "
    "secondary, 0.15 for mere mention, NEGATIVE only if evidence genuinely refutes.\n"
    " - If nothing in the evidence addresses any claim, return empty arrays."
)


# Quotes that callers (or the LLM) sometimes emit as placeholders. Treated
# as 'no real evidence' and skipped.
_PLACEHOLDER_QUOTES = {"none", "null", "n/a", "na", "(none)", "no quote", "no evidence", ""}


def _is_real_quote(quote: str) -> bool:
    if not quote:
        return False
    cleaned = quote.strip().strip("'\"`").strip()
    if cleaned.lower() in _PLACEHOLDER_QUOTES:
        return False
    if len(cleaned) < 10:
        return False
    return True


def _claims_digest(cm: ClaimsModel) -> str:
    lines: list[str] = []
    for c in sorted(cm.claims.values(), key=lambda c: (-c.priority, c.id)):
        if c.status.terminal:
            continue
        lines.append(
            f"- id={c.id} status={c.status.value} conf={c.confidence:.2f}  {c.text}"
        )
    return "\n".join(lines) if lines else "(no open claims)"


def _eval_user_prompt(
    cm: ClaimsModel,
    evidence_kind: str,
    evidence_text: str,
    evidence_url: str = "",
    subject: str = "",
) -> str:
    # Cap evidence body so the classifier model isn't drowned.
    body = evidence_text.strip()
    if len(body) > 6000:
        body = body[:6000] + "\n…[truncated]"
    source_line = f"SOURCE URL: {evidence_url}\n" if evidence_url else ""
    subject_line = f"RESEARCH SUBJECT: {subject}\n\n" if subject else ""
    return (
        f"{subject_line}"
        f"OPEN CLAIMS:\n{_claims_digest(cm)}\n\n"
        f"{evidence_kind.upper()} RESULT:\n{source_line}"
        f"{body}\n\n"
        "Output only the JSON verdict. Reminder: new_claims must be ABOUT the research "
        "subject named above — tangential mentions of other people, places, or events "
        "in this evidence are NOT new claims worth investigating."
    )


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse(raw: str) -> Optional[dict]:
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


def integrate_result(
    cm: ClaimsModel,
    evidence_kind: str,         # "search" | "fetch"
    evidence_text: str,
    evidence_url: str,
    evidence_category: str,
    llm: LLMFn,
    subject: str = "",          # query/subject string used for new-claim focus
) -> dict:
    """Update the claims model from a fresh action result. Returns a
    compact summary dict describing changes made."""
    summary: dict = {
        "updated_claims": [],
        "new_claim_ids":  [],
        "parse_failed":   False,
    }

    raw = llm(_EVAL_SYSTEM, _eval_user_prompt(cm, evidence_kind, evidence_text, evidence_url, subject))
    parsed = _parse(raw or "")
    if not isinstance(parsed, dict):
        summary["parse_failed"] = True
        return summary

    # Apply updates to existing claims
    for upd in parsed.get("updates", []) or []:
        if not isinstance(upd, dict):
            continue
        claim_id = str(upd.get("claim_id", "")).strip()
        if not claim_id or claim_id not in cm.claims:
            continue
        claim = cm.claims[claim_id]
        supports = bool(upd.get("supports"))
        quote = str(upd.get("quote", "")).strip()
        # Reject placeholder / "None" / too-short quotes — without this filter
        # an LLM that emits "quote": "None" can flip a claim to REFUTED on
        # what is effectively no evidence at all.
        if not _is_real_quote(quote):
            continue
        # Per-update source URL is required when the action was a search
        # (each result has its own URL). For a fetch the page-level URL is
        # the natural fallback. If a search-derived update arrives without
        # a URL, drop it — citations without sources defeat the architecture.
        per_url = str(upd.get("source_url", "")).strip()
        if per_url and per_url.startswith(("http://", "https://")):
            ev_url = per_url
        elif evidence_url:
            ev_url = evidence_url
        elif evidence_kind == "fetch":
            ev_url = evidence_url       # may be empty; rare
        else:
            # Search-derived update with no per-update URL → unusable.
            continue

        try:
            delta = float(upd.get("confidence_delta", 0.0))
        except (TypeError, ValueError):
            delta = 0.0
        # Tension flag: source-level disagreement that may not be real refutation.
        # When tension=true, store as supporting evidence (sources differ but the
        # claim may still hold) but apply a small confidence penalty rather than a
        # large negative delta.
        tension = bool(upd.get("tension")) and not supports
        if tension:
            # Promote to supports=True with reduced delta — preserves the quote
            # as evidence the claim is contested rather than wrong.
            supports = True
            delta = max(0.0, abs(delta) * 0.3)
        elif not supports:
            # Genuine refutation — flip the sign.
            delta = -abs(delta)

        claim.add_evidence(
            Evidence(
                url=ev_url,
                quote=quote[:300],
                supports=supports,
                category=evidence_category,
            ),
            confidence_delta=delta,
        )
        summary["updated_claims"].append({
            "id":     claim.id,
            "status": claim.status.value,
            "confidence": claim.confidence,
            "support_count":       len(claim.support),
            "contradiction_count": len(claim.contradictions),
            "tension":             tension,
        })

    # Raise new claims when the evidence surfaced facts
    for nc in parsed.get("new_claims", []) or []:
        if not isinstance(nc, dict):
            continue
        text = str(nc.get("text", "")).strip()
        if not text:
            continue
        try:
            priority = float(nc.get("priority", 0.5))
        except (TypeError, ValueError):
            priority = 0.5
        # Dedupe: skip if claim text already exists (case-insensitive, whitespace-collapsed)
        norm = re.sub(r"\s+", " ", text.lower())
        if any(re.sub(r"\s+", " ", c.text.lower()) == norm for c in cm.claims.values()):
            continue
        parent = str(nc.get("parent_claim_id") or "").strip() or None
        new = cm.add_claim(text=text, priority=priority, parent_id=parent)
        summary["new_claim_ids"].append(new.id)

    return summary
