# Deep Research Agent — Roadmap

> **⚠️ STALE SNAPSHOT (pre-2026-05). Superseded — see the repo-root ROADMAP.md and the Second Brain 'Deep Research Tool' folder.** Production today: FastAPI :8765 + Next.js :3015 (/DeepResearch), Death Star nvfp4 gemma via LiteLLM :4000, DuckDuckGo search (settled), Light/Medium/Heavy depth toggle.

**Repo:** [Howie002/Deep-Research-Agent](https://github.com/Howie002/Deep-Research-Agent) (private)
**Last Synced:** 2026-04-27
**Current Phase:** SUPERSEDED — see root ROADMAP.md
**Active Branch:** `dev2`

---

## Adaptive Loop — dev2 (Active Work)

### Shipped ✅
- [x] Claims-model architecture (Claim, Evidence, ClaimsModel, Budget)
- [x] Adaptive planner — decompose query into verifiable claims; pick next-best action
- [x] Adaptive evaluator — update claim statuses with confidence deltas, raise new claims
- [x] Adaptive worker — budget-driven loop with observability (stream events, fetch persister)
- [x] Deterministic synthesizer — renders directly from claims model (honest about unresolved claims)
- [x] Depth presets (light/medium/heavy/ultra) — budget-based, not stage-count-based
- [x] Grounding validator + pipeline hardening
- [x] Made adaptive the only mode (linear pipeline preserved but deferred)
- [x] Strategist turn — meta-loop reflection, corroboration awareness, persistence
- [x] Prose synthesizer for more readable output
- [x] Evaluator quality improvements
- [x] Sidebar repopulation, run delete, home button
- [x] Per-update source_url tracking, placeholder-quote filter
- [x] Takeaway notes, budget bump, clarifications wiring

### Active ⬜
- [ ] Active-personality indicator (roadmap entry added 2026-04-27)
- [ ] Adaptive clarifications UX improvements (roadmap entry added 2026-04-27)
- [ ] Prose scaffolding leak fixes (partial — fix committed but may need further tuning)
- [ ] Wire adaptive loop into API + UI fully (currently CLI-primary)
- [ ] Live claims-board UI — frontend subscribes to `claims_snapshot` / `claims_update` events
- [ ] Grounding-pass integration with adaptive loop (claims.json exists but grounding.run_all() not yet wired)
- [ ] Compare outputs on 3 test cases that broke the linear pipeline:
  - David Riggs (thin profile)
  - Jennifer Ann Scasta (stage-handoff corruption)
  - Stephen Guetersloh (ghost citations)

### Backlog
- [ ] LLM-polish pass inside each claim block for prose elegance (keep structure mechanical)
- [ ] Cancel job endpoint
- [ ] PDF export for final report
- [ ] `.env` editor / setup wizard in web UI for non-technical users
- [ ] Report search/filter in web UI history sidebar

---

## Linear Pipeline — main (Shipped, Preserved)

### Shipped ✅
- [x] Initial 3-agent pipeline: Research Specialist → Critical Analyst → Report Synthesizer
- [x] Dynamic clarifying questions + UYBJ ("Use Your Best Judgment") button
- [x] Live Plan Evolution + Research Notebook
- [x] 4-stage pipeline with Gap Analyst + ThoughtNodeTool + reasoning trail
- [x] Gap loop refactor — analyst identifies gaps, researcher fills, loops until satisfied
- [x] Resume fix, mind map PDF, dashboard, gap-fill branch, PDF fetch, notes gate filter
- [x] CrewAI + LM Studio + LangSearch architecture (replaced Ollama/SearXNG/Streamlit)
- [x] MCP server with detached worker — survives LM Studio disconnects
- [x] Native function calling — pipeline from 90+ min to ~4 min
- [x] Web UI — FastAPI + vanilla JS, live log, report renderer, history sidebar
- [x] LangSearch rate limit fix, zombie job fix

---

## Testing & Validation
- [ ] Test on RTX PRO 6000 Blackwell hardware — benchmark speed improvement
- [ ] Compare adaptive vs. linear pipeline on same queries
- [ ] Test against 3–5 real Foundation use cases (investment, donor, grant landscape)
- [ ] Compare output quality to Perplexity / ChatGPT Deep Research
- [ ] Get feedback from 1–2 intended users (Investment Team, Development Officers)

## Integration Opportunities
- [ ] Feed Deep Research output into Investment Risk Agent as context
- [ ] Surface Deep Research as a tool in Foundation Chat / OpenWebUI
- [ ] Shared search layer (LangSearch) across Foundation AI tools

---

**Last Updated:** 2026-07-17
