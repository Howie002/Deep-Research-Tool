"""
claims.py — The live state model for the adaptive research loop.

Replaces the linear 4-stage pipeline's "pile of notes" with a structured
representation of *what we know, at what confidence, with what evidence*.

The adaptive worker runs a loop that:
  1. Picks the highest-priority UNKNOWN claim (or raises a new one).
  2. Proposes a search or fetch action.
  3. Runs it and hands the result to the evaluator.
  4. The evaluator updates claim statuses and may add new claims.
  5. Stops when claims are resolved OR budget is exhausted.

Every field here is designed to be serialisable (for stream events and
the end-of-run artifact) and inspectable (so the UI can render a live
claims-board).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Iterable, Optional


# Confidence at/above which a claim with supporting evidence is promoted to
# SUPPORTED. Lowered 0.75→0.60 (2026-06-05): with the evaluator's conservative
# per-evidence deltas (0.5 strong / 0.3 secondary), 0.75 required 2+ pieces of
# evidence for ANY claim, so a single authoritative primary source (e.g. an
# org's own .edu page stating its dean) stalled at PARTIAL — runs were showing
# "0 of N supported" despite solid primary-source quotes. Paired with
# source-credibility weighting of deltas (see adaptive_evaluator).
SUPPORT_CONFIDENCE = 0.60


class ClaimStatus(str, Enum):
    UNKNOWN       = "unknown"
    INVESTIGATING = "investigating"   # action dispatched, awaiting result
    SUPPORTED     = "supported"
    PARTIAL       = "partial"         # some evidence but not conclusive
    REFUTED       = "refuted"
    ABANDONED     = "abandoned"       # pursued and couldn't resolve; budget used

    @property
    def terminal(self) -> bool:
        """Terminal states don't need further investigation."""
        return self in (ClaimStatus.SUPPORTED, ClaimStatus.REFUTED, ClaimStatus.ABANDONED)


@dataclass
class Evidence:
    url: str
    quote: str
    supports: bool                       # True supports the claim, False refutes
    accessed_at: float = field(default_factory=time.time)
    category: str = ""                   # source_classifier tier, e.g. "[Academic]"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Claim:
    id: str
    text: str                            # natural-language statement of the claim
    status: ClaimStatus = ClaimStatus.UNKNOWN
    confidence: float = 0.0              # 0.0 (unknown) to 1.0 (verified)
    priority: float = 0.5                # 0.0..1.0, higher = investigate sooner
    support: list[Evidence] = field(default_factory=list)
    contradictions: list[Evidence] = field(default_factory=list)
    attempts: int = 0                    # how many actions have targeted this claim
    parent_id: Optional[str] = None      # child claims raised by evidence
    created_at: float = field(default_factory=time.time)
    last_update_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "status": self.status.value,
            "support": [e.to_dict() for e in self.support],
            "contradictions": [e.to_dict() for e in self.contradictions],
        }

    def abandon(self, reason: str = "") -> None:
        """Mark this claim as ABANDONED. Used by the strategist when a claim
        is hopeless within remaining budget. Reason is stored on the action
        log via the worker, not on the claim itself."""
        if self.status.terminal:
            return     # don't overwrite SUPPORTED/REFUTED
        self.status = ClaimStatus.ABANDONED
        self.last_update_at = time.time()

    def add_evidence(self, ev: Evidence, confidence_delta: float) -> None:
        if ev.supports:
            self.support.append(ev)
        else:
            self.contradictions.append(ev)
        self.confidence = max(0.0, min(1.0, self.confidence + confidence_delta))
        self.last_update_at = time.time()
        # Status transitions:
        #   REFUTED is sticky — once a direct contradiction lands without
        #   offsetting support, we don't auto-undo it on later evidence.
        #   Otherwise: high confidence + support → SUPPORTED; a
        #   contradiction with no countering support → REFUTED; any
        #   mixed/weak evidence → PARTIAL.
        if self.status == ClaimStatus.REFUTED:
            return
        if self.contradictions and not self.support:
            self.status = ClaimStatus.REFUTED
        elif self.confidence >= SUPPORT_CONFIDENCE and self.support:
            self.status = ClaimStatus.SUPPORTED
        elif self.support or self.contradictions:
            self.status = ClaimStatus.PARTIAL


