# Research Agent — Roadmap

Features and improvements tracked here. Items are grouped by theme.
Status: `[ ]` planned · `[~]` in progress · `[x]` shipped

---

## Adaptive-Loop Architecture (dev2 branch)

The `dev2` branch pivots the research engine to a claims-model-driven
adaptive loop (see `BRANCH_NOTES.md`). The items below track the work
needed for adaptive to become a first-class, universally-accessible
mode alongside or in place of the linear pipeline. They should be
treated as a coherent phase rather than individual tickets.

### Universal Surface (API / UI / MCP parity)
- `[ ]` **Adaptive as the primary (or only) mode across every caller surface** — the same adaptive-loop run is reachable identically whether the caller is a human (UI), a REST client, or an MCP-using agent. No "it works via CLI but not via the API" gaps
  - `[ ]` **`research_worker.main()` dispatches on job mode** (or, on dev2, simply runs adaptive) so POST /api/jobs and MCP `start_research` both trigger the adaptive loop with identical semantics and stream output
  - `[ ]` **`save_run` picks up `claims.json` alongside `grounding.json` / `fetched.jsonl`** so the artifacts directory is mode-agnostic — consumers can request any JSON artifact without branching on mode
  - `[ ]` **MCP `start_research` gains an optional `mode` parameter** (default: whatever this branch decides is default) so agents can explicitly select the adaptive path by name
  - `[ ]` **REST `JobCreateRequest` gains an optional `mode` field** for agent/REST-client parity with MCP
  - `[ ]` **`GET /api/reports/{file}/claims`** — fetch the claims.json artifact for a completed adaptive run; same shape as the `claims_snapshot` stream payload so downstream consumers can use the same parser live or post-hoc

### Live Claims-Board UI
- `[ ]` **Live claims-board panel** rendered from `claims_snapshot` and `claims_update` stream events — replaces (or complements) the pipeline's Plan / Notes tabs on adaptive runs
  - Each claim renders as a card: claim text, status badge (unknown/investigating/supported/partial/refuted/abandoned), confidence bar 0-1, count of supporting + contradicting evidence with the most recent quote snippet expandable
  - Sorted by priority × (1 − confidence) so the most consequential open claims sit at the top while they're active
  - Status transitions animate subtly — a partial → supported promotion should visibly "lock in", so the user watching the feed sees the agent closing questions in real time
  - New claims raised mid-run fade in with a small "+ new thread" badge
  - Budget counter in the panel header: `fetches 3/10 · searches 2/10 · elapsed 1:24 / 15:00` so users see runway at a glance
- `[ ]` **Report header confidence + corruption badges** applied to adaptive reports identically to pipeline reports, reusing the grounding-pass output

### MCP parity with UI
- `[ ]` **MCP returns structured claims data, not just the report markdown** — add a `get_research_claims(job_id)` MCP tool that returns the parsed claims.json, so an agent consuming the result can reason about individual claim confidences rather than scraping them out of prose
- `[ ]` **Tool docstrings describe the adaptive flow honestly** — agents should understand they're invoking an LLM loop with a budget, not a deterministic pipeline; update the MCP tool descriptions accordingly

### Loop Discipline — Investigate Initial Claims Before Chasing Sub-claims
- `[ ]` **The strategist and evaluator can raise new claims mid-run, which is good — but the loop sometimes chases the new claims and lets initial claims expire with 0 attempts** — observed on the third Andrew Howerton run, where the strategist raised "subject is in TAMF annual reports", "subject's LinkedIn has tenure dates", etc. as new claims, then budget ran out before they (or some of the *original* claims) were ever investigated. Result: claims listed as "open — still unresolved" with "0× tried"
  - **Hypothesis:** the planner ranks by `priority * (1 - confidence)` style heuristic, and a freshly-raised claim with priority 0.6 outranks an initial claim at priority 0.6 with 1 attempt and confidence 0.5, even though the initial claim is closer to closing
  - **Proposed change:** the planner's claim-ranking should weight **`attempts == 0` PLUS membership in the original decomposition** more heavily than newly-raised claims of similar priority. The intuition: the user asked about A, B, C. Don't pivot to investigating D, E, F until A, B, C have at least one fetch each
  - **Implementation sketch:** add an `is_initial: bool` field to `Claim` (set True at decomposition, False for evaluator/strategist-raised claims). `highest_priority_open()` boost initial claims with 0 attempts to the top
  - **Constraint:** newly-raised claims that *narrow* an initial claim (e.g. "BS 1988 from TAMU" raised after "education" was already partial) should still get airtime — possibly via an explicit `parent_claim_id` link that lets the planner treat them as part of the same investigation slot

### Evaluator Quality — Subject Focus + Tension vs. Refutation
- `[~]` **Two refinements to the evaluator's claim-update logic** surfaced by the third Andrew Howerton run, where the loop succeeded broadly but produced two specific quality glitches:
  - **Subject focus on new_claims.** The evaluator currently raises ANY name-like / fact-like string in fetched content as a candidate claim. On a fetched army.mil article about the subject, this raised four open claims about other officers mentioned in the same piece (Warrant Officer Beeson, Spc Minter, etc.). Add an explicit rule to the evaluator: *new_claims must be ABOUT the research subject, not about people, places, or events that merely co-occur in the same source. If you find a tangential fact, ignore it.* Optionally pass the original query/subject string to the evaluator's prompt so it can self-check
  - **Tension vs. refutation.** The evaluator currently treats any mismatch between two sources as a contradiction, marking the claim REFUTED. On Howerton, two sources listed his platoon-leader posting in different companies of the same battalion (A Co vs B Co) — both could be true at different points in his career. Add a third state: when sources disagree, prefer **`tension`** (a new ClaimStatus, or a soft signal stored alongside the claim) over `refuted`. Refutation should require an authoritative *negation* — "the subject is NOT X" — not just two different attestations of related facts

### Sidebar Repopulation — Plan / Notes / Thoughts driven by the adaptive loop
- `[~]` **The Plan / Notes / Draft / Thoughts panels in the workspace are empty during adaptive runs** because they were wired to the linear pipeline's UpdatePlanTool / AddNoteTool / UpdateDraftTool / ThoughtNodeTool — none of which the adaptive loop uses. The panels are valuable real-estate; repurpose them to surface the adaptive loop's internal state so users see how the agent is working, not just the final result
  - **Plan panel:** render the live claims model as a markdown checklist — each claim as `- [x]` (supported / refuted / abandoned) or `- [ ]` (open / partial / investigating), with confidence and attempt count beside it. Re-emit on every claims_snapshot / claims_update / strategist turn so the panel updates live
  - **Notes panel:** render each piece of Evidence as a note with the quote + source URL + which claim it backs. The Notes panel becomes the live citation log the user can audit during the run
  - **Thoughts panel:** every strategist diagnosis becomes a thought node, plus optionally each tactical planner's action rationale. The panel becomes the agent's reasoning trail — not just narrating its mechanics but its meta-strategy
  - **Draft panel:** initially defer; once the prose synthesizer ships (below) the Draft panel can render the in-progress prose as the synthesizer builds it, or update once at end-of-run

