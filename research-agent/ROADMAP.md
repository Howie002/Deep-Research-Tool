# Deep Research Agent — Roadmap

**Repo:** [Howie002/Deep-Research-Agent](https://github.com/Howie002/Deep-Research-Agent) (private)
**Last Synced:** 2026-04-27
**Current Phase:** Adaptive Loop (dev2) — active development
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
- [x] **Texas A&M near-exclusive scope for researcher queries** (2026-07-27, Dr. G feedback) — `FOUNDATION_SCOPE_*` in `config.py`, prepended to the decompose / next-action / strategist / prose prompts. Any query about researchers / experts / faculty / labs / programs now reports Texas A&M people **near-exclusively** (not a national list); non-TAMU names allowed only as incidental context. Narrow exceptions: user explicitly names another institution / asks for a national comparison, or a donor-prospect brief on an external subject (not a researcher query). Toggle `FOUNDATION_SCOPE_ENABLED=0`. Verified by decompose A/B on the cluster model: nutrition query 5/5 TAMU (0/5 scope-off), bare space-researcher query 5/5, Lowry Mays donor profile 1/6 (correctly not hijacked).
- [x] Prose synthesizer for more readable output
- [x] Evaluator quality improvements
- [x] Sidebar repopulation, run delete, home button
- [x] Per-update source_url tracking, placeholder-quote filter
- [x] Takeaway notes, budget bump, clarifications wiring

### Active ⬜
- [x] **✅ Fix 1 — confidence calibration + source-credibility tiering (2026-06-05, dev2).** Diagnosed from a Bush School run that showed **0 of 11 claims supported** despite correct, primary-source-cited prose. Root cause: SUPPORTED required `confidence ≥ 0.75` while the evaluator hands out conservative per-evidence deltas (0.5 strong / 0.3 secondary), so a single authoritative primary source (e.g. an org's own `.edu` page) stalled at PARTIAL. **Changes:** (1) `claims.py` — `SUPPORT_CONFIDENCE` 0.75→**0.60**; (2) `source_classifier.py` — new `credibility_weight(category)` (.gov/.edu ×1.4, NGO ×1.2, Reference ×1.15, News/Pro/Web ×1.0, Social/UGC ×0.7); (3) `adaptive_evaluator.apply_evaluation` — classifies each evidence's own URL and multiplies its confidence delta by the weight. **Verified:** unit test (official .edu single source → supported 0.70; Wikipedia 0.57 / generic-web 0.50 / Reddit 0.35 → stay partial = no over-promotion). Re-run of the same Bush School query: **0→17 supported**, the "parent university" claim (previously unresolved after 2 tries) now supported at 1.00, 8 unevidenced claims correctly held at conf 0.00. Note: re-run was not a perfectly matched A/B (non-deterministic decomposition, more claims/budget at medium depth) but the calibration shift is unambiguous. **Leftover = Fix 2 (claim generation):** duplicate/overlapping + occasionally hallucinated claims (e.g. an invented "Liz Tisch Sherman") — parked.
- [ ] **Worker concurrency — batched-round fetch + evaluate (entry added 2026-06-05)** — The adaptive loop is fully sequential (`run_adaptive`, no asyncio/threads): one plan→fetch→evaluate per iteration, one URL at a time (~52 serial LLM calls / ~633s on a medium run). After a search surfaces K URLs, those K fetches + K claim-extractions are mutually independent and only converge at the ClaimsModel merge — the seam to parallelize.
  - **Design:** loop goes from *one action/iteration* → *one plan decision → batch of K independent actions run concurrently (ThreadPoolExecutor) → one serial merge → re-plan once.* Threads (not async) — the LLM caller is blocking `urllib`, fetches are blocking `requests`; both release the GIL on I/O. Concurrent eval calls hit the proxy together → vLLM continuous-batches them on the **Nano** (the win is single-GPU; Death Star only raises the ceiling later, and is CUDA-blocked).
  - **Edits:** (1) split `integrate_result` (adaptive_evaluator.py) → `evaluate_result` (pure LLM+parse, parallel-safe) + `apply_evaluation` (serial cm-mutation). (2) batched-round executor in `run_adaptive` (adaptive_worker.py). (3) thread-safety: budget increments in the serial merge; lock the stream emitter + fetch persister (tools.py); cm mutations stay main-thread.
  - **Controls:** `DR_CONCURRENCY` flag + `DR_BATCH_SIZE` (K tied to depth: light 3 / medium 5 / heavy 8; well under vLLM `max_num_seqs=256`). Per-future error isolation.
  - **Validation:** same query sequential vs concurrent → equivalent claims/citations (no quality regression) + wall-clock drop (est. medium ~633s→~150–250s, light→~60–120s).
  - **✅ Shipped + measured (2026-06-05, behind `DR_CONCURRENCY`, default-OFF / flag-on, dev2):** evaluator split (`evaluate_result`/`apply_evaluation`), batched-round fetch+eval in `run_adaptive`, sink/tracker/counter locks. A/B on the Mitchell query (depth=light): sequential 623s (15 claims / 5 supported) vs concurrent **599.5s** (19 / **7**). **No regression — slightly better.** Batches formed (5 pages in 142s, 4 in 93s).
  - **⚠️ Key finding — single-Nano speedup is only ~4%, and it's a GPU-physics limit, not a bug.** The eval calls are **prefill-heavy** (long page input, short JSON output). Prefill is **compute-bound**, so N concurrent evals saturate one GPU (per-call cost rose 22.3s→28.5s under load) — only the *fetch I/O* overlapped. The decode-batching "near-free" benefit doesn't apply to prefill-bound calls. **⇒ This refactor is the *prerequisite* for Death Star to help** (concurrent calls → `least-busy` across GPUs spreads the prefill), but on one Nano it's compute-capped.
  - **Next levers (in priority):** (1) **trim the eval prompt** — drop the per-eval page cap from 6000→~2–3k chars (`_eval_user_prompt`); extraction only needs supporting quotes, so this cuts prefill directly and is likely the bigger single-GPU win. (2) Death Star (after CUDA ≥12.9). (3) parallelize searches + their evals (currently serial). (4) shared-infra note: 5 concurrent big-prefill evals add GPU contention for other aisandbox users on the one Nano — **decided 2026-06-05 to ship default-OFF** (flag-ready), flip on once Death Star is serving.
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

**Last Updated:** 2026-04-27