# ── Budget ────────────────────────────────────────────────────────────────────


@dataclass
class Budget:
    """Hard budget the loop runs within. Exhausting ANY dimension stops the loop."""
    max_fetches: int = 10
    max_searches: int = 10
    max_llm_calls: int = 80
    max_wallclock_seconds: float = 900.0
    max_loop_iterations: int = 40         # safety cap — should never fire first

    fetches_used: int = 0
    searches_used: int = 0
    llm_calls_used: int = 0
    loops_used: int = 0
    started_at: float = field(default_factory=time.time)

    def remaining_wallclock(self) -> float:
        return max(0.0, self.max_wallclock_seconds - (time.time() - self.started_at))

    def exhausted(self) -> Optional[str]:
        """Return a human-readable reason if any dimension is exhausted."""
        if self.fetches_used >= self.max_fetches:
            return f"fetch cap ({self.max_fetches})"
        if self.searches_used >= self.max_searches:
            return f"search cap ({self.max_searches})"
        if self.llm_calls_used >= self.max_llm_calls:
            return f"LLM call cap ({self.max_llm_calls})"
        if self.remaining_wallclock() <= 0:
            return f"wallclock cap ({self.max_wallclock_seconds:.0f}s)"
        if self.loops_used >= self.max_loop_iterations:
            return f"loop cap ({self.max_loop_iterations})"
        return None

    def snapshot(self) -> dict:
        return {
            "fetches_used": self.fetches_used,
            "max_fetches": self.max_fetches,
            "searches_used": self.searches_used,
            "max_searches": self.max_searches,
            "llm_calls_used": self.llm_calls_used,
            "max_llm_calls": self.max_llm_calls,
            "loops_used": self.loops_used,
            "max_loop_iterations": self.max_loop_iterations,
            "elapsed_seconds": time.time() - self.started_at,
            "max_wallclock_seconds": self.max_wallclock_seconds,
        }


# ── ClaimsModel ──────────────────────────────────────────────────────────────