### Prose Synthesizer — Narrative report grounded in the claims model
- `[~]` **Today's deterministic synthesizer reads like an audit log, not a research brief** — bullet-listed sections of "Supported / Partial / Refuted / Open" with raw quotes. The architectural promise (every URL is a real fetch, every quote is verbatim) is preserved, but the prose value the pipeline used to produce is gone
  - Add an **LLM polish pass** that runs after the claims model is finalized and before the deterministic dump. Inputs: the supported + partial claims with their evidence (quote + URL per claim), the query, and any user clarifications
  - Prompt steers toward: *"Write a flowing biographical / topical narrative that incorporates these established facts. Cite every factual claim inline. Use ONLY the URLs in the supplied evidence — do not invent or recall any source. Write in connected paragraphs organised around the subject, not around the agent's investigation. Hedge appropriately for partial claims."*
  - Output structure: prose narrative as the body of the report; **append the existing deterministic structure as a "Verification Appendix"** so the audit trail and confidence breakdown are still one click away. This gives readers both the readable brief and the receipts
  - Hard rule: every URL in the prose must appear in the evidence set. Run a post-pass that strips any URL not present, mirroring the ghost-citation guard from the pipeline grounding pass
  - Failure mode: if the prose pass fails or produces nothing, fall back to today's deterministic synthesizer output as the body — never crash the run on a failed polish pass

