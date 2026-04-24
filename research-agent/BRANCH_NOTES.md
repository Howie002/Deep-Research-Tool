# dev2 — Adaptive research loop

This branch pivots the research engine from a linear 4-stage pipeline
(Researcher → Analyst → Gap Analyst → Synthesizer) to a **budget-driven
adaptive loop** that reasons about its own completeness.

The goal is to stop hard-coding "what good research looks like" (minimum
fetch counts, required LinkedIn probes, stage iteration caps) and let the
agent decide dynamically which thread to pull next, based on what it
still doesn't know.

## The two modes side by side

| | **Pipeline** (main) | **Adaptive** (this branch) |
|---|---|---|
| Shape | Fixed 4 stages, each with a prompt + agent + tools | One loop: decompose → plan → act → evaluate → repeat |
| State | Pile of notes + raw task output strings | Structured claims model with confidence, support, contradictions |
| Stopping | Fixed stage count per preset | Budget exhausted OR claims sufficiently supported OR stalled |
| Citations | Synthesizer writes prose with URLs | Every citation is a fetched URL with a stored quote |
| Failure mode if evidence is thin | Fabricated prose around real URLs (David Riggs) | Open claims stay open; report says "unresolved" honestly |
| Retry behavior | Per-agent `max_iter` burns through attempts | Planner picks the next *different* angle based on state |

## Running it

```bash
# Adaptive loop, printed to stdout + saved to reports/
python run.py adaptive "Research Stephen B Guetersloh former professor at Texas A&M" \
    --depth medium

# Depth → budget, not stage count:
#   light:  5 fetches,  5 searches,  40 LLM calls,  300s wall-clock, 15 loops
#   medium: 10 fetches, 10 searches, 80 LLM calls,  900s wall-clock, 40 loops
#   heavy:  20 fetches, 20 searches, 160 LLM calls, 1800s wall-clock, 60 loops
#   ultra:  40 fetches, 40 searches, 320 LLM calls, 3600s wall-clock, 120 loops
```

The linear pipeline still works exactly as before — `python run.py query "…"`.
API mode (`run.py api`) currently routes to the linear pipeline only;
wiring adaptive into the API + UI is a separate task.

## Module layout

| File | Role |
|---|---|
| `claims.py` | `Claim`, `Evidence`, `ClaimsModel`, `Budget`, status lifecycle, satisfaction/stagnation heuristics |
| `adaptive_planner.py` | Two LLM prompts: decompose a query into verifiable claims; pick the single next-best action given state |
| `adaptive_evaluator.py` | Read action results, update claim statuses with confidence deltas, raise new claims when evidence surfaces new threads |
| `adaptive_worker.py` | The loop itself. Wires observability (stream events, fetch persister), deterministic synthesizer that renders straight from the claims model |
| `run.py` | Adds `adaptive` subcommand |

## Why no API/UI integration yet

The loop needs to prove itself on real queries before we invest in UI.
Recommended next step is to compare outputs on the three test cases that
broke the pipeline: David Riggs (thin profile), Jennifer Ann Scasta
(stage-handoff corruption), Stephen Guetersloh (ghost citations). If the
adaptive loop is clearly better on ≥2 of 3, we port it to the API +
build a live claims-board UI. If not, we've learned something important
and we keep the pipeline.

## Known limitations on dev2 (today)

1. **Depends on LM Studio** — same as pipeline mode. The LLM must be
   reachable at `LM_STUDIO_BASE_URL`. If the planner or evaluator fail to
   get a response, the loop falls back gracefully (searches the
   highest-priority claim, stops on repeated failure), but it can't do
   interesting work without the model.

2. **No grounding-pass integration yet.** The adaptive loop persists a
   `claims.json` artifact, but doesn't yet run `grounding.run_all()` on
   the synthesized report. Every URL in the report is already from the
   fetched set (by construction), so ghost-citation detection is
   redundant — but the LLM citation-grounding check could still find
   cases where a quote was misattributed. Easy add-on.

3. **Synthesizer is deterministic** — renders straight from the claims
   model. Output is structured and honest but less prose-elegant than
   the pipeline's LLM-written report. If we want polished prose,
   insert an LLM-polish pass *inside each claim block*, keeping the
   overall structure and citations mechanical.

4. **No live UI yet.** The CLI prints progress. When we wire the API in,
   the emit calls (`claims_snapshot`, `claims_update`) are already there
   — a frontend can subscribe and render a live claims board.

## Comparing this branch to main

```bash
git diff main..dev2 -- research-agent/    # all changes on this branch
git log --oneline main..dev2              # commits unique to dev2
```

The linear pipeline and grounding pass from main are preserved intact.
Nothing from main was deleted — this is purely additive.