@dataclass
class ClaimsModel:
    """The full live state of an adaptive run."""
    query: str
    claims: dict[str, Claim] = field(default_factory=dict)
    budget: Budget = field(default_factory=Budget)
    # Running history of actions (for audit + stagnation detection)
    action_log: list[dict] = field(default_factory=list)
    # Set of URLs already fetched (mirrored from tools layer for quick checks)
    fetched_urls: set[str] = field(default_factory=set)

    # ── Claim management ────────────────────────────────────────────────

    def add_claim(self, text: str, priority: float = 0.5, parent_id: Optional[str] = None) -> Claim:
        claim = Claim(
            id=uuid.uuid4().hex[:8],
            text=text.strip(),
            priority=max(0.0, min(1.0, priority)),
            parent_id=parent_id,
        )
        self.claims[claim.id] = claim
        return claim

    def unresolved(self) -> list[Claim]:
        """Claims that might still benefit from investigation."""
        return [c for c in self.claims.values() if not c.status.terminal]

    def resolved(self) -> list[Claim]:
        return [c for c in self.claims.values() if c.status.terminal]

    def highest_priority_open(self) -> Optional[Claim]:
        """The claim the planner should probably work on next.

        Ranks by (priority desc, attempts asc, last_update_at asc) so we
        try high-value claims first and, among equals, ones we've tried
        less / haven't touched recently.
        """
        candidates = [c for c in self.unresolved()
                      if c.status != ClaimStatus.INVESTIGATING]
        if not candidates:
            return None
        candidates.sort(key=lambda c: (-c.priority, c.attempts, c.last_update_at))
        return candidates[0]

    def is_satisfied(self, min_confidence: float = 0.6, min_supported_fraction: float = 0.7) -> bool:
        """Does the current state look like a complete-enough answer?"""
        if not self.claims:
            return False
        total = len(self.claims)
        well_supported = sum(
            1 for c in self.claims.values()
            if c.status == ClaimStatus.SUPPORTED and c.confidence >= min_confidence
        )
        return (well_supported / total) >= min_supported_fraction

    # ── Action logging ──────────────────────────────────────────────────

    def log_action(self, action: dict, result_summary: str = "") -> None:
        self.action_log.append({
            "ts":   time.time(),
            "loop": self.budget.loops_used,
            "action": action,
            "result": result_summary[:300],
        })

    def recent_actions(self, n: int = 5) -> list[dict]:
        return self.action_log[-n:]

    def is_stagnating(self, lookback: int = 4) -> bool:
        """Heuristic: no new supported/refuted claims in the last `lookback` loops."""
        if len(self.action_log) < lookback:
            return False
        recent = self.action_log[-lookback:]
        recent_loops = {a["loop"] for a in recent}
        if len(recent_loops) < lookback:
            return False
        # If no claim's last_update fell in these loops, we're not progressing.
        earliest_ts = min(a["ts"] for a in recent)
        any_progressed = any(
            c.last_update_at >= earliest_ts and c.status.terminal
            for c in self.claims.values()
        )
        return not any_progressed

    # ── Serialisation ───────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "query":   self.query,
            "claims":  [c.to_dict() for c in self.claims.values()],
            "budget":  self.budget.snapshot(),
            "action_log": self.action_log[-200:],
            "fetched_urls": sorted(self.fetched_urls),
        }

    def summary_line(self) -> str:
        total = len(self.claims)
        by_status: dict[str, int] = {}
        for c in self.claims.values():
            by_status[c.status.value] = by_status.get(c.status.value, 0) + 1
        parts = [f"{k}={v}" for k, v in sorted(by_status.items())]
        return (
            f"Claims: {total}  ({', '.join(parts)})  "
            f"loops={self.budget.loops_used}  "
            f"fetches={self.budget.fetches_used}/{self.budget.max_fetches}  "
            f"elapsed={time.time() - self.budget.started_at:.0f}s"
        )


# ── Budget presets ───────────────────────────────────────────────────────────


def preset_budget(depth: str) -> Budget:
    """Map the shipped depth presets onto loop-based budgets.

    Budgets emphasise hard caps on *resources* (fetches / wallclock), not
    on process (stage iterations). The loop runs as long as productive
    moves are available within budget.

    These were bumped 2026-04-27 after Andrew Howerton runs were
    consistently exhausting fetch budget at depth=medium with thin profile
    subjects. The new mediums (~50% larger) leave room for the strategist
    to investigate newly-raised sub-claims after the initial pass closes.
    """
    # Searches are capped BELOW fetches: one search surfaces many fetchable URLs,
    # so a balanced run should fetch far more than it searches. Equal caps let runs
    # exhaust the search budget while starving fetches (observed 30 searches / 7
    # fetches at heavy). search ≈ fetch/2 forces the loop to spend its budget on
    # evidence-gathering, not query churn.
    depth = (depth or "medium").lower()
    if depth == "light":
        return Budget(max_fetches=10, max_searches=5,  max_llm_calls=60,  max_wallclock_seconds=600,  max_loop_iterations=25)
    if depth == "heavy":
        return Budget(max_fetches=32, max_searches=15, max_llm_calls=240, max_wallclock_seconds=2700, max_loop_iterations=90)
    if depth == "ultra":
        return Budget(max_fetches=60, max_searches=28, max_llm_calls=480, max_wallclock_seconds=5400, max_loop_iterations=180)
    # Default = medium
    return Budget(max_fetches=18, max_searches=9,  max_llm_calls=120, max_wallclock_seconds=1500, max_loop_iterations=60)