### Strategist Turn — Meta-loop reflection (tactical → strategic)
- `[~]` **The adaptive loop today picks individual actions well but never reflects on its own strategy** — when tactical moves stop producing claim updates, the loop just stops. It needs a periodic *strategist* turn that revises the plan, identifies stuck claims, and pivots strategy instead of bailing on the run
  - **Trigger:** runs (a) when the tactical loop would otherwise have stopped on stagnation, and (b) every N tactical loops (configurable, default ~5) regardless of progress, so re-planning isn't only reactive
  - **Inputs:** the live ClaimsModel, recent action log, surfaced-URL queue, remaining budget
  - **Outputs (a single LLM call):**
    - `diagnosis` — short text on why progress has stalled (single-source-only, dead-end claim, subject's profile is sparse, all aggregator-derived evidence, etc.)
    - `priority_updates` — raise/lower individual claim priorities based on what's now most consequential or most reachable
    - `abandon` — claim ids to mark ABANDONED when they're clearly dead-ends within the remaining budget (saves wasted tactical attempts)
    - `new_claims` — narrower or alternative claims that the current set is missing (e.g. "subject has a personal website" / "subject lists a phone in a specific area code")
    - `next_action` — a recommended search or fetch that explicitly breaks the stalemate (different angle, different source category)
  - **Corroboration awareness on the tactical Planner:** even outside strategist turns, the tactical Planner gets a new rule — *"if the target claim is PARTIAL with only ONE supporting source, your next action MUST seek corroboration from a different source category; don't repeat the same search and don't fetch another aggregator if the existing source is already one."* This addresses the specific PARTIAL-stuck-at-0.50 pattern observed on Andrew Howerton and Stephen Guetersloh runs
  - **Persistence:** stop-on-stagnation is removed in favour of "trigger strategist". Hard budget caps (max_fetches / max_wallclock / max_loop_iterations) remain the only stopping conditions, plus a small safety: stop if two consecutive strategist turns produce zero changes (the LLM has nothing left to suggest)
  - **Cost ceiling:** strategist calls count against `max_llm_calls` like everything else; a per-run cap (default ~5 strategist turns) prevents runaway re-planning
  - Works as designed when: a run that would have stopped at 5 partial / 0 supported instead persists, runs a strategist turn, gets a corroboration probe against a different source type, and promotes 2-3 of the partials to SUPPORTED before budget exhaustion

### Active-Personality Indicator (workspace status bars)
- `[ ]` **The pipeline-bar / status-bar UI shows which personality is currently running** — today the bar still reflects the linear-pipeline stages (Stage 1/2/3/4) which don't apply to the adaptive loop, so it's stuck at "idle" the entire run
  - Replace the 4-stage progress bar with a 3-role indicator: **Planner** (decomposing / picking next action), **Researcher** (executing search or fetch), **Evaluator** (integrating result into claims). The active role pulses; the others sit muted
  - Driven by the existing stream events: `Planner` log entries, `Researcher` log entries, `Evaluator` log entries already include the agent role — the UI just needs to read them and update the active indicator
  - Also surface a **loop counter** (e.g. "loop 3 of 40") and the running budget readout (`fetches 2/10 · elapsed 1:24 / 15:00`) so the user has a sense of progress and runway
  - Stretch: show the latest action's claim_id + truncated text under the indicator so the user can see WHICH claim the loop is currently working on

### Dynamic Clarifying Questions on the adaptive path
- `[ ]` **Re-wire the LLM-generated clarifying questions feature for the adaptive loop** — the original feature shipped on main (see "Dynamic Clarifying Questions (LLM-Generated, Prompt-Aware)" above) but the dev2 adaptive worker doesn't currently consume the clarifications field, so user answers are silently dropped
  - Confirm the existing `POST /api/clarify` endpoint still produces good per-query questions for the adaptive flow (it should — it's just an LLM call on the query string, no pipeline assumptions)
  - When the user submits clarifications, pass them as additional context to the **decomposition step** in the adaptive loop, not to the per-stage agent prompts (which no longer exist on dev2). The clarifications should sharpen the *initial claims* — e.g. "focus on professional, not personal background" should reduce the priority of personal-life claims in the decomposition
  - Optionally: clarifications also flow into the **planner's system prompt** as ongoing constraints — "the user has indicated X is the priority" — so action selection stays aligned through the loop, not just at decomposition time
  - Keep the < 2s loading skeleton + static fallback from the original implementation

### Adaptive-loop quality polish (pre-universal-surface)
Surfaced by the first two adaptive test runs on Stephen Guetersloh:
- `[ ]` **Per-update source URLs in the evaluator output** — when the evaluator extracts a quote from a search-result list, it must attach the URL of the specific result the quote came from, not the (empty) action-level URL. Citations currently render as `— ` (empty) for search-derived support
- `[ ]` **Reject placeholder quotes** — treat empty strings, "None", "null", and quotes shorter than 10 characters as invalid evidence and skip them; prevents false refutations like the "faculty position at A&M" → REFUTED-on-"None"-quote incident
- `[ ]` **URL normalisation for fetch dedup** — strip trailing slashes and lowercase host before storing in `fetched_urls`; `linkedin.com/in/x/` and `linkedin.com/in/x` should count as the same URL
- `[ ]` **Back-matching pass for newly-raised claims** — when the evaluator raises a new narrower claim mid-run (e.g. "BS 1988" raised after a page mentioned it), re-check that same page's content against the new claim immediately so the evidence isn't lost
- `[ ]` **Gated/403 fetch detection feeds back to the planner** — when a fetch returns a login-wall or 403 response, emit a signal so the planner doesn't keep suggesting variants of the same URL (LinkedIn variants were fetched twice because of this)

---

## Pre-Run

### Pre-Run Clarifying Questions (shipped ✅)
- `[x]` Before a research job starts, present the user with **4 diverse clarifying questions** generated by the LLM (or heuristics) to sharpen the research brief
  - Questions should cover: scope (how broad/narrow?), purpose (who will read this?), recency (how current must sources be?), and depth (executive summary vs. deep dive?)
  - Questions appear in the UI as a "Research Brief" step between query entry and job start
  - Answers are injected into the task prompts so all three agents have fuller context
  - The "Start Research" button becomes "Review Brief → Start" to make the step feel intentional

### Dynamic Clarifying Questions (LLM-Generated, Prompt-Aware) (shipped ✅)
- `[x]` **Replace the four static questions with questions the model generates live from the actual research prompt** — the current questions (Scope, Audience, Recency, Depth) are generic and identical for every query; they don't reflect what matters most for a specific request
  - When the user submits a query, a fast LLM call generates 3–5 questions tailored to that specific topic before the modal opens — e.g. for "Research Chris Speier at the Texas A&M Foundation" the questions might be: "Should I include his role in the 12th Man Foundation separately or treat all A&M entities as one?", "Do you want family/personal background or strictly professional?", "How far back should career history go?", "Any specific achievements or projects to prioritise?"
  - Questions are generated via a short system prompt: *"Given this research request, generate 3–5 clarifying questions that would most sharpen the research. Each question should be specific to this topic, not generic. Return as a JSON array of {question, placeholder} objects."*
  - The modal renders questions dynamically from the LLM response — label and placeholder text both come from the model
  - A loading skeleton shows while questions are being generated (typically < 2s on a fast model)
  - `POST /api/clarify` endpoint accepts `{query}` and returns `{questions: [{question, placeholder}]}`; the frontend calls this when the query is submitted, before showing the modal
  - Questions are generated in parallel with any other pre-flight work so they're ready by the time the modal opens
  - Fallback: if the LLM call fails or times out (>5s), the modal falls back to the current static questions silently

### Depth Presets (Light / Medium / Heavy / Ultra)
- `[ ]` **Replace scattered depth knobs with a single 4-button preset on the query form** — today a run's intensity is governed by `MAX_SEARCH_RESULTS`, `MAX_GAP_PASSES`, and three separate `max_iter` values in `crew.py`; users shouldn't have to tune four numbers
  - Segmented control on the query form: **Light · Medium · Heavy · Ultra** — Medium is the default and matches today's behaviour
  - Each preset is a bundle of knobs: search results per query, researcher/analyst/synthesizer `max_iter`, and gap-analysis passes
  - Suggested mapping (tune after real-run data): Light = 3 / 5 iter / 0 gap passes; Medium = 5 / 10 / 2; Heavy = 10 / 20 / 3; Ultra = 20 / 40 / 5
  - `POST /api/jobs` accepts `depth: "light" | "medium" | "heavy" | "ultra"`; the worker maps it to env vars and agent settings before the crew is constructed
  - Preset label shown in the run header and the exported report so later readers know which tier produced a given report

### Thorough Mode (Read Every Resource, Confirm or Deny Usefulness)
- `[ ]` **Per-run "Thorough" toggle that forces the agent to evaluate every resource it surfaces** — today the researcher can silently skip URLs it doesn't like, producing opaque gaps in the branch tree
  - New toggle on the query form: **Thorough read** (automatically enabled when Ultra preset is selected; otherwise opt-in)
  - When active, `WebSearchTool` no longer returns raw results — for every URL it calls a tight LLM classifier: *"Would this page help answer `<query>`? Answer yes/no in one sentence + one-line reason."*
  - The branch tree receives `resource_verdict` stream events `{url, title, verdict: "useful" | "reject", reason}` — rejected URLs render as dimmed nodes with the rejection reason on hover, so nothing is invisible
  - Researcher is still free to skip a `useful` URL, but has to justify it (reason captured in the reasoning trail); `reject` URLs are hard-blocked from fetch
  - Tradeoff: ~2–5× runtime since every result gets an LLM pass — surfaced in the preset label on the run header

### Second-Pass Gap Verification
- `[ ]` **Optional second full-run pass after the report is drafted** — re-runs the gap-analysis loop against the finished draft to confirm/deny the conclusions and close any remaining gaps
  - New toggle on the query form: **Second pass** (off by default; auto-enabled at Ultra)
  - After synthesis completes, the worker re-invokes the Gap Analyst against the draft + notes + gaps artifacts; newly-identified gaps trigger an additional Researcher → Analyst → Synthesis cycle
  - Distinct from the in-run gap loop (`MAX_GAP_PASSES`) — that runs before synthesis; this runs *after* the draft exists, so it can verify actual conclusions, not just notes
  - The second pass appends — it does not overwrite the original draft; the report header shows "Revised after second-pass verification" and the diff is viewable as an artifact
  - Hard-capped at one second pass per run to prevent runaway loops

### Configuration Page — Custom Research Profiles
- `[ ]` **Settings page for reusable research profiles that pre-configure plan, required sources, and output format** — power users doing the same kind of research repeatedly (donor prospect, academic literature review, competitive analysis) shouldn't have to re-specify everything each time
  - New **Profiles** tab in Settings: list of saved profiles, each with a name, description, and the fields below
  - **Custom research plan template**: a markdown checklist the researcher starts from instead of generating a plan from scratch (e.g. a "Donor Prospect" profile seeds steps like "[ ] Employment history, [ ] Philanthropic record, [ ] Board memberships, [ ] Recent public statements")
  - **Required lookup sites**: a list of domains the researcher MUST search against before finishing (e.g. SEC EDGAR, Guidestar, university gift registries) — enforced as `site:` searches before the run can complete
  - **Output format**: report structure template (sections, required headings, length cap) and optional export-preset defaults (PDF / Markdown / JSON)
  - Query form: a **Profile** dropdown picks from saved profiles; selecting one pre-fills the query form and overrides generated plan/clarifications; "None" preserves today's behaviour
  - Profiles persist in `settings.json` and are shareable as JSON import/export so teams can standardise research processes

### Pipeline Corruption Hardening (shipped ✅)
- `[x]` **Defend against stage-handoff corruption** — the Jennifer Ann Scasta run surfaced a different failure class from the David Riggs one: the Researcher did real work (10 URLs, 12 notes, 6.4KB of fetched text) but its final task-output string collapsed into a token loop (`thess_thess_thess…`). The Critical Analyst received that garbage, correctly refused to analyse, and the cascade continued downstream — producing a "no information available" report despite a full workspace
  - `[x]` **LM Studio decoding-stability params** — `repetition_penalty=1.15`, `frequency_penalty=0.3`, `presence_penalty=0.1` on every LLM call in `research_worker.py._make_llm` and `crew.py._make_llm`. Small local models (Gemma in particular) need this to avoid token-repeat collapse
  - `[x]` **Degenerate-output detector** — `_detect_degenerate_output()` runs on every stage-complete callback and catches three patterns: separator-joined token loops (`foo-foo-foo…`), whitespace-separated word loops (15+ consecutive identical tokens), and low-vocabulary responses (>400 chars but <20 unique tokens with heavy repetition)
  - `[x]` **Pipeline-corruption stream event** — when collapse is detected mid-run, `{"type": "stage_collapse", "agent", "signal", "sample"}` is emitted; the UI renders it as a red banner in the feed
  - `[x]` **Structural corruption signal** — the grounding pass flags "notes-fetched-but-zero-citations" when ≥3 notes exist or ≥2 pages were fetched but the final report contains zero URLs. Catches the Jennifer Ann failure mode even if the runtime detector missed the collapse
  - `[x]` **Corruption hard-caps confidence** — pipeline corruption takes the grounding tier down to LOW regardless of other signals; a score penalty of -3 is also applied
  - `[x]` **ReadWorkspaceTool** — new tool given to Critical Analyst, Gap Analyst, and Report Synthesizer; returns the canonical plan / notes / draft / confirmed-fetched sources directly from the workspace state. Every downstream task description now instructs the agent to call `read_workspace` as its required first action, making the workspace the source of truth rather than the previous task's fragile summary string
  - `[x]` **Downstream task prompts explicitly distrust the prior summary** — each post-Researcher task now contains explicit language telling the agent: "If the prior summary looks garbled or low-information, trust the workspace over it"
  - `[x]` **Unicode-normalised quote matching** — the quote validator now NFKC-normalises and translates smart quotes, em-dashes, and non-breaking spaces before comparison, fixing false-negative mismatches between `’11` (curly) and `'11` (straight) variants observed on the Jennifer Ann Facebook quote
  - Verified against the Jennifer Ann run: new grounding pass would have reported **LOW confidence (score -3), pipeline corrupted: True, signals [token-loop, notes-fetched-but-zero-citations]** — pointing the reader directly at the workspace (notes.md) where the real findings live

### Citation Grounding & Report Integrity Pass (shipped ✅)
- `[x]` **Post-synthesis validator runs against the fetched-URL cache to detect fabricated citations, unsupported claims, and thin-profile subjects** — addresses the failure mode surfaced in the David Riggs apples-to-apples test, where the synthesizer wrote confident fundraising prose citing a URL whose actual content never mentioned the subject
  - `[x]` **Ghost-citation detection** — any URL in the final report that was NOT fetched during the run is flagged inline (⚠ghost-citation) and listed in the Grounding Audit appendix; prevents the model from attaching real-looking URLs it invented
  - `[x]` **URL liveness probing** — HEAD (with GET fallback) every cited URL at publish time; dead/unreachable URLs are flagged in the appendix. Would have caught `txambound.com` in the test case
  - `[x]` **Per-citation LLM grounding** — for each (claim, URL) pair in the draft, an auditor LLM reads the fetched page content and decides whether the page ACTUALLY supports the claim, not just whether it's topically relevant; unsupported citations are called out explicitly in the appendix
  - `[x]` **Thin-profile detection** — counts fetched pages that mention the subject's name tokens; <3 triggers a skepticism warning on the report so readers know the model was working from thin sources
  - `[x]` **Computed (not asserted) confidence tier** — score combines supported-primary-source count, unsupported-citation penalty, ghost-URL penalty (heaviest), dead-URL penalty, and thin-profile penalty; ghost citations or ≥2 unsupported citations hard-cap the tier at "medium"; replaces LLM-authored "High/Moderate" labels with a mechanical readout
  - `[x]` **Quote-anchored citations** — `AddNoteTool` accepts optional `source_url` and `quote` fields; when provided, the validator checks that the quote appears verbatim (after whitespace collapse) in the fetched page body
  - `[x]` **Ghost-citation hard rule in Synthesizer prompt** — the synthesizer is now explicitly instructed that citing non-fetched URLs is a violation checked by the validator; also forbids self-asserted confidence levels
  - `[x]` **Thorough mode extended to fetched pages** — per-page LLM verdict asks *"does this page actually discuss the research subject?"*; off-topic verdicts get a warning footer telling the researcher NOT to write notes against the page. Complements the shipped per-search-result verdict
  - `[x]` **Entity-disambiguation candidates** — regex scan of fetched content flags name-like strings that share tokens with the subject but aren't identical (e.g. Davis Riggs donor vs. David Riggs staff); surfaced in the appendix under "Name collisions"
  - Artifacts: a `grounding.json` file is written alongside `meta.json` for each run; a `fetched.jsonl` cache of full-text fetches is persisted so the run can be re-audited later without re-fetching
  - UI: live stream badges for `resource_verdict`, `page_verdict`, and a summary `grounding` event with the final confidence tier
  - Follow-ups not yet shipped: (1) a UI-surfaced disambiguation modal that lets users *pick* an entity mid-run; (2) a dedicated confidence badge on the report header (currently shown only in the appendix and as a stream event)

### "Use Your Best Judgement" on Clarifying Questions (shipped ✅)
- `[x]` **Add a "Use Your Best Judgement" button to the clarifying questions modal** — lets the user skip answering and trust the model to make sensible decisions for every open question
  - Button appears alongside the existing "Skip" and "Start Research" buttons: `[Use Your Best Judgement]  [Skip]  [Start Research →]`
  - When clicked, a single clarification string is injected into the task prompts: *"The user has not provided specific guidance. Use your best judgement on scope, depth, audience, and recency based on the nature of the query."*
  - This is distinct from "Skip" (which passes empty clarifications) — "Use Your Best Judgement" gives the agents an explicit mandate to self-direct, which produces noticeably more confident and opinionated research
  - The button label in context: clicking it should feel like telling a trusted analyst "just run with it — I trust you"
  - Optionally: after the run, the report header shows a small badge "Agent used own judgement" so the user knows which decisions were autonomous

---

## Live Workspace / UI

### Agent Workspace (shipped ✅)
- `[x]` Real-time stream feed showing token-level reasoning, searches, fetches, agent switches
- `[x]` Plan panel — updates live as researcher calls UpdatePlanTool
- `[x]` Notes panel — accumulates as researcher calls AddNoteTool
- `[x]` Draft panel — updates live as synthesizer calls UpdateDraftTool
- `[x]` Sources panel — tracks all URLs found and read, with category badges
- `[x]` Stage pipeline bar with agent colour-coding
- `[x]` Cancel button, elapsed timer

### Live Plan Evolution (shipped ✅)
- `[x]` **Dynamic plan panel with checkoff tracking** — the Plan tab shows the research plan as a living document, not a static snapshot
  - Each bullet in the plan is rendered as a checklist item; the agent checks items off by calling `update_plan` with `[x]` markers
  - New items added mid-run appear with a subtle "added" highlight; completed items get a strikethrough with a timestamp
  - A plan diff timeline shows each version side-by-side so you can see how the strategy shifted as new evidence was found
  - The plan panel pulses briefly when updated so it's clear something changed without requiring the user to watch it
  - Plan version history saved in the artifact directory alongside the final `plan.md`

### Research Notebook (Notes + Sources unified) (shipped ✅)
- `[x]` **Merge the Notes and Sources panels into a single "Research Notebook"** — modelled on how a researcher builds source material for a paper
  - Each source becomes a **notebook card**: title, URL, source type badge, credibility tier, and the agent's own notes extracted from that page beneath it
  - Cards are added live as pages are fetched and notes are recorded — the notebook grows in real time
  - Each card has a collapsible "Evidence" section showing the key facts the agent pulled from that source, formatted as bullet points
  - Cards are grouped by sub-topic or research angle (derived from the search query that found them), with collapsible group headers
  - A confidence label ([VERIFIED] / [LIKELY] / [CONTESTED] / [UNVERIFIED]) is shown on each card once the analyst stage runs
  - Orphan notes — notes not tied to a specific fetch — appear as standalone "field notes" cards at the top
  - The final Sources list in the report links back to the corresponding notebook card
  - Artifact saved as `notebook.json`: array of `{ url, title, category, tier, queries[], notes[], confidence, confirmed }` objects

### Numbered In-Report Citations (shipped ✅)
- `[x]` **Footnote-style numbered citations** — URLs in the report body are replaced with superscript reference numbers (e.g. `[1]`) that anchor to a formatted References section at the bottom
  - The synthesizer is instructed to emit citations as `[REF:url]` markers inline wherever it references a source
  - Post-processing scans the final report Markdown, collects all `[REF:url]` markers in order of first appearance, assigns sequential numbers, and replaces each with a `<sup><a href="#ref-N" id="cite-N">[N]</a></sup>` anchor
  - The References section is appended automatically as a numbered list: `[N] Title — domain.com` with each entry having `id="ref-N"` so the anchor link jumps the page to it
  - **Hover tooltip**: hovering a citation number for ~600ms shows a floating card with the source title and a clickable URL — implemented with CSS `[data-tooltip]` + a small JS `mouseenter`/`mouseleave` handler; no library required
  - **Click behaviour**: clicking the superscript scrolls to the reference entry at the bottom; clicking the `↩` backlink in the reference list jumps back to the cite location in the text
  - Same treatment applied in the PDF export — superscripts rendered as `(N)` inline and a References appendix added at the end
  - The synthesizer prompt is updated with explicit instructions: "For every factual claim drawn from a source, append `[REF:full_url]` immediately after the sentence."

---

## Run Auditability (shipped ✅)

- `[x]` Per-run artifact directory saved alongside each report: `meta.json`, `audit.jsonl`, `plan.md`, `notes.md`, `draft.md`, `sources.json`
- `[x]` Heuristic evaluator (`evaluator.py`) — scores each run A–F across 10 dimensions with actionable suggestions
- `[x]` Artifact panel in report view: stats strip, grade badge, suggestions, tabbed access to all artifacts
- `[x]` API endpoints: `/meta`, `/evaluate`, `/plan`, `/notes`, `/draft`, `/sources`, `/audit`

### Cross-Run Trends
- `[ ]` **Trends dashboard** — aggregate stats across all runs (average score, fetch count over time, source diversity, tool usage rates)
- `[ ]` **Run comparison** — side-by-side view of two reports: scores, stats, source overlap
- `[ ]` **Auto-improvement suggestions** — after N runs on similar topics, surface patterns ("you consistently under-fetch government sources on policy queries")

---

## Agent Behaviour

### Tool Adherence (shipped ✅)
- `[x]` Post-hoc extraction: after each task, checks the stream file for missed tool calls and calls note/plan/draft tools directly from Python if the LLM skipped them

### "What Am I Missing?" Gap Analysis Phase (shipped ✅)
- `[x]` A **two-part gap loop** runs between Analysis and Synthesis (up to `MAX_GAP_PASSES = 2` iterations):
  - **Gap Analyst** (analytical only — no searches): reads all findings, classifies each gap as RESOLVED / PARTIALLY RESOLVED / STILL OPEN, closes with `STILL OPEN: N`
  - **Research Specialist** (targeted fill): receives the gap list, runs focused searches on STILL OPEN items only, records findings with add_note
  - Loop repeats until N == 0 (all gaps resolved) or max passes reached
  - **Satisfied when:** `STILL OPEN: 0` in the gap analyst's output, or `MAX_GAP_PASSES` exhausted — whichever comes first; remaining open items are acknowledged in the report's Caveats section
  - Saves `gaps.md` artifact (notes from stage 3 passes); viewable in the "Gaps" art-tab

### Agent Rotation (Iterative Collaboration) (shipped ✅)
- `[x]` Implemented as the **Gap Analysis Loop** — analyst findings hand back to the researcher for targeted follow-up, then gap analyst re-evaluates; this is true rotation (Gap Analyst → Researcher → Gap Analyst) within stage 3
  - Pipeline is orchestrated as multiple sequential `Crew` objects rather than a monolithic four-task crew, enabling conditional re-entry
  - Synthesis crew receives the full context of all gap identification and fill tasks regardless of how many passes ran

### Iteration Count & Thread Tracking (shipped ✅)
- `[x]` Track and display the **number of passes each agent has taken** across the full run
  - Stream an `iteration_tick` event at each stage start with `{stage, pass}` fields
  - Track **logical threads** followed: each distinct sub-topic or angle the researcher pursued, labelled and counted
  - Final artifact `threads.json` lists each thread with: label, queries run, URLs fetched, note count, stage
  - Stats strip in the artifact panel shows "Thoughts" and "Gaps Filled" counts from meta.json

### Visual Research Branching Tree (shipped ✅)
- `[x]` **Interactive branching graph** showing how research expanded from the original query
  - Nodes: original query → sub-topics → specific searches → pages fetched
  - Edges: "led to" relationships (search result → fetch → note → claim in report)
  - Rendered as a collapsible tree or force-directed graph in the UI
  - Clicking a node shows the associated note, source, or search query in a side panel
  - Built from the `audit.jsonl` stream file — no extra data collection required
  - Visible as a new "Branch Map" tab in the artifact panel

### Branch Tree "What I Learned" Nodes (shipped ✅)
- `[x]` **Per-node learning annotations on the branch map** — notes attach to the search branch that produced them; thought nodes group searches into reasoning chapters
  - As the researcher fetches and notes pages, each resulting insight is attached as a child "learned" node on the branch that prompted it: e.g. under the "Chris Speier Texas A&M philanthropy" search, a sub-node reads "Learned: Speier emphasizes donor passion as the core of engagement strategy"
  - The researcher task prompt is updated to emit a `note_add` call after each fetch that explicitly names the thread/angle it relates to (e.g. `[Thread: philanthropy] Speier cited in Salesforce story…`)
  - The `note_add` stream event gains an optional `thread` field; `_build_research_tree` attaches note nodes to the specific search branch they belong to rather than just `last_fetch_node`
  - In the interactive D3 tree, learned nodes appear as 📝 cards that expand on hover to show the full note text
  - In the SVG PDF tree, learned nodes render with a soft purple tint and truncated insight text (max 80 chars)
  - The effect: the branch map becomes a research narrative — you can follow any thread and see exactly what each avenue taught the agent

### Source Knowledge Mind Map
- `[ ]` **A live cross-connected graph of every source, search, and claim discovered during a run** — goes beyond the linear branch tree into a true knowledge graph where sources reinforce, contradict, or link to each other
  - **Node types:**
    - 🔵 Search query — each distinct search angle
    - 🟢 URL (stub) — every URL surfaced by any search, even unfetched
    - 🟡 URL (enriched) — URLs that were fetched and read; richer node with content summary
    - 🔴 Claim — a specific factual assertion extracted from enriched notes
    - 🟣 Topic cluster — auto-grouped by keyword overlap across notes
  - **Edge types:**
    - `found via` — search → URL (which query surfaced this source)
    - `corroborates` — URL → claim (when two sources assert the same fact)
    - `contradicts` — URL → claim (conflicting evidence)
    - `links to` — URL → URL (when a fetched page mentions another URL already in the graph)
    - `shares topic` — URL → URL (when two sources appear in searches with overlapping keywords)
  - The data is already there: stub and enriched notes carry `Found in search:` and content; cross-references can be extracted by scanning note content for URLs already in the graph, and by clustering notes with shared keywords
  - Rendered as a **force-directed D3 graph** in a new "Mind Map" tab in the artifact panel — nodes repel, edges pull, clusters form naturally
  - Clicking a node opens its note in a side drawer; clicking an edge explains the relationship
  - Stub nodes (unfetched) appear faded — clicking one offers a "Fetch this source" button to enrich it on demand post-run
  - The mind map persists as `mindmap.json` in the run artifact directory: `{ nodes: [{id, type, url, title, summary}], edges: [{from, to, relation}] }`
  - **Cross-run mind map**: optionally merge mind maps from multiple runs on the same subject, revealing how the knowledge graph grows over successive research passes

### Thought-Process Branch Tree (shipped ✅)
- `[x]` **Branch map reflects the agent's reasoning journey, not just its search mechanics** — nodes narrate the *why* behind each move
  - Agent emits `thought_node` stream events `{ type, id, label, rationale }` via `ThoughtNodeTool` before each new search angle
  - Branch map renders thought nodes (purple, radius 8) as chapter headers; subsequent searches nest beneath them
  - D3 tooltip shows the rationale for thought nodes on hover
  - New "🧠 Thoughts" art-tab lists all thought nodes in order with stage and rationale
  - Artifact saved as `thought_tree.json` alongside other run files

### Live Plan Checkbox Updates
- `[ ]` **Plan panel checks off items in real time as the agent progresses** — the plan is already rendered with `[ ]` / `[x]` markers, but the UI does not visually reflect progress until the user manually refreshes or switches tabs; checkboxes should tick automatically as `update_plan` stream events arrive
  - Stream listener detects an `update_plan` event mid-run and re-renders the Plan tab content immediately — no tab switch or page reload required
  - `[x]` lines render as struck-through checked boxes with a subtle green tint; `[ ]` lines remain as open boxes
  - A brief pulse animation on the Plan tab badge signals that a new checkoff occurred, so the user notices progress without watching the tab constantly
  - Checked items accumulate a completion timestamp shown in muted text to the right: `✓ 14:32`
  - Completion ratio (`n / total`) shown as a small progress bar beneath the Plan tab header

### Plan as Executable Checklist
- `[ ]` **Researcher writes the plan as an actionable checklist and checks items off throughout the run** — the current plan is a free-form text document written once and never updated; it should be a live task list the agent actively maintains
  - Researcher writes plan steps as `- [ ] Do X` items (not prose bullets) — each step should be a discrete, checkable action: "[ ] Search for subject's professional background", "[ ] Find philanthropic history", "[ ] Verify employment details"
  - As each step is completed, the researcher calls `update_plan` with the same content but the relevant `[ ]` replaced by `[x]` — one call per completed step, not a full rewrite
  - The research task prompt is updated with an explicit instruction: *"Treat the plan as a live checklist. After completing each step, call update_plan to mark it `[x]`. Do not wait until the end — check off each item as you go."*
  - The `create_plan` call at the start of a run must produce `- [ ]` prefixed items; if the agent writes prose instead, a post-hoc pass rewrites the bullet list with `[ ]` prefixes before the first `update_plan`
  - The Plan panel already renders `[x]` as strikethrough (Live Plan Evolution shipped) — this item is about enforcing the agent behaviour so checkoffs actually happen, not adding new UI
  - Plan completeness is tracked in `meta.json` as `plan_checked_ratio` (checked items / total items); the evaluator flags runs where this is below 0.5 as "plan not maintained"

### SearXNG Integration (Self-Hosted Search)
- `[ ]` Bundle or auto-launch a **SearXNG** instance as an optional local search backend — zero API keys, zero rate limits, fully private
  - Add `searxng` as a selectable option in the Search Backend dropdown alongside DuckDuckGo / LangSearch / Brave / SerpAPI
  - Settings UI: SearXNG instance URL field (default `http://localhost:8080`) with a status indicator showing whether the instance is reachable
  - `tools.py`: new `SearXNGBackend` class — calls `{base_url}/search?q=…&format=json&categories=general` and maps results to the shared `SearchResult` schema
  - Docker Compose snippet provided in README so users can spin up SearXNG with a single command alongside the research agent
  - Optional: detect a running SearXNG instance automatically during endpoint discovery (same scan that finds LM Studio / Ollama)
  - Optional: settings toggle to let SearXNG aggregate from specific engines (Google, Bing, Brave, etc.) without individual API keys

### Headless Chromium Fetch (4th Fallback Strategy)
- `[ ]` **Add Playwright headless Chromium as a 4th fetch fallback** — triggered only when trafilatura, requests+BeautifulSoup, and the Wayback Machine all return gated or empty content
  - **When it helps:** JavaScript-rendered pages (React/Vue/Angular SPAs), sites that fingerprint for a real browser (Canvas, WebGL), true lazy-loaded content that only appears after scroll or delay
  - **When it won't help:** Hard server-side paywalls (IEEE, ACM, Science) — the browser still hits the same login wall; LinkedIn actively detects and blocks headless browsers
  - Implementation: `pip install playwright && playwright install chromium`; new `_fetch_with_playwright(url, limit)` helper in `tools.py` that launches a headless Chromium page, waits for `networkidle`, scrolls once to trigger lazy loads, then extracts `document.body.innerText`
  - Only invoked as 4th strategy — not on every fetch — to avoid the 5–15 s launch overhead and heavy memory footprint
  - Consider a per-domain allow-list (e.g. `.gov`, `.edu`, conference sites) to limit Playwright to domains where it's likely to add value

### Search Quality
- `[ ]` **Adaptive query expansion** — if initial searches return few results, automatically broaden scope
- `[ ]` **Site-specific follow-up** — when a credible domain is found, run `site:` searches on it for related pages
- `[ ]` **Recency filter** — add date-range parameters to search queries for time-sensitive topics

### Parallel Multi-Tool Search
- `[ ]` **Utilize multiple search backends simultaneously** to increase research speed and source diversity
  - Run DuckDuckGo, Brave, SerpAPI, and LangSearch concurrently for the same query and merge deduplicated results
  - Results from all backends are merged, deduplicated by URL, and ranked by aggregate score
  - Settings toggle: "Parallel search" — when enabled, all configured backends run in parallel; when off, primary backend only
  - **Parallelise agent tasks across AI endpoint threads** — the researcher can dispatch independent sub-searches as concurrent tasks rather than sequential tool calls
  - Pool of worker threads (configurable, default 3) each holds a separate API session; the orchestrator fans out searches and collects results
  - `tools.py`: `ParallelSearchTool` wraps the existing backends and uses `asyncio.gather` or `ThreadPoolExecutor` to run queries simultaneously
  - Visible in the stream as multiple simultaneous `search` events with a "[parallel]" badge; fetch ordering is interleaved as results arrive
  - Stats strip gains a "Parallel Searches" counter

### Past-Run Knowledge Base (Embedded Research / RAG)
- `[ ]` **Index past research artifacts so the agent can retrieve relevant content before hitting the web** — surfaces what we already know before spending search budget re-discovering it
  - After each completed run, index the notes and draft into a local vector or keyword store keyed by run prefix and topic keywords
  - At the start of a new run, query the index with the current research query and surface the top 3–5 most relevant passages as "Prior Knowledge" context injected into the researcher's prompt
  - The researcher is instructed to treat prior knowledge as a starting point: verify currency, fill gaps, and supplement — not blindly repeat
  - **Prior Knowledge panel** — a new workspace tab showing which past-run excerpts were surfaced, with source run links so the user can navigate to the original report
  - Particularly valuable for iterative research on the same subject (e.g. multiple donor prospect runs) — avoids redundant web fetches and builds cumulative depth
  - Storage: lightweight SQLite FTS (full-text search) index over `notes.md` + `draft.md` content from all past runs; no embeddings or GPU required
  - `tools.py`: `PriorKnowledgeTool` — agent can explicitly query the index mid-run for a specific angle, not just at job start
  - Privacy: excluded from indexing when "Don't learn from this run" is checked

### Recursive Learning Module (shipped ✅)
- `[x]` **Backend knowledge store where the AI records insights about the research process** — learns from each run to improve future ones
  - After each completed run, a short reflection pass extracts process-level lessons: "What search strategies worked well?", "Which source types were most reliable?", "What question formulations returned the best results?"
  - Insights are stored in a persistent `learning_store.json` (or SQLite table) keyed by topic domain and source type
  - At the start of each new research run, relevant past insights are injected into the researcher and analyst prompts: e.g. "Previous runs on similar topics found that academic .edu sources were most reliable — prioritise those"
  - **Teach Me / Feedback Session** — after viewing a report, the user can open a "Teach Me" panel: a structured conversation where they correct the agent's understanding, flag weak sources, or redirect emphasis; feedback is written back into the learning store and tagged to the topic
  - **Connected follow-up research** — the Teach Me session can spawn a new targeted research job pre-loaded with the user's corrections and the original context, producing a refined second-pass report
  - Learning store UI: a "Research Memory" settings panel showing accumulated insights, with the ability to edit, tag, or delete entries
  - Privacy toggle: "Don't learn from this run" checkbox on the query form

---

## Settings & Configuration (shipped ✅)

- `[x]` Settings modal with current connection (URL + model)
- `[x]` Auto-discover AI endpoints on the local network (LM Studio, Ollama, etc.)
- `[x]` Click-to-select endpoint + model from discovered results
- `[x]` Search backend selector (DuckDuckGo / LangSearch / Brave / SerpAPI) with API key fields
- `[ ]` **Per-run model selection** — choose a different model for each agent (e.g. fast model for researcher, slower/smarter for synthesizer)
- `[x]` **Research behaviour sliders** — UI controls for `MAX_SEARCH_RESULTS`, `MAX_PAGE_CONTENT_LENGTH`, `CONTEXT_LIMIT_TOKENS`

---

## Export & Polish

### PDF Export Section Selector (shipped ✅)
- `[x]` **Configurable export** — before downloading the PDF, the user picks which sections to include via a multi-select panel; no unnecessary pages for quick one-off exports
  - A modal (or popover) opens when clicking "Export PDF" with a checklist of all available sections:
    - [x] Research Report *(always included, not toggleable)*
    - [ ] Research Plan
    - [ ] Research Notes
    - [ ] Sources
    - [ ] Run Statistics
    - [ ] Branch Map
    - [ ] Evaluator Report *(if a score exists for this run)*
  - Each checkbox shows a page-count estimate next to it (e.g. "Sources — ~3 pages")
  - Defaults stored in `localStorage` so user's typical preferences persist across exports
  - The export request sends the selection as query params: `?sections=plan,notes,sources,stats`
  - `export_report()` in `api_server.py` reads the `sections` param and passes it into `_build_export_html()` as a `set[str]`; each section block is guarded by `if "plan" in sections`
  - A "Quick Export" button (report-only, no artifacts) and a "Full Export" button (everything) appear as presets at the top of the modal

### PDF Cover Page Redesign (shipped ✅)
- `[x]` **AI-generated cover title + polished cover layout** — the current cover just echoes the raw query string verbatim, which reads as a run-on instruction rather than a document title
  - The synthesizer (or a short post-run LLM call) generates a clean, publication-style title from the query — e.g. "Chris Speier: Technology Leadership at the Texas A&M Foundation" instead of the raw query
  - Cover layout is rebuilt as a full-bleed first page: large indigo accent bar at top, title in 28pt bold, subtitle (model, date, word count) in muted type, a thin divider, and a brief one-sentence abstract auto-extracted from the report Summary section
  - A second optional cover element: a "Research Snapshot" bar at the bottom of the cover showing 3-4 key stats (pages read, sources found, run time) as icon+number pairs
  - Cover generation is triggered as a separate step in `_build_export_html` — calls `_generate_cover_title(query, report_md)` which extracts or synthesises the title without an LLM call (regex on the first `## Summary` paragraph + simple heuristics)
  - If a proper title can't be extracted, falls back to a truncated and title-cased version of the query (max 12 words)

### Research Homepage Dashboard
- `[ ]` **A dedicated homepage that surfaces institutional knowledge and past activity** — replaces the blank query box with a knowledge-rich landing page
  - **Lessons Learned panel** — surfaces the top insights from the learning store relevant to recent research, displayed as cards: what worked, what sources proved reliable, what query strategies paid off
  - **Recommended Sites** — a curated, editable list of high-value domains the agent has learned to trust (e.g. SEC EDGAR, university gift registries, Bloomberg, Guidestar); shown as quick-access chips the researcher can reference when planning fetches
  - Sites can be manually added/pinned by the user, or auto-suggested by the learning module when a domain appears reliably across multiple runs
  - **Past Research Runs** — a browseable grid or list of previous jobs with: title, date, score grade, word count, top sources; clicking opens the full report view
  - Filter/search past runs by keyword, date range, or grade
  - A "Continue Research" button on each past run opens a pre-filled query box seeded with the original query and prior context, for iterative follow-up
  - Homepage is the default route (`/`) when no active job is running; transitions to the live workspace when a job starts

---

## Infrastructure

- `[ ]` **Persistent job history** — jobs table (SQLite) instead of ephemeral JSON files, enabling filtering and search across all past jobs
- `[ ]` **Rate limiting per user** — multi-user support with per-IP or per-token limits
- `[ ]` **MCP parity** — expose artifact endpoints via the MCP server so Claude Desktop can retrieve run data
- `[ ]` **Export bundles** — download all run artifacts as a single ZIP file
- `[ ]` **DOCX export** — export the full run (report, plan, notes, sources, stats) as a Word document using `pypandoc` + a local `pandoc` binary; requires `pandoc` to be installed on the host (`apt install pandoc`)

---

## 2026-07-01 — Migrated UI to Next.js (over the FastAPI backend, SSE)
The CrewAI multi-agent backend + SSE streaming are unchanged; the UI moved.
- `frontend/` = Next.js (basePath `/DeepResearch`, port **3015**) replacing the
  3197-line static SPA. Port: query + depth (light/medium/heavy/ultra) + thorough
  + Clarify-first modal; **live SSE stream** rendering all ~20 event types; 4-stage
  pipeline; artifact tabs (Plan/Notes/Draft/Sources/Thoughts/**D3 Mind Map**)
  accumulated from the stream; markdown report (marked); reports history
  (search / PDF+DOCX export / delete); settings modal.
- Backend runs INTERNAL on **127.0.0.1:8765** (routes at root); frontend rewrites
  `/api/*` + `/health` → :8765. SSE streams through the rewrite.
- New `boot.sh` runs both; `proxy_routes` `/DeepResearch` 8765 → 3015,
  **strip_prefix 1 → 0**.
- Fix: `experimental.proxyTimeout` = 1h so the long-lived SSE stream isn't cut at
  the 30s rewrite-proxy default.
- NOTE: live research runs remain gated on the Death Star CUDA blocker (model
  crash-loops); the migrated UI + SSE transport are verified and ready.
Commits: migration `b0da63e`, proxyTimeout `4d34475`.

---

## 2026-07-06 — Simplified run controls (Andrew's pre-use review)
- UI now exposes **only the Depth toggle (Light/Medium/Heavy)**. Removed from the
  frontend: the **Ultra** depth option, the **Thorough mode** checkbox, and the
  **Clarify first** button + modal (`ClarifyModal.tsx` deleted; `clarify()` and the
  `thorough`/`clarifications` fields dropped from the API client).
- Backend unchanged: `/clarify` and the `ultra`/`thorough` params still exist and
  work (run.py CLI can still use them); the UI just never sends them.
- Em-dash pass over all user-facing strings (header blurb, layout metadata,
  feedback toast, live-stream rows, tour copy) per the fleet style rule.

## 2026-07-07 — Degeneration watchdog on prose synthesis
- Dominic caught the browser-path run's "What Remains Open" section devolving
  into a 7.6k-char repetition/word-salad loop (gemma collapsed near the end of
  the 2400-token synthesis; the repetition-penalty params in the payload are
  silently dropped at the LiteLLM hop, so only frequency_penalty survives).
- Fix is deterministic, per the fleet "the cap IS the watchdog" lesson:
  `_trim_degenerate_tail()` in adaptive_worker.py truncates any run longer
  than 400 chars with no sentence terminator or newline at the last healthy
  boundary (a dangling section header gets a stock "could not be completed"
  body). Wired into synthesize_prose after the scaffolding trim.
- Verified: the guard trims the real corrupted report (12.4k -> 4.8k chars,
  saved file repaired) while leaving healthy prose byte-identical, and a fresh
  light-depth live run (federal endowment excise tax post-2025) produced a
  clean 1.1k-word report, longest terminator-free run 225 chars.

## 2026-07-07 — Browser-proxy-path run verified (the 07-06 gap closed)
- Third live run (medium depth, endowment spending-rate + Texas UPMIFA query)
  executed entirely through the browser's transport chain: job created and SSE
  streamed via the Next.js rewrite on :3015 (`/DeepResearch/api/...`), not the
  direct backend. 222 events arrived over ~9 min (530s) and the `done` event
  carried the FULL report payload in `result` — the 07-06 payload-loss bug did
  not reproduce; the self-heal fetch (2e75794) remains as belt-and-braces.
- Report quality spot-checked: ~3k words, real citations (NACUBO FY25 release,
  Texas Property Code ch. 117 statute text, ERIC), grounding audit visibly
  stripping unverifiable URLs. Telemetry: 51 research LLM calls + 1 reflection
  logged to the dashboard under deep-research-agent.
- CLOSED same day: Dominic ran a real browser session at HEAVY depth (first
  heavy-preset run) — the report card rendered on completion and the
  "What Remains Open" section came out as normal prose (degeneration guard
  path healthy at the largest synthesis size yet). Browser path fully
  verified end to end; nothing about the live-run pipeline remains untested.

## 2026-07-06 — First live-run verification + report-display hardening
- **First live run verified end-to-end** (post-CUDA-fix): two real runs on the
  Death Star (117s medium, ~3min light), real web sources fetched, cited reports
  saved and listed. SSE stream captured via curl: all ~20 event types + a `done`
  event carrying the full report payload arrived intact on the direct path.
- **Bug (Andrew's first run):** the browser's `done` event arrived without the
  report payload, so the UI said "report ready" but rendered nothing (report WAS
  saved server-side). Direct-path capture shows backend + Next rewrite are sound;
  the loss was in the browser-side proxy chain or a transient race. Frontend now
  self-heals: on payload-less completion it fetches the newest saved report from
  the store; the Recent-reports sidebar refreshes on every completion; and the
  report card scrolls into view when it renders (it used to appear silently
  below the long live-feed).
