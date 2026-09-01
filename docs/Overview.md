# Deep Research Tool

## Repository

- **GitHub:** [Howie002/Deep-Research-Tool](https://github.com/Howie002/Deep-Research-Tool) (private)
- **GitLab:** [tamfassoc_gitlab/ai/Deep-Research-Tool](https://gitlab.com/tamfassoc_gitlab/ai/Deep-Research-Tool) (private) - *push-mirrored 2026-09-01: this clone's `gitlab` remote is live and verified (local HEAD matches GitLab HEAD on the current branch). Andrew's dual-remote directive is resolved fleet-wide as of today.*
**Local path:** `~/Documents/Foundation AI Projects/Deep-Research-Agent/` *(corrected 2026-08-25 — the old `VS Code Projects/Deep Research Tool/` path predates the move to the standard repo root and no longer exists)*
**Branch model:** `main` (stable linear pipeline), `dev2` (active - adaptive claims-model loop)

> ⚠️ **Name mismatch, deliberate but easy to trip on:** the GitHub repo is **`Deep-Research-Tool`**
> while the local clone directory is **`Deep-Research-Agent`**. Both are correct; `git remote -v`
> resolves it. Nothing depends on the directory name.

### Superseded precursor — `research-agent`

[github.com/Howie002/research-agent](https://github.com/Howie002/research-agent) (private) is the
**original CrewAI + LM Studio prototype** this tool grew out of. It is **archived, not maintained,
and deliberately not cloned to aivm** — classified as such in the 2026-08-25 fleet audit. A copy is
already vendored inside this repo at `research-agent/`, carrying explicit *STALE SNAPSHOT
(pre-2026-05)* banners that point back to the current `docs/Roadmap.md`. Do not treat it as a second
live implementation.

## Purpose

Fully local, multi-agent research tool that performs deep web research on any topic - automatically decomposing a question into verifiable claims, searching the web iteratively, evaluating evidence, and producing a structured, cited research report with confidence annotations. An in-house, air-gapped alternative to Perplexity Deep Research or ChatGPT Deep Research.

## Status

**Current Phase:** LIVE IN PRODUCTION — adaptive loop is the shipped engine, on the fleet at `/DeepResearch` since 06-03; Death Star nvfp4 gemma via LiteLLM `:4000` since 07-02; browser + Heavy-depth verified 07-07. Branch `dev2`.
**Last Updated:** 2026-07-17 *(fleet docs audit)*

## Architecture Evolution

### v1 - Linear Pipeline (main branch, shipped 2026-03-24 → 2026-04-17)
Fixed 4-stage pipeline: Research Specialist → Critical Analyst → Gap Analyst → Report Synthesizer. Each stage runs once with hard-coded iteration caps. Works but fragile - prone to fabricated prose around real URLs when evidence is thin, stage-handoff corruption, and ghost citations.

**Main branch commits (5 total):**
- Initial commit with CrewAI 3-agent pipeline (Apr 14)
- Dynamic clarifying questions, UYBJ button, Live Plan Evolution, Research Notebook (Apr 15)
- 4-stage pipeline with Gap Analyst, ThoughtNodeTool, reasoning trail (Apr 15)
- Gap loop refactor - analyst identifies gaps, researcher fills, loops until satisfied (Apr 15)
- Resume fix, mind map PDF, dashboard, gap-fill branch, PDF fetch, notes gate filter (Apr 17)

### v2 - Adaptive Claims-Model Loop (dev2 branch, active since 2026-04-24)
Budget-driven adaptive loop that reasons about its own completeness. Instead of hard-coding "what good research looks like," the agent decides dynamically which thread to pull next based on what it still doesn't know.

**Core concept:** Decompose query → verifiable claims → plan next action → execute → evaluate → repeat until budget exhausted or claims sufficiently supported.

| | Linear Pipeline (main) | Adaptive Loop (dev2) |
|---|---|---|
| Shape | Fixed 4 stages | Decompose → plan → act → evaluate → repeat |
| State | Pile of notes + raw task output | Structured claims model with confidence, support, contradictions |
| Stopping | Fixed stage count | Budget exhausted OR claims satisfied OR stalled |
| Citations | Synthesizer writes prose with URLs | Every citation is a fetched URL with a stored quote |
| Failure mode | Fabricated prose around real URLs | Open claims stay open; report says "unresolved" honestly |

**Dev2 commits (8 total, Apr 24–27):**
- Grounding validator, depth presets, pipeline hardening (Apr 24)
- Adaptive claims-model research loop (Apr 24)
- Make adaptive the only mode; defer universal-surface polish (Apr 26)
- Per-update source_url, placeholder-quote filter, run delete, home button (Apr 27)
- Roadmap entries for active-personality indicator + adaptive clarifications (Apr 27)
- Strategist turn - meta-loop reflection, corroboration awareness, persistence (Apr 27)
- Evaluator quality, sidebar repopulation, prose synthesizer (Apr 27)
- Prose scaffolding leak, takeaway notes, budget bump, clarifications wiring (Apr 27)

## Technical Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3.10+ / FastAPI on port **8765** |
| Frontend | **Next.js on :3015**, basePath `/DeepResearch` (migrated 07-01; vanilla-JS SPA retired) |
| Engine | **Adaptive loop** (`research_worker`→`adaptive_worker`) — the CrewAI linear pipeline is legacy |
| LLM | **Death Star nvfp4 gemma** (`gemma-4-26b-a4b-nvfp4`) via LiteLLM `:4000` (since 07-02) |
| Web Search | **DuckDuckGo** (`SEARCH_BACKEND=duckduckgo` — SETTLED decision; do not re-propose SearXNG) |
| MCP | FastMCP (stdio) - exposes tools to external clients |
| Worker | Detached subprocess survives MCP disconnects |
| Persistence | JSON + .log files in `jobs/`; reports in `reports/` |
| Infrastructure | **Production on the fleet** (aivm frontend/backend; inference on the Death Star via the AI VLAN) |

### Depth Presets

Simplified 07-06 (`77e88d5`) to a single **Light / Medium / Heavy** toggle (the old four-preset table incl. `ultra` is retired). Heavy-depth verified end-to-end 07-07.

### Module Layout (dev2)

| File | Role |
|------|------|
| `claims.py` | Claim, Evidence, ClaimsModel, Budget, status lifecycle, satisfaction/stagnation heuristics |
| `adaptive_planner.py` | Decompose query into verifiable claims; pick next-best action given state |
| `adaptive_evaluator.py` | Read action results, update claim statuses, raise new claims on new threads |
| `adaptive_worker.py` | The loop itself - wires observability, deterministic synthesizer from claims model |
| `api_server.py` | FastAPI web server + UI (~96KB) |
| `crew.py` | CrewAI agents + tasks (linear pipeline, still intact) |
| `tools.py` | WebSearchTool + FetchPageTool |
| `grounding.py` | Citation grounding validator |
| `learning_store.py` | Cross-run learning persistence |

## Use Cases

- Investment due diligence background research
- Donor prospect research
- Grant landscape research for Development Officers
- Competitive intelligence on peer institutions
- Policy / regulatory monitoring
- Staff ad-hoc research requests

## Key Contacts

| Person | Role |
|--------|------|
| Andrew Howerton | Project Owner / Builder |
| Chris / Steve | Executive audience (roundtable presentation 3/19) |
| Investment Team | Power users (due diligence support) |
| Development Officers | Power users (donor and grant research) |

## Strategic Value

- Positions Foundation with Perplexity/Deep Research-grade capability, locally owned
- Feeds into Research & Fundable Ideas Agent (shares search infrastructure)
- Reduces time staff spend on manual web research and summarization
- Complements Investment Risk Agent (front-loads context gathering)

---

**Filed Under:** Work Projects > 0. Active Priority
**Created:** 2026-03-24
**Last Updated:** 2026-04-27
