# Deep Research Tool - Notes

## 2026-07-29 (Wednesday) - Feedback #107 resolved as "hide the tile", NOT a move to Portfolio Strategy; registry ports corrected

No code change in this repo. The change is in the dashboard registry (`foundation-ai-dashboard` `3b4f2d6`); recorded here because it determines who can reach this tool.

**The ask (#107, Andrew, 07-28):** *"Move Deep Research Agent to the Portfolio Strategy group off from the foundation wide group."* Left open yesterday pending his intent, because `foundationWide` is what *grants access*, not just where the tile sits.

**Resolved:** the only place Deep Research needs to exist right now is where it already does, **inside Living Catalog**, which dispatches college / department / student-org research to this agent. So the standalone tile is `hidden: true` and **nothing about access changed.**

**Why the literal move was the wrong action, measured rather than estimated:**
- 31 non-admin users; **exactly one** (`cspeier`) has any `user_departments` row. `dreweast` holds all nine department slugs but is an admin regardless. Yesterday's figure of "23 of 33 with no department" was itself understated - it is **32 of 34**.
- So `departments: ['portfolio-strategy']` resolves to **zero non-admins**. The tool would have become reachable by the 3 admins only.
- Telemetry over 930 calls (2026-06-25 → 07-27) shows the real users: **`aschilling` 92 calls (last 07-21)** and **`sguetersloh` 48 (last 07-16)**, both non-admin with no department, plus `dferrara` (admin, 132) and **658 unattributed calls with a NULL `user_email`** - the async job worker runs LLM calls outside a user request context, so most usage is invisible per-user. Worth fixing separately if per-user DR attribution ever matters.
- Both non-admins would have lost an actively-used tool silently. `decide()` checks `user_tool_grants` **before** `foundation_wide` and departments, so grants would have been the safe migration lever had a real move been wanted.

**Verified after deploy:** `tool_access` still `foundation_wide=1, departments=''` for `deep-research-agent`; 0 grants, 0 revokes; `/DeepResearch` route still `enforce`, `enabled=1`, `→ :3015`, returning 302 to login. `hidden` filters only the dashboard listings; `permissions.ts` never reads it.

**Registry entry corrected** in the same commit - stale since the 2026-07-01 migration off the FastAPI-only UI:
- `ports: [8765]` → **`[3015, 8765]`**. `:3015` is the Next frontend and the public surface `/DeepResearch` targets; `8765` is the FastAPI backend and is bound to **`127.0.0.1` only** (confirmed: `:3015` → 404 at `/`, `:8765` → 200, `ss` shows `127.0.0.1:8765`).
- Description still described ":8765 live web UI"; now describes the Next UI over an internal backend.

**Boot metadata for this repo was already correct** (`deep-research`, port 3015, health `/DeepResearch`, `depends_on: ["llm-proxy"]`) and needed no change; it sorts to position 3 behind the proxy in the 18-app order.

Related: [[../Foundation AI Dashboard/Notes|Foundation AI Dashboard Notes]] 07-29, [[../Foundation Infrastructure/Notes|Foundation Infrastructure Notes]] 07-29.

## 2026-07-27 (Monday) - Near-exclusive Texas A&M scope for researcher queries (Dr. G feedback)

Repo `Deep-Research-Tool`, `8872f0e`, `dev2`; :8765. Dr. G reported that a "find researchers" run returned only non-TAMU names. Added a config-driven `FOUNDATION_SCOPE_*` directive (`config.py`) prepended via a lazy `_scope()` helper to the adaptive planner's decompose / next-action / strategist prompts and the prose synthesizer: **near-exclusive** Texas A&M for researcher / expert / lab / program queries; overridable for explicit other-institution, external-subject donor-prospect, and comparative queries; `FOUNDATION_SCOPE_ENABLED=0` kill switch. Verified via a decompose A/B (5/5 TAMU-scoped with the fix vs 0/5 without) and a full end-to-end run that returned real A&M nutrition faculty (Seguin-Fowler, Regan Bailey; IHA). **Live for new jobs with no restart** — the API subprocess-launches a fresh worker per job that re-imports from disk. [[reference_deep_research_tamu_scope]]; full detail Activity Log 2026-07-27.

## 2026-07-17 (Friday) - Prompts 2+3 merged after first real test run

The due-diligence pipeline pitched to Chris (below) got its first real test - live subject was Beaver Aplin (Arch Aplin III, Buc-ee's), not a placeholder. Output was correct in substance (findings, categories, corroboration levels all accurate) but came back as a flat, unbranded text list - no Word formatting, no Foundation branding, despite Prompt 3 explicitly asking for it.

Diagnosis: natural-language color/branding instructions ("use maroon #500000") don't reliably survive LLM document generation - the model tends to default back to plain formatting regardless of what's described in prose. Recommended the more durable fix: build a real Word template once (custom Office theme, saved styles) and have the prompt say "apply the template" rather than describing formatting from scratch - pending Andrew's call on whether to invest in that.

**Action taken:** merged the "condense" and "format as branded report" prompts (previously Prompt 2 and Prompt 3) into a single Prompt 2, since testing showed they're really one job. Filed in [[../../../../1. Quick Notes/Due Diligence Research Prompts|Due Diligence Research Prompts]] (Quick Notes - still actively being iterated on, not yet promoted into this project's permanent docs).

## 2026-07-16 (Thursday) — Copilot Researcher-seeded "Brutal Critics" mode pitched to Chris Speier

**Context:** Chris Speier emailed Andrew + Mattie Snell about a new TAMU policy requirement - naming gifts now need a reputational-risk review before a donor's name goes on something (regulation 51.06.01, ethics policy 07.01). Chris's contact Jody wants something closer to a WealthX-style self-serve tool he could run himself and, if required, submit alongside the naming gift agreement. Chris floated the Deep Research Tool as a candidate but flagged it produces "an absolute ton of information" - too much for what Jody needs.

**Andrew's pitch back to Chris (sent same day):**
1. Build a due-diligence prompt any user can run via **Copilot's standard Researcher agent** - does the heavy lifting of the first pass, on the frontier model (currently GPT-5.6 "Sol" or latest SOTA), at max settings.
2. Feed that output into a new native section of the Deep Research Tool that **"Brutal Critics" the results** - adversarially checks for missing considerations and does a deeper dive to fill gaps.
3. Output comes back in a jointly-agreed **simple format** - the details that matter, none of the vanilla filler.

**Explicit open tradeoff Andrew flagged to Chris:** this requires real cross-platform hand-off (Copilot → Deep Research Tool), which is more work than customizing Copilot Researcher's own output to be "good enough" in a single run - but a single-run approach eliminates the multi-agent review entirely. Andrew called it "a bit of a gamble," not recommended without testing first.

**Next step proposed:** test the workflow on a prior recent large gift before committing to a build. Asked Chris for a recommendation on which one - not chosen yet.

**Filed:** Roadmap.md updated same day - "Copilot Researcher-Seeded Due Diligence Mode" section (supersedes the initial "additional research" framing from earlier today with the "Brutal Critics" name and the tradeoff decision point).

---

## 2026-07-07 (Tuesday) — Browser-proxy-path live run verified (Dominic session)

Closed the remaining gap from 07-06: a third live run (medium depth, "endowment spending-rate policies + Texas UPMIFA" — a genuinely useful report for Andrew) executed entirely through the browser's transport chain (job POST + SSE via the Next.js rewrite on `:3015/DeepResearch/api/...`, not the direct backend). **222 SSE events over 530s and the `done` event carried the full report payload in `result`** — the payload-loss bug from Andrew's first browser run did not reproduce; the `2e75794` self-heal stays as belt-and-braces. Report ~3k words with real citations (NACUBO FY25, Texas Property Code ch. 117, ERIC); grounding audit visibly stripped unverifiable URLs; report listed in the frontend reports API; telemetry logged 51 `research` calls + 1 `learning` reflection to the dashboard. Only remaining unknown: the literal in-browser render (one human click in Edge). Prereq re-verified first: gemma + nomic green through `:4000`. Housekeeping same day: local git remote repointed to `Howie002/Deep-Research-Tool.git` (fetch verified) and `research-agent/.env.example` corrected to match production defaults (cluster proxy `10.2.35.10:4000` instead of a localhost LM Studio that doesn't exist; `API_PORT=8765`). Roadmap entry: repo `ROADMAP.md` 2026-07-07 section (commits `9609d06`, `0bbc118`, `a01590f`, `5221b67`; dev2). Note for the record: `SEARCH_BACKEND=duckduckgo` is a settled prior decision (DDG resolves better than the local SearXNG instance for this tool) — not an oversight, don't re-propose the flip ([[feedback_deep_research_ddg_stays]]).

**Also same day (frontend):** ported Tetrix Lite's snip-style feedback screenshot capture into `frontend/src/components/FeedbackButton.tsx` + `api/feedback/route.ts` + `feedbackReport.ts` (branch `dev2`, commit `b6da5ad`) as one of 12 tools in a fleet-wide rollout ([[project_dashboard_feedback_panel]]). Verified end-to-end; rebuilt + restarted on :3015.

**CLOSED same day — browser render confirmed at Heavy depth:** Dominic ran a real browser session at Heavy (first heavy-preset run ever): report card rendered on completion and "What Remains Open" was normal prose (degeneration guard healthy at the largest synthesis yet). Roadmap updated (commit `a01590f`). Nothing about the live-run pipeline remains unverified.

**Follow-up same day — degeneration watchdog (commit `0bbc118`):** Dominic caught the run's "What Remains Open" section devolving into 7.6k chars of repetition word-salad: gemma collapsed near the end of the 2400-token synthesis, and the `repetition_penalty`/`repeat_penalty` params in the payload are silently dropped at the LiteLLM hop (only `frequency_penalty` survives). Fix per the fleet watchdog philosophy: deterministic `_trim_degenerate_tail()` in `adaptive_worker.py` (any run >400 chars with no sentence terminator/newline gets truncated at the last healthy boundary; a dangling header gets a stock body). Verified three ways: trims the real corrupted report (repaired in place), leaves healthy prose byte-identical, and a fresh light-depth live run (federal endowment excise tax post-OBBBA) came out clean (1.1k words, max run 225 chars). Same failure family as the Tetrix max_tokens queue-wedge: [[feedback_max_tokens_runaway_and_basepath]] updated with this third face.

## 2026-06-25 (Thursday) — Telemetry instrumentation + per-user attribution (Dominic)

Instrumented Deep Research for the dashboard's cross-tool telemetry ([[project_dashboard_telemetry]]) and added **per-user attribution** of research tasks. Key gotcha: the live engine is `adaptive_worker._build_llm_caller` (the direct chat/completions path) — **NOT** `crew.py`/litellm, which is the legacy `run.py` CLI path. Research jobs run in a subprocess, so the submitter's identity rides via the job JSON → the worker calls `set_user()` (process-global) → all `report_usage` in that process attribute to the submitter. `api_server.start_job` reads `x-foundation-user`; `/api/clarify` instrumented with explicit user. Verified ~29 attributed `research` rows for the submitting user. Commits `7a1c588` → `fc7a935` (branch `dev2`).

---

## 2026-07-06 - First live run VERIFIED + report-display hardening + simplified controls

- **First live-run verification done** (the item that waited on the Death Star CUDA fix): Andrew's real query (117s, medium) + a light verification run both produced cited reports from real web sources; the SSE stream was captured via curl and the `done` event carried the full report payload on the direct path.
- **Bug found via Andrew's first run:** the browser's completion event arrived without the report text (lost in the browser-side proxy chain or a race), so the UI said "report ready" but showed nothing; the report WAS saved server-side. Frontend now self-heals: payload-less completion fetches the newest saved report, the Recent-reports sidebar refreshes on every completion, and the report card scrolls into view. Commit `2e75794` (dev2).
- **Pre-use control cleanup per Andrew:** depth is now the only knob (Light/Medium/Heavy); the Ultra preset, Thorough-mode checkbox, and Clarify-first button/modal were removed from the UI (backend still accepts the params). Em dashes swept from all user-facing strings. Commit `77e88d5`.
- NOTE: GitHub reports the repo was renamed remotely to `Howie002/Deep-Research-Tool`; pushes work via redirect but the local remote URL should be updated sometime.

---

## 2026-06-05 - Planned benchmark: DR tool vs Dr. G's real cases (from 30-min meeting)

30-min meeting (Dominic + Andrew + Dr. G, 6/5). **Dr. G will provide older, *real* research cases** — project leads that donors were looking to contribute to, with his own human research/briefs as the ground truth. **Plan: benchmark the DR tool's output against his work** to get a measurable verdict on quality (vs. eyeballing single runs).

**Status: BLOCKED — awaiting the cases from Dr. G.** His next move; nothing to build until they land. This becomes the gold-standard eval and supersedes the older ROADMAP "3 cases that broke the linear pipeline" item.

**When cases land — comparison approach (per case, DR tool vs his brief on the same project lead/PI):** coverage/recall (key facts he found), accuracy (errors/hallucinations vs his verified facts — cf. the Speier failure), grounding (DR cites sources; his tacit knowledge often isn't written), net-new (what each found the other missed), actionability (tear-line/next-steps for a DO), and time (DR minutes vs his hours). Score 1–5 per dimension + qualitative notes in a comparison matrix → points straight at refinement priorities. **⚠️ Data:** PIs/project leads are public (fine to run through DR); **keep donor identities out of the tool entirely** — research the project/PI, treat donor names in his briefs as reference-only.

---

## 2026-06-05 - Fix 1: confidence calibration + source-credibility tiering (effectiveness)

Diagnosed from a real **Bush School of Government** run in the Living Catalog research queue: the brief's prose was accurate and primary-source-cited, yet the ledger said **"0 of 11 claims supported."** That's a calibration bug, not a research failure — and "0 supported" makes a good brief look untrustworthy to a DO.

**Root cause:** a claim became SUPPORTED only at `confidence ≥ 0.75`, but the evaluator assigns conservative per-evidence deltas (0.5 strong / 0.3 secondary / 0.15 mention). So one authoritative primary source (an org's own `.edu` page stating its dean) = 0.5 → stuck at PARTIAL; even two quotes only reached ~0.65–0.70. Nothing crossed 0.75.

**Fix (on `dev2`):**
- `claims.py` — `SUPPORT_CONFIDENCE` **0.75 → 0.60** (named constant).
- `source_classifier.py` — new **`credibility_weight(category)`**: `.gov`/`.edu` ×1.4, NGO ×1.2, Reference (Wikipedia) ×1.15, News/Professional/Web ×1.0, Social/UGC ×0.7. (This is the parked "source-credibility / positive tiering" lever, now shipped.)
- `adaptive_evaluator.apply_evaluation` — classifies each piece of evidence's **own** URL and multiplies its confidence delta by the weight (more precise than the action-level category, esp. for search hits).

**Verified — fixes the problem without over-promoting:**
- Unit test: official `.edu` single source → **supported (0.70)**; Wikipedia 0.57 / generic-web 0.50 / Reddit 0.35 → **stay partial**.
- Bush School re-run: **0 → 17 supported**; the "parent university" claim (previously *"tried 2×, unresolved"* — absurd, it's Texas A&M) now **supported at 1.00**; **8 unevidenced claims correctly stayed at conf 0.00**; 1 refuted (a non-existent certificate). *(Caveat: not a perfectly matched A/B — non-deterministic decomposition produced 32 claims at medium depth vs the original 11; the calibration shift is nonetheless unambiguous.)*

**Leftover, parked as Fix 2 (claim *generation*, not calibration):** the decomposer/evaluator sometimes mints duplicate, overlapping, or **hallucinated** claims (an invented person "Liz Tisch Sherman"; speculative facilities). Calibration correctly leaves these unsupported, but they waste budget and clutter the brief — fix by constraining claim generation to the query + fetched evidence. Also still parked: the eval-prompt-trim speed lever. Detail in DR repo `ROADMAP.md`.

---

## 2026-06-05 - Worker concurrency refactor (batched-round fetch+evaluate; flag-ready, default-OFF)

Added optional concurrency to the adaptive loop (was fully sequential) to speed up runs for the test-refine loop. **On `dev2`, behind `DR_CONCURRENCY` (default OFF — set `=1` to enable); `DR_BATCH_SIZE` overrides per-depth batch (light 3 / medium 5 / heavy 8).**

**What changed:**
- `adaptive_evaluator.py`: split `integrate_result` → `evaluate_result` (pure LLM+parse, parallel-safe) + `apply_evaluation` (serial cm-mutation); `integrate_result` kept as a back-compat wrapper.
- `adaptive_worker.py`: the fetch branch now builds a batch of independent queued URLs and runs **fetch → evaluate concurrently** (ThreadPoolExecutor), then applies results **serially** to the claims model. Thread-safe LLM-call counter.
- `tools.py`: locked the stream emitter, fetch persister, and shared trackers (`_budget`/`_fetched`/`_diversity`) so concurrent fetches don't corrupt them; network I/O stays unlocked so fetches overlap.

**A/B measured (Mitchell query, depth=light):** sequential 623s (15 claims / 5 supported) vs concurrent **599.5s** (19 / 7). **No regression — slightly better quality. But only ~4% faster.**

**Key finding (the important part):** the eval calls are **prefill-bound** (long page input, short JSON out). Prefill is **compute-bound**, so N concurrent evals saturate one GPU (per-call cost rose 22.3s→28.5s under load) — only the *fetch I/O* overlapped. The "batching is near-free" effect applies to *decode*, not prefill. **⇒ Concurrency is the prerequisite for Death Star to help (multi-GPU spreads prefill via least-busy), but on a single Nano it's compute-capped.** Hence default-OFF for now (avoids GPU contention for other aisandbox users for a 4% gain); flip on once Death Star serves.

**Next single-Nano lever (not yet done):** trim the per-eval page cap (6000→~2–3k chars in `_eval_user_prompt`) to cut prefill directly — likely a bigger single-GPU win than concurrency; needs its own speed-vs-quality A/B. Full detail + measured numbers in the DR repo `ROADMAP.md` (Active section).

---

## 2026-06-05 - Now powers R&FI's seamless research (consumer integration; no DR code change)

The Deep Research Agent is now the engine behind R&FI's one-click research (replacing the manual *prompt → Claude → paste JSON* loop). **No changes to the DR Agent itself** — the integration is an adapter inside R&FI that reshapes the agent's markdown report into R&FI's structured JSON. Full writeup lives in the R&FI Notes (2026-06-05). Two DR-Agent behaviors that shaped the integration, recorded here for future callers:

- **`POST /api/jobs` caps `query` at 2000 chars** — callers must send a clean question, not a giant enriched prompt (R&FI was doing the latter; fixed on their side).
- **A job is DELETED the instant `GET /api/jobs/{id}` returns `status:complete`** (save_run + cleanup_job). So the completion result is **single-consumption** — a caller that misses that exact poll gets a 404 afterward. Mitigation for callers: the run is still saved under `/api/reports` (matchable by the query line), so recover from there. R&FI added a 404→report-recovery fallback. *(Possible future DR-side improvement: a short grace window before cleanup, or a terminal-state response that returns the result without 404.)*

Live-validated with a ~13-min real run (George P. Mitchell): 18/33 claims supported, real `txamfoundation.com`/`cgmf.org` sources → clean structured brief in R&FI.

---

## 2026-06-04 - 6/3 work pushed to remote (auth excluded); panel wiring; reboot recovery

- **Pushed all 6/3 work to `origin/dev2`** (commit `f578bee`): entity anchoring + evaluator guard, URL-norm dedup, evidence dedup, source-credibility filter, SearXNG backend + DuckDuckGo default, loop-balance (circuit-breaker/FETCH-FIRST/budget), per-depth budget tuning, Foundation UI re-skin. ⚠️ **The LLM-endpoint auth fix (bearer header) was deliberately EXCLUDED from the commit per request** — `grounding.py`/`learning_store.py` not committed; auth lines stripped from `config.py`/`adaptive_worker.py`/`tools.py`'s staged copies; `api_server.py` (discover auth-probe) excluded. Verified the pushed commit has 0 auth lines. **Those auth edits remain LOCAL-ONLY in the working tree** (so the running agent still talks to the cluster) but are not in the repo. *(NB: branch is `dev2` = the active dev branch, 10 commits ahead of the stale `dev`/`main` at Apr 17.)*
- **Restarted after the 6/3 reboot** (`:8765`, nohup — still not a systemd service; that + path-based persistence remain TODOs).
- **Wired into the dashboard panel** at `/deep-research`: NPM location + UI converted to **relative paths + a path-prefix base-href** (so it works under `/deep-research` AND at `:8765`). UI backups: `static/index.html.dark-backup` (pre-reskin), `static/index.html.pre-basepath` (pre-relative-paths). ⚠️ **Update (later 6/4): NPM was uninstalled** (Cody — tools now go through the Entra App Proxy directly), so the `/deep-research` NPM location is **gone**. Panel access to Deep Research now depends **entirely on the Entra App Proxy publishing `/deep-research`** (Azure/Cody) — not yet done, so the tile won't work until then. The agent is still reachable directly at `:8765` on the box. The relative-path/base-href UI changes remain correct and are forward-compatible with whatever path it's eventually served under.
- **Live runs UNBLOCKED (end of day):** the LLM proxy `10.2.35.10:4000` is back up — the cluster LiteLLM proxy was down from the reboot, but the **gemma vLLM on the Nano (`10.2.35.30:8020`) never went down**; restarted just the proxy (`AI-Distributed-Inference-Cluster/litellm/start_proxy.sh`) and verified `gemma-4-26b-a4b-nvfp4` responds through `:4000`. Deep Research can run real queries again (still `nohup`, not a service). Recovery steps: [Foundation Infrastructure/Reboot-Recovery-Runbook.md](../Foundation%20Infrastructure/Reboot-Recovery-Runbook.md).

---

## 2026-06-03 - 2nd donor test (George P. Mitchell) — weaker run, 3 diagnosable causes (good signal)

Ran a 2nd public-benefactor prospect profile to see if today's fixes generalize beyond Lowry Mays. Subject: **George P. Mitchell** (Mitchell Energy / fracking pioneer / The Woodlands / A&M '40, Mitchell Institute for Fundamental Physics & Astronomy; d. 2013). Heavy + thorough, DDG backend. Job `02f7544a`.

**Result: 0 of 21 supported, 7 partial, 0 refuted, 14 open** (27 fetches / 15 searches / 2709s) — markedly weaker than the Mays run (12 supported). **NOT a regression in today's fixes** — three separate, diagnosable causes:

1. **Single-source partials never corroborated.** All 7 partials are "1 supporting, 0 contradicting." The loop found each good fact *once* (the $20M 2012 legacy gift, the 1940 petroleum-eng degree, fracking, the Institute) but "supported" needs confidence ≥0.75 (effectively a 2nd corroborating source); budget ran out before double-sourcing.
2. **Fetch budget burned on unreadable sources.** It fetched `txamfoundation.com/*.aspx` (JS-rendered ASP.NET), `cdn.txamfoundation.com/*.pdf`, `tamus.edu/*.pdf` (fund-accounting modules) — all yield ~nothing to the static fetcher → corroboration budget wasted. **The JS/PDF fetcher limit is the binding constraint for this subject** (predicted mid-run: Mitchell's A&M facts live disproportionately on the Foundation's own JS-heavy site).
3. **🔴 NEW: credibility filter too narrow + synthesis source-ranking gap.** The prose leaned on `studentsandparents.com` and pulled a WRONG fact: *"net worth … $2.5 billion as of 2024"* — Mitchell **died 2013**, so a 2024 net worth is nonsense (present-tense, alive-implying). Content-farm bios (studentsandparents, mabumbe, goodreturns, celebritynetworth) dominated. My `LOW_CREDIBILITY_DOMAINS` denylist (grokipedia/ask/answers) is too small to catch these. Worse: the **best** sources WERE fetched — `chron.com` ("$35M gift to A&M physics", "largest donor to Texas A&M"), `philanthropy.com` obituary, `physicstoday.aip.org`, Wikipedia — but the synthesizer used the *weakest* source for its headline instead of those.

**What worked:** entity clean (correct George P. Mitchell, no wrong-person drift among several notable namesakes); prose synthesizer produced a readable, honest brief (explicitly flags 14 unresolved items, no fabrication beyond the bad net-worth fact it inherited from a junk source); credibility filter kept grokipedia out; the right facts were all surfaced (just under-corroborated).

**Action items this surfaced (Roadmap):**
- **Expand `LOW_CREDIBILITY_DOMAINS`** to cover SEO/content-farm bio sites (studentsandparents.com, mabumbe.com, goodreturns.in, celebritynetworth.com, …) — or better, a *positive* credibility tiering (news/.edu/.gov/Wikipedia/foundation-official > bio aggregators).
- **Source-ranking in fetch priority AND synthesis** — prefer high-credibility domains; don't let a content farm headline the brief when chron.com/philanthropy.com were fetched.
- **JS/PDF fetcher (Playwright + PDF text extraction)** — promote from "secondary" to near-term; this run proves it's the binding constraint for Foundation-site-heavy subjects.
- **Recency/liveness guard** — a deceased subject (d.2013) got a present-tense "2024 net worth"; add an is-living/key-dates check (same gap flagged on the Mays run, still open).
- **Corroboration-aware budget** — when many claims sit at partial w/ a single source, spend remaining budget seeking a 2nd source rather than raising new claims.

Lesson echoes the Mays run: **one real query reorders the priority list.** Mays worked because his facts live on clean static news/Wikipedia; Mitchell exposes the JS-fetcher + source-quality gaps because his live on JS/PDF + content farms.

---

## 2026-06-03 - Quality polish (evidence dedup + source-credibility filter) + on-brand Foundation UI re-skin

**Two polish fixes shipped** (from the DDG-run wart list), both unit-verified:
- **Evidence dedup in synthesis** (`adaptive_worker.py`) — added module-level `_norm_url_key()` + `_dedup_evidence()`; wired into `_claim_block()` so duplicate quotes (same normalized URL + quote) collapse, and the per-claim evidence *count* reflects the deduped set. Fixes the "same source quoted twice" wart (myaggienation/IHeartMedia appeared twice each). Test: 5 evidence (2 dup pairs incl. www/trailing-slash variants) → 3.
- **Source-credibility filter** (`tools.py`) — `LOW_CREDIBILITY_DOMAINS` denylist (grokipedia.com + ask/answers) + `_is_low_credibility()` (suffix match, covers subdomains), applied in `_execute_search()` right after the backend returns, so low-cred domains are dropped **before** fetch/cite. Fixes the grokipedia.com-as-source wart. Logs how many were dropped per query.
- **Deferred:** near-duplicate *claim* merge (the two death claims "Sept 2022" supported + "Sept 12 2022" partial) — needs fuzzy/LLM matching to merge safely across different statuses/confidences; not worth the risk of a blind string merge. Logged on Roadmap.
- **Off-entity force-fetch filter** also still deferred (Barry Diller/Robert Pittman pages) — needs entity-relevance scoring of surfaced URLs before they enter the fetch queue.
Agent restarted on `:8765` to pick up the `.py` changes.

**On-brand Foundation UI re-skin (in-place).** The tool's web UI (`static/index.html`, single 2855-line file) was a dark indigo/purple theme — completely off-brand vs the Foundation fleet (dashboard, HR, K-1), which is a **light maroon `#500000`** theme. Chose **re-skin in place** (Dominic's pick) over a Next.js rebuild or ground-up rewrite — preserves every feature (live stream, mind map, citation tooltips, history, clarifications, PDF export) and is the fastest path to on-brand.
- Extracted the canonical Foundation brand sheet from foundation-ai-dashboard/hr-analytics-platform: maroon `#500000` / dark `#3c001c` / light `#732f2f`, gold `#C6A84B`, light-blue accent (`#3B82F6` family — per Dominic, blue is also in the brand guide), bg `#F6F6F6`, white surfaces, taupe `#D6D3C4` borders, system fonts, `tamu-foundation-logo.png`.
- Backed up the original to `static/index.html.dark-backup`.
- Remapped the `:root` token set to the light Foundation palette + appended a "FOUNDATION LIGHT THEME OVERRIDES" layer (wins by source order) that re-grounds prose, citations, chips, badges, stage pipeline, severity/grade, buttons, inputs, tabs, status pills, report cards, mind-map for the light surface. **Old indigo accents → Foundation light-blue; brand chrome (header, primary button, brand-gradient) → maroon; highlights → gold.**
- Rebranded the header: maroon bar with gold underline + real `tamu-foundation-logo.png` + "Deep Research / Texas A&M Foundation" wordmark; updated `<title>` + favicon. Copied the logo into `static/`.
- Fixed d3 mind-map text fills (were near-white → invisible on light), node/edge palettes (query→maroon, thought→blue, led_to→maroon), tooltips (dark→white), and every Tailwind indigo/purple utility class (`border-indigo-500`, `accent-purple-500` → `#500000`).
- Verified structurally: HTTP 200, logo reachable, brand markup present, no invisible near-white inline text remaining. **No headless browser on aivm (Playwright still the unchecked TODO) so couldn't screenshot — needs a live visual eyeball at `localhost:8765` / `10.2.35.10:8765`.**
- **Note for later:** this in-place skin is the pragmatic path; the cleaner long-term move is a **Next.js rebuild reusing the shared Foundation `Sidebar` component** (collapsible maroon rail) the rest of the fleet uses — more cohesion + fits the path-based deploy pattern. Logged on Roadmap.

---

## 2026-06-03 - ✅ DDG backend + full fix stack VERIFIED end-to-end — 12/27 supported, genuine prospect profile (internal-only)

**The decision that unblocked everything: switch the search backend to DuckDuckGo (keyless, internal).** Per Dominic's constraint that the solution "has to be internal" (no commercial/keyed API), we couldn't go to Brave/Tavily/Serper. DDG's `DDGS().text()` is keyless and — unlike SearXNG's free scraped engines — does **not** CAPTCHA/suspend under deep-run load. Pre-flight direct test: `"Lowry Mays" Clear Channel Texas A&M` → 8/8 Mays-relevant, 0 Kyle Lowry, honored `"quotes"` and `-basketball`. Set `.env SEARCH_BACKEND=duckduckgo`, restarted agent on `:8765`.

**This is the run that validates the entire session's fix stack** (auth header → DDG search → entity anchor+guard → URL dedup → loop balance → decomposition), all on an **internal-only** backend driving Gemma-4-26B on the Nano.

**Job `0d658409` — heavy+thorough Lowry Mays prospect profile. Final: 12 of 27 claims supported, 11 partial, 0 refuted, 4 open.** 21 fetches / 15 searches / 76 LLM calls / 2315s. Stopped cleanly on the search-cap budget.

Every prospect-research pillar landed, cited to the RIGHT sources:
- **Source of wealth:** founded Clear Channel Communications 1972 w/ Red McCombs (one radio station → media empire, now iHeartMedia) — conf 1.00 (Wikipedia/IHeartMedia, referenceforbusiness, HBS)
- **A&M major gifts:** **$15M endowment to the business school in 1996** (named in his honor) — conf 1.00; **+ another $25M in 2017** — conf 0.80 (myaggienation, Houston Chronicle)
- **Philanthropic vehicle:** founded the **Mays Family Foundation in 1994** w/ wife Peggy; Bexar County focus — conf 1.00 (maysfamilyfoundation.com/history, altss)
- **A&M affiliation:** **1957 grad, petroleum engineering**; two terms on the **A&M System Board of Regents** (chaired 2003-05) — conf 0.90 / 0.65 (tamu stories, AP)
- **Recency/status:** **died September 2022, age 87** — conf 1.00 (TAMU Stories, Deadline)

**What this proves vs. prior runs:**
- **Entity: flawless** — zero Kyle Lowry / zero NBA across 21 fetches (anchoring + evaluator guard held).
- **Death + successor-foundation pivot: CAUGHT** — it followed the 2022 death to maysfamilyfoundation.com + news.mays.tamu.edu, the exact pivot a real prospect researcher needs once the principal is deceased. (Prior runs never surfaced the death.)
- **Loop balance: held** — 21 fetch / 15 search (was inverted ~7 fetch / 30 search). FETCH-FIRST + circuit-breaker 2→1 + budget rebalance all working.
- **Decomposition: fixed** — 6 real sub-claims at start (was 1 mega-claim), grew to 27 via Strategist's adaptive claim-raising.
- **Honesty intact** — 4 open questions (Harvard MBA? exact Regents dates?) reported unresolved, not fabricated.

**Minor warts (all already on Roadmap, none harmful):**
- Duplicate quotes — same source credited twice for one claim (evidence-dedup gap, cosmetic).
- 2 off-entity pages fetched (Barry Diller, Robert Pittman) via circuit-breaker force-fetch — guard kept them OUT of output but they burned budget → "don't force-fetch off-entity URLs" item still stands.
- One source is grokipedia.com (AI-generated wiki) → a source-quality/credibility filter is worth adding.
- Death claim fragmented into two (one supported "Sept 2022", one partial "Sept 12 2022") — claim-dedup nit.

**Bottom line: an internal-only stack (DuckDuckGo + Gemma + the adaptive loop) now produces a fundable, well-cited prospect profile.** The earlier "keyed provider is the robust fix" conclusion is superseded for the internal-constraint case — **DDG is the keyless backend that doesn't CAPTCHA under load.** Loop-balance + entity + dedup fixes are now VERIFIED (not just "shipped but unverifiable"). Remaining levers are quality polish (evidence/claim dedup, off-entity fetch filter, source-credibility filter) and the model swap (Gemma→Qwen/Tongyi) once Deathstar is on the VLAN — now an enhancement, not a fix.

---

## 2026-06-03 - Loop-balance fixes shipped; verification blocked by SearXNG flakiness → keyed provider warranted

Shipped 3 loop-balance fixes: **(a)** circuit-breaker fires at `consecutive_searches >= 1` (was 2) — fetch what you found before searching more; **(b)** planner `_NEXT_ACTION_SYSTEM` FETCH-FIRST rule; **(c)** rebalanced adaptive budget so searches are capped ~half of fetches (heavy 32 fetch / 15 search; medium 18/9). All syntax-clean, agent restarted.

**Verification run REGRESSED to 0/15 — but NOT due to the loop code.** Search was degraded during the run (surfaced only the 5 Kyle Lowry pages → force-fetched → evaluator guard rejected → 0 support). Engine state fluctuates run-to-run; checked right after: `brave/duckduckgo/qwant/startpage` suspended again, only google/bing/mojeek/wikipedia up. **Cause: ~6 heavy runs today (hundreds of engine hits) kept the free scraped engines in CAPTCHA/suspension faster than they recover.** The 2.5s throttle helps within a run but can't beat sustained back-to-back load. Loop ratio this run: 5 fetch / 15 search — it hit the search cap because once the 5 bad URLs were fetched, search surfaced nothing NEW to fetch, so the circuit-breaker had nothing to force.

**Honest conclusion: we've hit the ceiling of the no-key SearXNG route.** The loop-balance changes are sound but unverifiable while search is degraded — search reliability dominates everything downstream. Under realistic multi-run/multi-user load the free scraped engines WILL keep suspending. **A keyed search provider (Brave API free 2k/mo, Tavily, or Serper — API-based, never CAPTCHAs) is the genuinely robust fix; tool already has `BraveBackend`, needs only a key in `.env`.** Recommend getting a key, then re-verifying loop-balance on a clean search state. Loop-balance + entity + dedup + throttle fixes all stay (correct + necessary).

---

## 2026-06-03 - SearXNG solutions shipped + final verification: output now correct; remaining gap = loop balance

Implemented the no-key SearXNG fixes: **(a)** throttled+retried `SearXNGBackend` in `tools.py` (global 2.5s min-interval + 3× backoff so the deep-run search burst stops CAPTCHA-ing the engines); **(b)** tuned the SearXNG engine set (added resilient keyless engines mojeek/qwant/wikipedia/wikidata alongside google/ddg/bing) via the container-mounted `settings.yml` + restart. Search-layer verified: a disambiguated "Lowry Mays" query flipped from ~0 to **22/29 relevant results** (was all Kyle Lowry); SearXNG result count 7→32.

**Final verification run (all 4 fixes together — entity anchor + dedup + throttle + engine tuning):**
- ✅ **Output is now correct** — accurate, well-cited profile: Lowry Mays '57, founded Clear Channel, ~$25B sale w/ McCombs, investment-banking→radio origin, Mays Business School; every prose claim cited to the RIGHT sources (wikipedia/Lowry_Mays, mays.tamu.edu). **Zero NBA facts in the output.**
- ✅ **Evaluator entity-guard works** — 5 Kyle Lowry pages still got *fetched*, but the guard credited none → no contamination in the profile.
- 🟡 **Supported tally still low: 1/19 (3 partial, 15 open).** Root cause is now clearly **loop balance**, not search/entity: **30 searches / only 7 fetches** → too few pages → limited corroboration. Many claims (Harvard MBA, petroleum eng, boards) weren't in the ~5 useful fetched pages.
- 🟡 Planner still **force-fetches off-entity URLs** (circuit-breaker grabs `surfaced_urls[0]` regardless of entity) — wasted budget though harmless to output.
- 🟡 Death (2022) / Mays Family Foundation pivot still not surfaced.

**Net:** the SearXNG + entity fixes delivered correct entity + reliable search + accurate cited output. Remaining gap is a well-scoped **loop-balance** problem (fetch more / fewer searches / don't force-fetch off-entity URLs) and ultimately the model for richer extraction — distinct from everything fixed so far.

---

## 2026-06-03 - Entity + dedup fixes shipped; verify run exposed the REAL bottleneck = SearXNG engine blocking

Shipped two fixes to the in-house tool: **entity anchoring** (anti-drift rule in planner `_NEXT_ACTION_SYSTEM` + entity-match guard in evaluator `_EVAL_SYSTEM`) and **URL-normalization dedup** (`_norm_url` wired into all 4 dedup points in `adaptive_worker.py`). Both verified correct at the code level.

**Re-ran the Lowry Mays prospect query → it REGRESSED to 0/15 supported, all Kyle Lowry sources.** But the audit shows *why*, and it's not the fixes:
- The entity-anchoring fix **worked** — the planner generated textbook disambiguation queries: `"Lowry Mays" "Mays Business School" -Kyle -NBA -basketball -player`, quoted names, negative keywords.
- The evaluator guard **worked** — it refused to credit the Kyle Lowry pages → 0 support (honest, no fabrication).
- **The binding constraint is SearXNG:** direct testing showed it returns the *identical* 5 Kyle Lowry pages for EVERY query (even `"Lowry Mays" Clear Channel`), because **4 of 5 engines are suspended** — `brave: too many requests`, `duckduckgo: CAPTCHA`, `google: access denied`, `startpage: CAPTCHA` — leaving **only Bing**, which returns the NBA player for "Lowry." Under the 30-search volume of a deep run, the free scraped engines rate-limit/CAPTCHA the SearXNG scraper. The earlier "good" run was partly search luck (more engines responsive then).

**Honest layered-debugging lesson:** auth → search-backend → fetcher → search-*reliability*. Each fix was correct and peeled back to the next real constraint. **Current bottleneck = search retrieval reliability.** SearXNG's scraped engines are unreliable at deep-research query volume.

**Fix options (search reliability):** (1) add a **keyed search provider** — Brave Search API (free 2k/mo), Tavily, or Serper — reliable, not scraped, won't CAPTCHA (tool already has Brave/SerpApi/LangSearch backends; just needs a key); (2) tune SearXNG engines / add a request limiter / cache; (3) accept flakiness. Recommend (1) — a keyed provider is the robust answer for a tool firing 10-40 searches/run. Entity-anchoring + dedup fixes stay regardless (they're correct and necessary).

**Decision (2026-06-03):** going the **no-key SearXNG-side route (2)** for now — throttle the tool's SearXNG request rate (the 30-search burst is what CAPTCHA'd the engines), add retry/backoff, and tune the SearXNG engine set toward less-block-prone engines. Keyed provider remains the robust longer-term answer if throttling isn't enough.

---

## 2026-06-03 - Deep prospect run (post-auth-fix): tool works; real quality levers surfaced

Ran a deep donor-prospect profile (heavy + thorough) on **Lowry Mays** (public TAMU benefactor — chosen deliberately to avoid putting real internal donor data through a demo). ~17 min (1023s, 15 fetch / 10 search / 116 LLM). **Produced a genuine, readable, grounded prospect profile** across career/capacity, A&M giving, affiliations, interests — every prose claim cited, confidence-scored grounding appendix, honest "What Remains Open," and the validator even stripped a hallucinated URL. Real facts captured: Clear Channel founder, $25B sale 2008 w/ McCombs, $15M endowment 1996 → Mays Business School naming, $25M 2017, United Way SA chair.

**But the run exposes the genuine quality levers (now that auth works, these are the REAL issues, not red herrings):**
1. **Entity disambiguation / wrong-person drift** — source list included *Kyle Lowry the NBA player* (wikipedia/espn). A prospect tool must lock onto the right person.
2. **Shallow resolution** — only **2/19 claims supported, 13 open**; many publicly-answerable facts (education, boards, founding) left unresolved; stopped at 15/20 fetches (didn't exhaust budget).
3. **Source dedup broken** — ~10 near-identical `stories.tamu.edu/...graduate-education-building` URL variants wasted fetch budget.
4. **Missed a prospect-critical fact** — Lowry Mays died 2022; report reads present-tense. Real prospect work would pivot to the **Mays Family Foundation** (which the tool glimpsed but didn't elevate).

**These are the legitimate model/loop levers** — a stronger model (Qwen3/Tongyi) would plausibly disambiguate, resolve more claims, and dedup better. The Gemma-vs-Qwen comparison now rests on real evidence. Candidate fixes: entity-anchoring in the planner/decompose prompt; URL-normalization dedup before fetch; recency/biographical claims (incl. "is the subject living?"); push harder before stopping with many open claims.

---

## 2026-06-03 - 🔴 ROOT CAUSE FOUND: tool wasn't authenticating to the cluster (every LLM call 401'd silently)

**This supersedes the "fetcher is the bottleneck" conclusion below.** A confirmation run on a static, Wikipedia-rich query (1900 Galveston hurricane) *also* returned "0 supported, unresolved" in 22s — identical stats to the FEA run, content-independent. The `audit.jsonl` showed **`"parse_failed": true` on every single LLM call** (planner, evaluator, strategist: *"Strategist LLM call failed or returned unparseable output"*). The Wikipedia page extracted fine (4 KB, the `MAX_PAGE_CONTENT_LENGTH` cap), so it wasn't the fetcher either.

**Root cause:** the tool's LLM calls send only `{"Content-Type": "application/json"}` — **no `Authorization` header**. The cluster's LiteLLM router requires a bearer token (its `master_key` is the literal string `"none"`), so **every call returned HTTP 401**, which the code silently catches (`except URLError... return None`) → `parse_failed` everywhere → the loop runs completely blind, attaches no evidence, and gives up in ~22s with an honest "unresolved." Confirmed by a direct urllib call: no header → **401**; with `Authorization: Bearer none` → 200 + clean JSON. It worked historically because LM Studio (the old backend) needs no auth; the moment it was repointed at the cluster's LiteLLM, every call started 401ing — silently.

**Why this fooled us:** the failure mode is invisible — the grounding correctly reports "unresolved" rather than fabricating, so a total auth failure looks identical to "thin evidence / weak model." We chased model → search → fetcher; the real issue was that **the tool was never getting any LLM output at all.**

**Fix:** added `LM_STUDIO_API_KEY` to `config.py` (+ `.env`, default `"none"`) and added `Authorization: Bearer {LM_STUDIO_API_KEY}` to **all 5 LLM call sites** (`adaptive_worker.py` loop caller, `tools.py` ×2 classifier/verdict, `grounding.py`, `learning_store.py`). Agent restarted on `:8765`.

**✅ VERIFIED — night-and-day.** Same Galveston query, post-fix: **3/8 claims supported, 2 partial, 0 refuted, 3 open** (was 0/1), proper 8-sub-claim decomposition (was 1 blob), a coherent **cited report** with verbatim grounded quotes (death toll 6,000–12,000 / 8,000 cited, 8–12 ft surge, Cat-4 145 mph, ~7,000 buildings destroyed, $17–30M damage; sources Wikipedia + Gilder Lehrman) + honest "What Remains Open." 71s real work vs 22s blind-bail. **The tool works.**

**Implication for the whole eval:** every prior "Gemma output is poor" judgment is INVALID — Gemma was never driving the loop. Gemma-4-26B just produced a solid grounded report, so the Qwen/Tongyi model swap is now an *enhancement, not a fix* — the Gemma-vs-Qwen question is genuinely open and should be re-judged on real output. The SearXNG (better sources) and JS-fetcher (institutional sites) findings remain valid as *secondary* improvements, not the primary problem.

---

## 2026-06-03 - Real run on SearXNG reveals the NEXT bottleneck: the page fetcher (JS-rendered pages)

Ran a real medium-depth job through the live tool (now SearXNG + Gemma): *"eligibility requirements + selection process for the TAMU Foundation FEA scholarships."* Finished in **39s** with **0/1 claims supported, 1 unresolved** — and the fetched-text lengths explain why:
- **SearXNG surfaced the CORRECT page** (`txamfoundation.com/...Apply-for-FEA.aspx`) — search is no longer the bottleneck. ✅
- **The page fetcher extracted only ~497 chars from it** (and 456 from another Foundation page). txamfoundation is **JS-rendered ASP.NET**; the tool's static `requests`+BeautifulSoup fetcher can't read it → got boilerplate, not criteria.
- Most "content" fetched was a **Merriam-Webster dictionary page (4058 chars)** — pure noise (the word "eligibility" pulled it; THOROUGH_MODE was off at medium so no pre-fetch relevance gate).
- The loop tried 7×, found no usable content, and **honestly returned "unresolved"** rather than fabricating — grounding working correctly. The model was never given content to reason about.

**Corrected diagnosis:** the binding constraint is now the **page fetcher** (can't handle JS-rendered sites — i.e. most institutional sites incl. our own), NOT the model and NOT search. A Gemma→Qwen swap would NOT have fixed this query. Also: decomposition produced 1 mega-claim instead of sub-claims (model/prompt weakness).

**Reordered levers:** (1) **fetcher: add a JS-capable path** (Playwright/headless or a reader/extraction API) — new top bottleneck; (2) cut SearXNG noise via THOROUGH_MODE pre-fetch gate / engine tuning; (3) model Gemma→Qwen (still worth it, not the cause here); (4) better decomposition. *Lesson: running one real query reordered the whole priority list — search was a red herring relative to extraction.*

---

## 2026-06-03 - Restored Foundation SearXNG + grafted a SearXNG backend into the tool (search-quality fix)

Diagnosed "poor Gemma output" as **two compounding caps**: weak search (tool was on the DuckDuckGo *fallback* — no LangSearch/Brave key) + modest model (Gemma-4-26B). Tackled the search half (cheaper, not Deathstar-gated).

**Infra repair:** Foundation **SearXNG** (Foundation Chat Docker stack, host `:8888` → container `:8080`) had been **down 5 weeks** — exited 4/29 with code 127 because its `settings.yml` bind-mount source went missing in the late-April repo move, so Docker auto-created an empty *directory* there → "mount directory onto file" error. Fixed by restoring the real settings file (from `Documents/Foundation-Chat/configs/searxng-settings.yml`, which has `formats: [html, json]`) at the path the container expects (`~/Foundation AI Projects/Foundation Chat/configs/searxng/settings.yml`) — via a throwaway Docker container since `sudo` needs a password in this env. `restart=unless-stopped`, so it survives reboot. Verified JSON API returns real results.

**Tool change (graft toward the "add Odysseus parts" direction):** the in-house tool had no SearXNG backend (only DDG/Brave/SerpApi/LangSearch). Added `SearXNGBackend` to `tools.py` (`/search?format=json` → title/url/snippet), `SEARXNG_URL` to `config.py`, wired `SEARCH_BACKEND=="searxng"` into `_get_backend()`, set `.env` `SEARCH_BACKEND=searxng` / `SEARXNG_URL=http://localhost:8888`. Agent restarted on `:8765` (pid changes; `nohup` bg, still not a service).

**DDG vs SearXNG comparison (same queries):** easy query ~tie (SearXNG longer snippets but some Wikipedia/Texas.gov noise); **niche query (FEA eligibility) = clear SearXNG win** — surfaced the correct Aggie One Stop + txamfoundation pages DDG missed entirely (DDG returned wrong-campus TAMIU pages). Net: SearXNG = better recall of correct niche sources + reliable at volume (no DDG rate-limiting) + local/air-gapped; generic noise filtered downstream by the tool's source-classifier / THOROUGH_MODE gate. **Search half of "poor quality" materially improved; model half (Gemma→Qwen) still pending Deathstar.**

Follow-ups: optionally tune SearXNG engines (restrict general web, drop dictionary/Wikipedia) and/or enable THOROUGH_MODE; SearXNG settings file sits at an orphaned old repo path — worth relocating with the Foundation Chat stack someday.

---

## 2026-06-03 - Odysseus Engine Extraction: Architecture Exploration

Continued the Odysseus thread toward a concrete posture: **reconstruct/harvest** — lift Odysseus's Deep Research engine out of the workspace and re-host it as a standalone backend behind a **Foundation AI Dashboard UI**, with the model on the **inference cluster**. Holding on test/validation for now.

Reverse-engineered the Odysseus deep-research module from source + mapped the Foundation AI Dashboard. Findings (full writeup → [Odysseus-Engine-Integration-Design.md](Odysseus-Engine-Integration-Design.md)):
- Engine (`DeepResearcher`, `src/deep_research.py`) is **cleanly extractable** — IterResearch loop, ~12 files, light deps; auth/DB/session coupling lives only in the route layer.
- **Model interface is per-call swappable** (raw httpx, endpoint+headers passed in) → points at our LiteLLM cluster proxy with a constructor arg.
- Search is pluggable (SearXNG default — Foundation already runs one; DuckDuckGo needs nothing). State is filesystem JSON. No Chroma/SQL needed.
- **Live cluster state:** proxy `10.2.35.10:4000` serves only `gemma-4-26b-a4b-nvfp4` today (Nano full + shared with HR). Deathstar (384 GB, the Tongyi-30B-A3B home) not yet migrated onto `.35.2x`. → validate on Gemma now, swap to Tongyi later via config.

Open decisions captured in the design doc: front-end shape (native dashboard feature vs standalone path-based tool + tile), reuse-vs-rebuild UI, relationship to Andrew's in-house CrewAI tool, model timing, license hygiene (preserve Tongyi Apache-2.0 attribution).

---

## 2026-06-03 - Booted the Deep Research Agent in Production + Model Roster (RESUME POINT)

**Deployed:** booted the Foundation Deep Research Agent (`Deep-Research-Agent/research-agent`) in production — `python run.py api` on **:8765** (was a `nohup` bg job; **not yet a service — won't survive reboot**), pointed at cluster proxy `10.2.35.10:4000` / `gemma-4-26b-a4b-nvfp4` (Gemma on the Nano), running the **adaptive loop**. Verified root 200 + `/api/discover` online. Fixed `.env` `API_PORT` 8000→8765 (8000 collided w/ R&FI). Reach via `localhost:8765` (VM) or `10.2.35.10:8765` (VLAN). Dashboard tile href is hardcoded `localhost:8765` → only works from a VM browser; path-based NPM wiring is a TODO.

**Confirmed:** the loop architecture is effective — it's a *model* issue, not a loop issue. **Candidate model roster** (drivers: Qwen3-235B-A22B / GLM-4.6 / gpt-oss-120b / Nemotron-Super-49B; judge: R1-Distill-Qwen-32B; cheap legs: Qwen3-30B-A3B / gpt-oss-20b / Jan-nano-4B) + task-routing plan documented in [Odysseus-Engine-Integration-Design.md](Odysseus-Engine-Integration-Design.md) §8–§9.

**Resume next steps** (full list in design doc §9): (1) adaptive-in-UI already satisfied; (2) path-based reachability for the tile [unblocked]; (3) model bake-off on Deathstar [blocked on `.35.2x` migration]; (4) task-routing config; (5) systemd persistence.

---

## 2026-06-03 - In-house Adaptive Loop vs IterResearch: Comparison Result

Grounded the "port IterResearch into the in-house tool" idea by reading `adaptive_worker.py` et al. against Tongyi IterResearch. **Result partly overturns the idea:** the in-house adaptive loop **already implements IterResearch's core innovation** (per-round workspace reconstruction to beat context bloat) — it threads no transcript, rebuilding each LLM prompt from the structured ClaimsModel. The only genuinely additive IterResearch technique is **Heavy mode (parallel rollouts / test-time scaling)** — the loop is currently strictly single-track. The in-house claims-model + post-run grounding validator are *better* suited to Foundation's verifiability needs than IterResearch's evolving-prose-report memory.

**Revised ranked plan:** (1) Tongyi-30B-A3B on Deathstar + repoint tool [blocked on VLAN migration]; (2) wire adaptive loop into the web UI [unblocked, most actionable — tile currently serves the linear pipeline]; (3) optional later: Heavy-mode parallel rollouts. Dropped "port workspace reconstruction" (already solved). Full table → [Odysseus-Engine-Integration-Design.md](Odysseus-Engine-Integration-Design.md) §7.

---

## 2026-06-03 - Current State Assessment: Foundation Deep Research Agent

Inspected the existing in-house tool (repo clone `Foundation AI Projects/Deep-Research-Agent`, branch `dev2`, code in `research-agent/`). **This is the same project as the SB "Deep Research Tool"** (the `research-agent` → `Deep-Research-Tool` rename; the dashboard tile labels it "Deep Research Agent", port 8765). Reconciling the naming matters for the Odysseus decision — *we already have a cluster-integrated deep research tool.*

**Is it functional?**
- **Code-complete and last-known-good** — adaptive claims-model loop (`adaptive_worker/planner/evaluator`, `claims.py`), linear CrewAI pipeline, depth presets (light/medium/heavy/ultra), grounding validator, MCP server, FastAPI web UI. Last successful report on disk: 2026-04-29 ("Andrew Howerton…"). `jobs/` empty.
- **Not currently running** — nothing listening on 8765 (or its configured port). It's stopped, not broken.
- **Caveat (from `BRANCH_NOTES.md`):** API/web-UI mode (`run.py api`) routes to the **linear pipeline only**; the better **adaptive loop is CLI-primary, not yet wired into the API/UI**. Grounding pass also not yet wired into the adaptive loop. So the dashboard tile today would run the older pipeline.
- *Not launched to confirm live (Dominic holding on validation).*

**Already wired to the cluster.** `.env` points at the cluster proxy: `LM_STUDIO_BASE_URL=http://10.2.35.10:4000/v1`, `LM_STUDIO_MODEL=gemma-4-26b-a4b-nvfp4`, plus `INFERENCE_HOSTS=10.2.35.10`, `CONTEXT_LIMIT_TOKENS=256000`. Search backend = DuckDuckGo.

**Multiple models based on hardware capability?**
- **Multi-endpoint/model discovery: YES.** `/api/discover` (`api_server.py:~2264-2506`) probes localhost + machine IPs + the configured base-URL host + `INFERENCE_HOSTS`, across a port map that explicitly includes LM Studio (1234), the **LiteLLM cluster router (4000)**, Ollama (11434), and vLLM instances labeled **"Nano" (8020), "Super A/B" (8003/8004), and "Death Star" (8021/8022)**. Walks an auth chain (`None`/`"none"`/`"lm-studio"`), reads `/v1/models` or `/api/tags`, and surfaces discovered models for selection in Settings.
- **Hardware-capability-based auto-selection: NO.** It discovers and lets you *manually pick* an endpoint/model; it does **not** profile GPU/VRAM and auto-choose a model to fit. Hardware-fit is handled upstream at the cluster layer (vLLM/LiteLLM + the inference-cluster dashboard) — and is conceptually where Odysseus's "Cookbook" feature would map if we ever wanted that.

**Strategic implication for the Odysseus decision:** the in-house agent is more mature and more cluster-integrated than the 6/2 framing assumed — it already discovers cluster models (incl. Death Star) and runs on `gemma-4-26b`. This strengthens the **"harvest Tongyi/IterResearch ideas into the existing tool"** option relative to extracting Odysseus wholesale. Decision #2 (replace / run-both / harvest) should be revisited with this in mind — see [Odysseus-Engine-Integration-Design.md](Odysseus-Engine-Integration-Design.md) §5.

---

## 2026-06-02 - Odysseus Evaluation: Candidate Alternative / Complement Harness

**Context:** Dferrara + Andrew are evaluating **Odysseus** as a possible deep-research harness to run locally — specifically the idea of **lifting just its Deep Research module for our own purposes**, alongside (or as an alternative to) the in-house CrewAI tool. Research only at this stage — **nothing installed.**

### What Odysseus is
- PewDiePie's self-hosted, open-source (MIT) AI workspace — `github.com/pewdiepie-archdaemon/odysseus`. Launched **2026-05-31** (≈2 days old at time of writing; 30k+ GitHub stars in 2 days). Whole-workspace scope: chat, autonomous agents (built on opencode + MCP), email/calendar, notes, image gen, a hardware-aware "Cookbook" model installer, and **Deep Research**.
- **Deep Research module is adapted from Alibaba's Tongyi DeepResearch** (Apache-2.0) — lives in `api/research_*.py`, `routes/research_routes.py`, `services/search/`. Multi-step gather → read → synthesize into a cited visual report. Native model is **Tongyi-DeepResearch-30B-A3B**: ~30.5B-param MoE (3B active), 128K context, Qwen3-30B-A3B base, ReAct + "Heavy" IterResearch (test-time scaling) modes. Available on OpenRouter; weights on HF.
- **Backends:** vLLM · llama.cpp · Ollama · OpenAI · OpenRouter. **Stack (Docker Compose):** app on :7000, bundles **SearXNG** (search), **ChromaDB** (vectors), ntfy. Binds `127.0.0.1` by default; `APP_BIND=0.0.0.0` for reverse-proxy exposure. Python 3.11+, `tmux` for Cookbook downloads.

### Why this is interesting for us
- **It's the same problem Andrew's tool solves**, but with a different lineage (Tongyi's purpose-trained agentic research model + IterResearch test-time scaling) vs. our CrewAI adaptive claims-model loop. Worth benchmarking head-to-head and mining for ideas regardless of whether we adopt it.
- Three postures to decide between: **(a) adopt** Odysseus's Deep Research as the engine; **(b) complement** — run both, route by use case; **(c) harvest** — study Tongyi/IterResearch and the SearXNG+Chroma plumbing, fold the good parts into our own tool. The leaner integration target if we only want the engine is upstream **Tongyi DeepResearch** (`github.com/Alibaba-NLP/DeepResearch`) rather than the whole workspace.

### Hardware fit — Deathstar makes local trivial
- The native 30B-A3B model needs ~24 GB VRAM at Q4 / ~60 GB at FP16. **Deathstar** (4× RTX PRO 6000 Blackwell, 96 GB each = **384 GB total**, the in-house tool's stated production target, see [[AI Distributed Inference Cluster]]) runs it in **full FP16 on a single card** with ~36 GB left for 128K-context KV cache — no quantization, max quality. Four cards → dedicate-by-role (research model + chat + embeddings + spare) via vLLM, exactly how our cluster already serves models.
- **aivm itself is CPU-only** (VMware SVGA, 23 GB RAM) — it cannot run the model locally. So while Deathstar is mid-migration onto the `.35.2x` AI VLAN, an **interim eval** can self-host the Odysseus workspace + SearXNG on aivm but drive the loop with a remote model (Tongyi-30B-A3B via OpenRouter) to judge the harness independent of hardware. **Note:** this breaks the project's air-gapped / local-AI-only rule (see Project-Instructions) — interim only, research content would leave the box; true eval runs fully-local on Deathstar.

### Deployment shape (when greenlit)
- Docker Compose on the host, `APP_BIND=0.0.0.0`, reverse-proxied as a path-based tool: `aisandbox.txamfoundation.com/odysseus` (mirrors the K-1 / cluster path-based pattern). Model calls point at the cluster's vLLM/LiteLLM endpoint on Deathstar.

### Status
Research only — nothing installed, awaiting go-ahead. Built-in Claude `deep-research` skill noted as a benchmark baseline.

**Sources:** [Odysseus repo](https://github.com/pewdiepie-archdaemon/odysseus) · [Tongyi DeepResearch](https://github.com/Alibaba-NLP/DeepResearch) · [Tongyi-30B-A3B (HF)](https://huggingface.co/Alibaba-NLP/Tongyi-DeepResearch-30B-A3B) · [on OpenRouter](https://openrouter.ai/alibaba/tongyi-deepresearch-30b-a3b)

---

## 2026-04-28 - Cross-Device Push Landed: `dev2` Branch + Substantial Work

🎉 **The cross-device push the project has been waiting for landed today.** A new `dev2` branch appeared on origin with 9 substantial commits - feature work, doc updates, plus an SB project docs push from the other device.

### Commits on `origin/dev2` (other device → GitHub today)
- `b836a99` **docs(dev2): Add Second Brain project docs and ROADMAP** - the other device pushed *its own* SB project docs. Reconciliation needed against this device's local SB folder (`0. Active Priority/Deep Research Tool/`).
- `a82e8d4` fix(dev2): prose scaffolding leak, takeaway notes, budget bump, clarifications wiring
- `3711a7d` feat(dev2): evaluator quality, sidebar repopulation, prose synthesizer
- `5625cdd` feat(dev2): strategist turn - meta-loop reflection, corroboration awareness, persistence
- `3104e97` docs(dev2): roadmap entries for active-personality indicator + adaptive clarifications
- `e16ac1a` fix(dev2): per-update source_url, placeholder-quote filter, run delete, home button
- `48b594c` feat(dev2): make adaptive the only mode; defer universal-surface polish
- `9ae0102` feat(dev2): adaptive claims-model research loop
- `11b5558` feat(dev2-baseline): grounding validator, depth presets, pipeline hardening

### Reconciliation Pending - Tomorrow Morning's Work
- [ ] **Pull `origin/dev2` locally** and inspect the docs commit (`b836a99`) - what does the other device's SB scaffold look like vs. this device's?
- [ ] **Merge SB docs** - likely the other device's are more current; figure out which Overview / Roadmap / Notes / Project-Instructions to keep, and what to integrate
- [ ] **Reconcile this device's local SB** with the merged content; sync `docs/` back into the repo via the protocol
- [ ] **Reconfirm the rename decision** - `research-agent` orphan vs. `Deep-Research-Tool`. The dev2 branch suggests serious work; we need a single canonical name
- [ ] **Decide branch posture going forward** - is `dev2` the new active, or was it a one-off branch to be merged into `dev` / `main`?

This is the project that got flagged "on hold pending cross-device push" through this whole modernization sweep. Now the push is here; reconciliation is tomorrow's first repo task.

---

## 2026-04-27 - Initial SB↔Repo Sync + Modernization

**Context:** SB project was frozen at 2026-03-24 (first successful test). Repo has undergone two major evolution phases since then. This sync catches up ~5 weeks of development.

### Repo renamed
- Was `research-agent` → now `Deep-Research-Tool` on GitHub
- Local path remains: `~/Documents/VS Code Projects/Deep Research Tool/Deep-Research-Tool/`

### Branch state
- `main` - linear pipeline, stable, 5 commits (Apr 14–17)
- `dev2` - adaptive claims-model loop, active, 8 commits (Apr 24–27). This is the future.
- `dev` - exists on remote but no local branch; appears to be an intermediate step before dev2

### What shipped on main since last SB update (Mar 24 → Apr 17)

| Date | Commit | Summary |
|------|--------|---------|
| Apr 14 | `4fbbf0c` | Initial commit - Deep Research Tool (repo recreated/restructured) |
| Apr 15 | `422f0c2` | Dynamic clarifying questions, UYBJ button, Live Plan Evolution, Research Notebook |
| Apr 15 | `2c25dbd` | 4-stage pipeline: Gap Analyst + ThoughtNodeTool + reasoning trail |
| Apr 15 | `21fa5af` | Gap loop refactor - analyst identifies gaps, researcher fills, loops until satisfied |
| Apr 17 | `2ecc5ec` | Resume fix, mind map PDF, dashboard, gap-fill branch, PDF fetch, notes gate filter |

**Key evolution:** Pipeline went from 3-stage to 4-stage with the addition of Gap Analyst. Dynamic clarifying questions + "Use Your Best Judgment" button added for research scoping. Mind map PDF export shipped.

### What shipped on dev2 (Apr 24 → Apr 27)

| Date | Commit | Summary |
|------|--------|---------|
| Apr 24 | `11b5558` | Grounding validator, depth presets, pipeline hardening |
| Apr 24 | `9ae0102` | Adaptive claims-model research loop - core paradigm shift |
| Apr 26 | `48b594c` | Made adaptive the only mode; deferred universal-surface polish |
| Apr 27 | `e16ac1a` | Per-update source_url, placeholder-quote filter, run delete, home button |
| Apr 27 | `3104e97` | Roadmap entries for active-personality indicator + adaptive clarifications |
| Apr 27 | `5625cdd` | Strategist turn - meta-loop reflection, corroboration awareness, persistence |
| Apr 27 | `3711a7d` | Evaluator quality, sidebar repopulation, prose synthesizer |
| Apr 27 | `a82e8d4` | Prose scaffolding leak, takeaway notes, budget bump, clarifications wiring |

**Key paradigm shift:** Moved from fixed 4-stage pipeline to budget-driven adaptive loop. System decomposes queries into verifiable claims, plans next-best action based on what it doesn't know, evaluates evidence with confidence deltas, and stops when claims are sufficiently supported or budget is exhausted. Honest about what it couldn't resolve - "unresolved" instead of fabricated prose.

### Known issues at sync
- `learning_store.json` is untracked (gitignored runtime artifact)
- Grounding pass not yet wired into adaptive loop
- API/UI routing still on linear pipeline; adaptive is CLI-primary
- No `docs/` folder in repo yet

---

## 2026-03-24 - Major Architecture Rebuild + First Successful Test

**Architecture overhauled - Ollama/SearXNG/Streamlit replaced entirely:**
- LM Studio replaces Ollama as local LLM backend
- LangSearch replaces SearXNG for web search (AI-generated summaries)
- CrewAI replaces custom agentic loop (3-agent pipeline)
- FastMCP replaces direct tool calls - MCP server for LM Studio / Claude Desktop
- FastAPI + vanilla JS web UI replaces Streamlit

**Key engineering problems solved:**
- LM Studio kills MCP connections after ~2 min → detached subprocess worker
- CrewAI defaulted to ReAct text generation (90+ min) → native function calling via litellm (~4 min)
- LangSearch rate-limited → threading.Lock with 1s delay
- Zombie jobs → atexit + SIGTERM handlers

**First successful test:** Query about Andrew Howerton completed in ~4 min with accurate results.

---

## 2026-03-19 - Roundtable Presentation

Deep Research Tool included in AI roundtable deck for Chris and leadership. Concept well received.

---

## 2026-03-18 - Framework Built & Pushed to GitHub

Original framework: Ollama + SearXNG + Streamlit. Agentic research loop: planner → search → extract → synthesize.

---
