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
    "  (a) a list of open claims in an ongoing investigation, and\n"
    "  (b) new evidence that just came in (a search result list or fetched page content).\n\n"
    "For each claim, decide whether the evidence supports, refutes, or doesn't touch it. "
    "If it does, extract a short verbatim quote (max 200 chars) from the evidence that "
    "establishes this. Also raise NEW claims when the evidence reveals facts worth "
    "verifying that aren't in the current claims list (e.g. a new credential, employer, "
    "or affiliation not yet tracked).\n\n"
    "Output STRICT JSON and nothing else:\n"
    "{\n"
    '  "updates": [\n'
    '    {"claim_id": "<id>", "supports": true|false, "quote": "<short verbatim>", "confidence_delta": 0.05-0.5}\n'
    "  ],\n"
    '  "new_claims": [\n'
    '    {"text": "<short verifiable claim>", "priority": 0.0-1.0, "parent_claim_id": "<id or null>"}\n'
    "  ]\n"
    "}\n\n"
    "Rules:\n"
    " - Be STRICT. 'Evidence' means the page actually contains the claim — topical "
    "adjacency is not support.\n"
    " - confidence_delta: 0.5 for strong primary-source confirmation, 0.3 for solid "
    "secondary, 0.15 for mere mention, NEGATIVE only if evidence refutes.\n"
    " - Only raise new claims that are concrete and verifiable — skip vague assertions.\n"
    " - If nothing in the evidence addresses any claim, return empty arrays."
)


def _claims_digest(cm: ClaimsModel) -> str:
    lines: list[str] = []
    for c in sorted(cm.claims.values(), key=lambda c: (-c.priority, c.id)):
        if c.status.terminal:
            continue
        lines.append(
            f"- id={c.id} status={c.status.value} conf={c.confidence:.2f}  {c.text}"
        )
    return "\n".join(lines) if lines else "(no open claims)"


def _eval_user_prompt(cm: ClaimsModel, evidence_kind: str, evidence_text: str, evidence_url: str = "") -> str:
    # Cap evidence body so the classifier model isn't drowned.
    body = evidence_text.strip()
    if len(body) > 6000:
        body = body[:6000] + "\n…[truncated]"
    source_line = f"SOURCE URL: {evidence_url}\n" if evidence_url else ""
    return (
        f"OPEN CLAIMS:\n{_claims_digest(cm)}\n\n"
        f"{evidence_kind.upper()} RESULT:\n{source_line}"
        f"{body}\n\n"
        "Output only the JSON verdict."
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
) -> dict:
    """Update the claims model from a fresh action result. Returns a
    compact summary dict describing changes made."""
    summary: dict = {
        "updated_claims": [],
        "new_claim_ids":  [],
        "parse_failed":   False,
    }

    raw = llm(_EVAL_SYSTEM, _eval_user_prompt(cm, evidence_kind, evidence_text, evidence_url))
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
        if not quote:
            continue
        try:
            delta = float(upd.get("confidence_delta", 0.0))
        except (TypeError, ValueError):
            delta = 0.0
        # Flip sign: refuting evidence decreases confidence toward 0 (and below).
        if not supports:
            delta = -abs(delta)

        claim.add_evidence(
            Evidence(
                url=evidence_url,
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
