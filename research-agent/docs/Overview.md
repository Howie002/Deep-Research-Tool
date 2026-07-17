# Deep Research Agent

> **⚠️ STALE SNAPSHOT (pre-2026-05). Superseded — see the repo-root ROADMAP.md and the Second Brain 'Deep Research Tool' folder.** Production today: FastAPI :8765 + Next.js :3015 (/DeepResearch), Death Star nvfp4 gemma via LiteLLM :4000, DuckDuckGo search (settled), Light/Medium/Heavy depth toggle.

## Repository

**Repo:** [Howie002/Deep-Research-Agent](https://github.com/Howie002/Deep-Research-Agent) (private)
**Local path:** `~/Documents/VS Code Projects/Deep Research Tool/research-agent/`
**Branch model:** `main` (stable linear pipeline), `dev2` (active — adaptive claims-model loop)

## Purpose

Fully local, multi-agent research tool that performs deep web research on any topic — automatically decomposing a question into verifiable claims, searching the web iteratively, evaluating evidence, and producing a structured, cited research report with confidence annotations. An in-house, air-gapped alternative to Perplexity Deep Research or ChatGPT Deep Research.

## Status

**Current Phase:** Active Development — Adaptive Loop (dev2) replacing Linear Pipeline (main)
**Last Updated:** 2026-04-27

## Architecture Evolution

### v1 — Linear Pipeline (main branch, shipped 2026-03-24 → 2026-04-17)
Fixed 4-stage pipeline: Research Specialist → Critical Analyst → Gap Analyst → Report Synthesizer. Each stage runs once with hard-coded iteration caps. Works but fragile — prone to fabricated prose around real URLs when evidence is thin, stage-handoff corruption, and ghost citations.

**Main branch commits (5 total):**
- Initial commit with CrewAI 3-agent pipeline (Apr 14)
- Dynamic clarifying questions, UYBJ button, Live Plan Evolution, Research Notebook (Apr 15)
- 4-stage pipeline with Gap Analyst, ThoughtNodeTool, reasoning trail (Apr 15)
- Gap loop refactor — analyst identifies gaps, researcher fills, loops until satisfied (Apr 15)
- Resume fix, mind map PDF, dashboard, gap-fill branch, PDF fetch, notes gate filter (Apr 17)

### v2 — Adaptive Claims-Model Loop (dev2 branch, active since 2026-04-24)
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
- Strategist turn — meta-loop reflection, corroboration awareness, persistence (Apr 27)
- Evaluator quality, sidebar repopulation, prose synthesizer (Apr 27)
- Prose scaffolding leak, takeaway notes, budget bump, clarifications wiring (Apr 27)

## Technical Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3.10+ / FastAPI on port 8000 |
| Frontend | Vanilla JS single-page app (no build step) |
| Agent Framework | CrewAI 1.11+ with native function calling |
| LLM | LM Studio (local, OpenAI-compatible) — currently Qwen 3.5 35B |
| Web Search | LangSearch API (AI summaries; fallback: DuckDuckGo) |
| MCP | FastMCP (stdio) — exposes tools to LM Studio / Claude Desktop |
| Worker | Detached subprocess survives MCP disconnects |
| Persistence | JSON + .log files in `jobs/`; reports in `reports/` |
| Infrastructure | Dev laptop → production target: RTX PRO 6000 Blackwell (4-card workstation) |

### Depth Presets (dev2)

| Preset | Fetches | Searches | LLM Calls | Wall Clock | Loops |
|--------|---------|----------|-----------|------------|-------|
| light | 5 | 5 | 40 | 300s | 15 |
| medium | 10 | 10 | 80 | 900s | 40 |
| heavy | 20 | 20 | 160 | 1800s | 60 |
| ultra | 40 | 40 | 320 | 3600s | 120 |

### Module Layout (dev2)

| File | Role |
|------|------|
| `claims.py` | Claim, Evidence, ClaimsModel, Budget, status lifecycle, satisfaction/stagnation heuristics |
| `adaptive_planner.py` | Decompose query into verifiable claims; pick next-best action given state |
| `adaptive_evaluator.py` | Read action results, update claim statuses, raise new claims on new threads |
| `adaptive_worker.py` | The loop itself — wires observability, deterministic synthesizer from claims model |
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
