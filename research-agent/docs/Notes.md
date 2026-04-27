# Deep Research Agent — Notes

---

## 2026-04-27 — Initial SB↔Repo Sync + Modernization

**Context:** SB project was frozen at 2026-03-24 (first successful test). Repo has undergone two major evolution phases since then. This sync catches up ~5 weeks of development.

### Repo renamed
- Was `research-agent` → now `Deep-Research-Agent` on GitHub
- Local path remains: `~/Documents/VS Code Projects/Deep Research Tool/research-agent/`

### Branch state
- `main` — linear pipeline, stable, 5 commits (Apr 14–17)
- `dev2` — adaptive claims-model loop, active, 8 commits (Apr 24–27). This is the future.
- `dev` — exists on remote but no local branch; appears to be an intermediate step before dev2

### What shipped on main since last SB update (Mar 24 → Apr 17)

| Date | Commit | Summary |
|------|--------|---------|
| Apr 14 | `4fbbf0c` | Initial commit — Deep Research Agent (repo recreated/restructured) |
| Apr 15 | `422f0c2` | Dynamic clarifying questions, UYBJ button, Live Plan Evolution, Research Notebook |
| Apr 15 | `2c25dbd` | 4-stage pipeline: Gap Analyst + ThoughtNodeTool + reasoning trail |
| Apr 15 | `21fa5af` | Gap loop refactor — analyst identifies gaps, researcher fills, loops until satisfied |
| Apr 17 | `2ecc5ec` | Resume fix, mind map PDF, dashboard, gap-fill branch, PDF fetch, notes gate filter |

**Key evolution:** Pipeline went from 3-stage to 4-stage with the addition of Gap Analyst. Dynamic clarifying questions + "Use Your Best Judgment" button added for research scoping. Mind map PDF export shipped.

### What shipped on dev2 (Apr 24 → Apr 27)

| Date | Commit | Summary |
|------|--------|---------|
| Apr 24 | `11b5558` | Grounding validator, depth presets, pipeline hardening |
| Apr 24 | `9ae0102` | Adaptive claims-model research loop — core paradigm shift |
| Apr 26 | `48b594c` | Made adaptive the only mode; deferred universal-surface polish |
| Apr 27 | `e16ac1a` | Per-update source_url, placeholder-quote filter, run delete, home button |
| Apr 27 | `3104e97` | Roadmap entries for active-personality indicator + adaptive clarifications |
| Apr 27 | `5625cdd` | Strategist turn — meta-loop reflection, corroboration awareness, persistence |
| Apr 27 | `3711a7d` | Evaluator quality, sidebar repopulation, prose synthesizer |
| Apr 27 | `a82e8d4` | Prose scaffolding leak, takeaway notes, budget bump, clarifications wiring |

**Key paradigm shift:** Moved from fixed 4-stage pipeline to budget-driven adaptive loop. System decomposes queries into verifiable claims, plans next-best action based on what it doesn't know, evaluates evidence with confidence deltas, and stops when claims are sufficiently supported or budget is exhausted. Honest about what it couldn't resolve — "unresolved" instead of fabricated prose.

### Known issues at sync
- `learning_store.json` is untracked (gitignored runtime artifact)
- Grounding pass not yet wired into adaptive loop
- API/UI routing still on linear pipeline; adaptive is CLI-primary
- No `docs/` folder in repo yet

---

## 2026-03-24 — Major Architecture Rebuild + First Successful Test

**Architecture overhauled — Ollama/SearXNG/Streamlit replaced entirely:**
- LM Studio replaces Ollama as local LLM backend
- LangSearch replaces SearXNG for web search (AI-generated summaries)
- CrewAI replaces custom agentic loop (3-agent pipeline)
- FastMCP replaces direct tool calls — MCP server for LM Studio / Claude Desktop
- FastAPI + vanilla JS web UI replaces Streamlit

**Key engineering problems solved:**
- LM Studio kills MCP connections after ~2 min → detached subprocess worker
- CrewAI defaulted to ReAct text generation (90+ min) → native function calling via litellm (~4 min)
- LangSearch rate-limited → threading.Lock with 1s delay
- Zombie jobs → atexit + SIGTERM handlers

**First successful test:** Query about Andrew Howerton completed in ~4 min with accurate results.

---

## 2026-03-19 — Roundtable Presentation

Deep Research Agent included in AI roundtable deck for Chris and leadership. Concept well received.

---

## 2026-03-18 — Framework Built & Pushed to GitHub

Original framework: Ollama + SearXNG + Streamlit. Agentic research loop: planner → search → extract → synthesize.

---
