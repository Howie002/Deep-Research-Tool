"""
research_worker.py — Standalone research pipeline runner.

Launched as a detached subprocess by mcp_server.py so it survives
MCP server disconnects. Reads the query from a job file, runs the
full CrewAI pipeline, and writes the result back to the same file.

Progress is logged to the job file in real-time via scratchpad.py so
get_research_result() can show the user what is happening while the
pipeline runs.

Usage (internal — called by mcp_server.py):
    python research_worker.py <job_id> <jobs_dir>
"""
import atexit
import json
import os
import signal
import sys
from pathlib import Path

_job_file: Path | None = None


def _mark_failed(signum=None, frame=None) -> None:
    """Write error status if the process is killed before completing."""
    if _job_file and _job_file.exists():
        try:
            current = json.loads(_job_file.read_text(encoding="utf-8"))
            if current.get("status") == "running":
                current["status"] = "error"
                current["result"] = "Worker process was terminated unexpectedly."
                _job_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    if signum is not None:
        sys.exit(1)


def main() -> None:
    global _job_file

    if len(sys.argv) != 3:
        print("Usage: research_worker.py <job_id> <jobs_dir>", file=sys.stderr)
        sys.exit(1)

    job_id = sys.argv[1]
    jobs_dir = Path(sys.argv[2])
    job_file = jobs_dir / f"{job_id}.json"
    _job_file = job_file

    # Register cleanup handlers so zombie jobs are avoided
    atexit.register(_mark_failed)
    signal.signal(signal.SIGTERM, _mark_failed)

    # Write a startup marker so polling code can detect immediate crashes
    try:
        (jobs_dir / f"{job_id}.started").write_text("ok", encoding="utf-8")
    except Exception:
        pass

    try:
        data = json.loads(job_file.read_text(encoding="utf-8"))
        query = data["query"]
        clarifications = data.get("clarifications", "")
    except Exception as exc:
        job_file.write_text(
            json.dumps({"status": "error", "query": "", "log": [], "result": f"Failed to read job file: {exc}"}),
            encoding="utf-8",
        )
        sys.exit(1)

    from scratchpad import Scratchpad
    import tools as _tools_module
    _scratchpad = Scratchpad(job_id, jobs_dir)
    log = _scratchpad.log
    # Wire tools to the stream file for this job
    _tools_module.set_stream_emitter(_scratchpad.stream_event)

    try:
        log(f"Starting research pipeline for: \"{query}\"")
        log("Stage 1/4 — Research Specialist: gathering sources from multiple angles", agent="Research Specialist")

        def _instrumented_run(q, verbose=False, clarifications=""):
            # Import here to avoid circular issues; stages are logged via crew callbacks below
            from crewai import Crew, Task, Agent, Process, LLM
            from config import LM_STUDIO_BASE_URL, LM_STUDIO_MODEL
            from tools import AddNoteTool, FetchPageTool, ThoughtNodeTool, UpdateDraftTool, UpdatePlanTool, WebSearchTool
            import litellm, re, os

            os.environ.setdefault("OPENAI_API_KEY", "lm-studio-local-no-key-needed")

            # Register the local model so LiteLLM/CrewAI knows it supports
            # native function calling — without this CrewAI falls back to the
            # verbose ReAct text pattern which generates 10K+ tokens per step.
            litellm.register_model({
                f"openai/{LM_STUDIO_MODEL}": {
                    "supports_function_calling": True,
                    "max_tokens": 32768,
                }
            })

            _THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
            _THINK_TAG_RE = re.compile(r"</?think>")
            _OBS_STOP = "\nObservation:"

            def _clean_content(content, had_obs_stop):
                content = _THINK_RE.sub("", content)
                content = _THINK_TAG_RE.sub("", content)
                content = content.strip()
                if had_obs_stop and _OBS_STOP in content:
                    content = content[: content.index(_OBS_STOP)]
                return content.strip()

            def _patch_response(response, had_obs_stop):
                try:
                    for choice in response.choices:
                        if choice.message and choice.message.content is not None:
                            choice.message.content = _clean_content(choice.message.content, had_obs_stop)
                except Exception:
                    pass
                return response

            _orig_completion = litellm.completion
            _orig_acompletion = litellm.acompletion

            def _patched_completion(*args, **kwargs):
                stop = list(kwargs.get("stop") or [])
                had_obs_stop = _OBS_STOP in stop
                if had_obs_stop:
                    kwargs["stop"] = [s for s in stop if s != _OBS_STOP] or None
                return _patch_response(_orig_completion(*args, **kwargs), had_obs_stop)

            async def _patched_acompletion(*args, **kwargs):
                stop = list(kwargs.get("stop") or [])
                had_obs_stop = _OBS_STOP in stop
                if had_obs_stop:
                    kwargs["stop"] = [s for s in stop if s != _OBS_STOP] or None
                return _patch_response(await _orig_acompletion(*args, **kwargs), had_obs_stop)

            litellm.completion = _patched_completion
            litellm.acompletion = _patched_acompletion

            def _make_llm(temperature=0.3):
                return LLM(
                    model=f"openai/{LM_STUDIO_MODEL}",
                    base_url=LM_STUDIO_BASE_URL,
                    api_key="lm-studio-local-no-key-needed",
                    temperature=temperature,
                    timeout=3600,
                    extra_body={"enable_thinking": False},
                )

            llm = _make_llm()
            search_tool   = WebSearchTool()
            fetch_tool    = FetchPageTool()
            plan_tool     = UpdatePlanTool()
            note_tool     = AddNoteTool()
            draft_tool    = UpdateDraftTool()
            thought_tool  = ThoughtNodeTool()

            _current_agent: list[str] = [""]  # mutable container for step_callback closure
            _stream_line_cursor: list[int] = [0]  # tracks stream file position between stages

            # Build optional clarification block injected into all task prompts
            _CLARIF_BLOCK = ""
            if clarifications.strip():
                _CLARIF_BLOCK = (
                    "\n\nUSER CONTEXT — apply these directly to scope and focus your research:\n"
                    + clarifications.strip()
                    + "\n"
                )

            # ── Post-hoc tool enforcement helpers ────────────────────────────

            # Patterns that indicate the LLM echoed task instructions rather
            # than producing actual research content — skip these lines.
            _INSTR_PREFIXES = (
                "you must", "you have to", "step 1", "step 2", "step 3",
                "step 4", "step 5", "step 6", "step 7", "step 8",
                "aim for at least", "at least 6 distinct", "at least one",
                "tools must have been called", "add_note and update_draft",
                "update_draft tool must", "add_note tool must",
                "the add_note", "the update_draft", "the update_plan",
                "call this at the start", "call update_", "call add_note",
                "return a structured list", "return the full", "return the final",
                "return a complete", "return your", "return all",
                "expected_output", "your task is", "your goal is",
                "you are a ", "you excel at", "you are an expert",
                "each finding must", "each claim", "each note must",
                "write the final", "produce a clear", "review the research",
                # gap analyst / gap fill echoes
                "step 1 — review", "step 2 — identify gaps", "step 3 — classify",
                "step 4 — note", "step 5 — output",
                "analytical only", "still open: 0", "targeted gap research",
                "review all research", "identify and fill", "gap analysis report",
            )

            def _is_instruction_line(s: str) -> bool:
                low = s.lower()
                return any(low.startswith(p) or (len(p) > 20 and p in low)
                           for p in _INSTR_PREFIXES)

            def _content_score(text: str) -> int:
                """Heuristic score: higher = more likely to be real research content."""
                score = 0
                score += text.lower().count("http") * 3       # URLs are strong signal
                score += text.count("**") * 1                  # markdown emphasis
                score += len([l for l in text.splitlines()
                               if len(l.strip()) > 60]) // 2   # substantial lines
                score -= sum(1 for p in _INSTR_PREFIXES
                             if p in text.lower()[:400]) * 4   # penalise instructions
                return score

            def _extract_note(text: str) -> str:
                """Extract real research content from task output, filtering instruction echo."""
                if _content_score(text) < 2:
                    return ""  # not enough real content to be worth saving
                useful = []
                for line in text.splitlines():
                    s = line.strip()
                    if not s:
                        continue
                    if s.startswith(("```", "Action:", "Thought:", "Final Answer:",
                                     "Observation:", "Tool:", "Result:")):
                        continue
                    if _is_instruction_line(s):
                        continue
                    if len(s) > 30:
                        useful.append(s)
                    if sum(len(l) for l in useful) > 1200:
                        break
                return "\n".join(useful[:20]) if len(useful) >= 2 else ""

            def _enforce_tool_calls(raw_text: str, stage: int) -> None:
                """If the LLM skipped required tool calls, call them now from Python."""
                stream_file = jobs_dir / f"{job_id}.stream"
                if not stream_file.exists():
                    return
                try:
                    all_lines = stream_file.read_text(encoding="utf-8").splitlines()
                except Exception:
                    return
                task_events = []
                for line in all_lines[_stream_line_cursor[0]:]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        import json as _j
                        task_events.append(_j.loads(line))
                    except Exception:
                        pass
                types_seen = {ev.get("type") for ev in task_events}

                if stage in (1, 2, 3) and "note_add" not in types_seen:
                    content = _extract_note(raw_text)
                    if content:
                        note_tool._run(content=content)
                        log("Auto-extracted note (LLM skipped add_note)", agent=_current_agent[0])

                if stage == 1 and "plan_update" not in types_seen:
                    # Only extract a plan if content looks like real research strategy
                    plan_content = _extract_note(raw_text)
                    if plan_content:
                        plan_tool._run(content=plan_content[:600])
                        log("Auto-extracted plan (LLM skipped update_plan)", agent=_current_agent[0])

                # Only auto-extract draft from synthesizer (stage 4) — earlier stage
                # output is rarely a coherent draft and creates noise pages in exports.
                if stage == 4 and "draft_update" not in types_seen:
                    content = _extract_note(raw_text)
                    if content and len(content) > 150:
                        draft_tool._run(content=content[:3000])
                        log("Auto-extracted draft (LLM skipped update_draft)", agent=_current_agent[0])

                # Re-read cursor after any auto-calls so next stage starts fresh
                try:
                    _stream_line_cursor[0] = len(stream_file.read_text(encoding="utf-8").splitlines())
                except Exception:
                    _stream_line_cursor[0] = len(all_lines)

            researcher = Agent(
                role="Research Specialist",
                goal="Comprehensively gather information about '{query}' by searching the web from multiple angles and reading the most relevant pages.",
                backstory="You are a senior research analyst with a talent for finding the most relevant, credible sources on any topic. You formulate diverse search queries, identify authoritative sources, and extract key facts with their URLs. You never rely on a single source.",
                tools=[search_tool, fetch_tool, plan_tool, note_tool, draft_tool, thought_tool],
                llm=llm, verbose=verbose, allow_delegation=False, max_iter=10, max_rpm=10,
            )
            analyst = Agent(
                role="Critical Analyst & Fact-Checker",
                goal="Scrutinise the research findings for accuracy, consistency, and credibility. Verify key claims with independent searches, prioritising primary and authoritative sources over secondary or user-generated ones.",
                backstory=(
                    "You are a meticulous fact-checker and investigative researcher. "
                    "You evaluate not just what sources say, but how credible they are. "
                    "You understand source hierarchies: primary sources (official statements, "
                    "original research, government records, academic papers) outweigh secondary "
                    "sources (news articles, summaries), which outweigh tertiary or UGC sources "
                    "(blogs, Reddit, social media). You always note source type alongside each "
                    "claim. You classify claims as VERIFIED, LIKELY, CONTESTED, or UNVERIFIED, "
                    "and you flag when a finding rests only on low-credibility sources. "
                    "When [Social/UGC] or [News] sources are the only evidence for a claim, "
                    "you actively search for corroborating [Academic], [Government], or "
                    "[Non-profit/NGO] sources before drawing conclusions."
                ),
                tools=[search_tool, fetch_tool, note_tool, draft_tool],
                llm=llm, verbose=verbose, allow_delegation=False, max_iter=6, max_rpm=10,
            )
            gap_analyst = Agent(
                role="Gap Analyst",
                goal="Critically review all research findings and identify the most important gaps: unanswered questions, unverified claims, missing perspectives.",
                backstory=(
                    "You are a rigorous research editor who specialises in gap analysis. "
                    "You read the researcher's and analyst's findings critically, identify what is "
                    "missing or weak, and produce a clear prioritised gap list for the research team. "
                    "You do NOT run searches yourself — your job is pure analysis and identification. "
                    "You are explicit: each gap is labelled RESOLVED, PARTIALLY RESOLVED, or STILL OPEN. "
                    "You always close your response with exactly: STILL OPEN: N  (N = integer count)"
                ),
                tools=[note_tool],
                llm=llm, verbose=verbose, allow_delegation=False, max_iter=4, max_rpm=10,
            )
            synthesizer = Agent(
                role="Report Synthesizer",
                goal="Produce a clear, well-structured, fully cited Markdown report that directly answers the research query.",
                backstory="You are a professional technical writer and editor. You excel at turning raw research into structured, readable reports. You cite every claim, note confidence levels inline, and organise information so readers can trust and act on it immediately.",
                tools=[draft_tool],
                llm=_make_llm(temperature=0.5), verbose=verbose, allow_delegation=False, max_iter=4, max_rpm=10,
            )

            # ── Satisfaction check for the gap loop ──────────────────────────
            def _has_open_gaps(text: str) -> bool:
                """Return True if the gap analyst output signals unresolved critical gaps."""
                # Prefer the explicit "STILL OPEN: N" count line
                m = re.search(r"still\s+open\s*:\s*(\d+)", text, re.IGNORECASE)
                if m:
                    return int(m.group(1)) > 0
                # Fallback: bare "STILL OPEN" label present in the text
                return "still open" in text.lower()

            # Mutable closure state
            _is_gap_fill_pass: list[bool] = [False]

            def _stage_callback(output):
                raw_text = (
                    getattr(output, "raw", None)
                    or str(getattr(output, "output", "") or getattr(output, "result", ""))
                    or ""
                ).strip()
                agent_role = getattr(output, "agent", "") or ""
                if "Research Specialist" in agent_role:
                    # Initial research = stage 1; targeted gap fill = stage 3
                    stage = 3 if _is_gap_fill_pass[0] else 1
                    _enforce_tool_calls(raw_text, stage=stage)
                    if not _is_gap_fill_pass[0]:
                        # Transition to analyst within phase-1 crew
                        log("Stage 1/4 complete — handing off to Critical Analyst", agent="Research Specialist")
                        log("Stage 2/4 — Critical Analyst: verifying key claims and flagging gaps", agent="Critical Analyst")
                        _current_agent[0] = "Critical Analyst"
                        _scratchpad.stream_event({"type": "iteration_tick", "agent": "Critical Analyst", "stage": 2, "pass": 1})
                        _scratchpad.stream_event({"type": "agent_switch", "agent": "Critical Analyst", "stage": 2})
                elif "Critical Analyst" in agent_role:
                    _enforce_tool_calls(raw_text, stage=2)
                    log("Stage 2/4 complete — entering gap analysis loop", agent="Critical Analyst")
                elif "Gap Analyst" in agent_role:
                    _enforce_tool_calls(raw_text, stage=3)
                elif "Report Synthesizer" in agent_role:
                    _enforce_tool_calls(raw_text, stage=4)

            def _step_callback(step_output):
                """Stream each agent reasoning step to the UI."""
                try:
                    agent = _current_agent[0]
                    raw = getattr(step_output, "log", None) or ""
                    if not raw:
                        raw = str(getattr(step_output, "output", "") or
                                  getattr(step_output, "return_values", {}).get("output", ""))
                    raw = _THINK_RE.sub("", raw).strip()
                    if raw:
                        _scratchpad.stream_event({
                            "type": "step",
                            "agent": agent,
                            "content": raw[:800],
                        })
                except Exception:
                    pass

            # ── Phase 1: Research + Analysis (single crew) ────────────────────
            research_task = Task(
                description=(
                    f"Research the following query thoroughly:\n\n  QUERY: {q}\n{_CLARIF_BLOCK}\n"
                    "You MUST use tools in this exact sequence — do not skip any step:\n\n"
                    "STEP 1 — PLAN (REQUIRED): Call update_plan immediately with a bullet-point "
                    "research strategy tailored to this specific query before doing anything else.\n\n"
                    "STEP 2 — SEARCH WITH THOUGHT TRAIL: Before each new search angle, call "
                    "record_thought with a short label describing what you are investigating "
                    "(e.g. 'Investigating career background at Texas A&M' or 'Looking for funding sources'). "
                    "Then run web_search at least FOUR times using different phrasings and angles. "
                    "For each angle write a distinct query — do not repeat similar searches.\n\n"
                    "STEP 3 — FETCH: Call fetch_webpage on the 4–6 most promising URLs. "
                    "Prioritise [Academic], [Government], and [Non-profit/NGO] sources.\n\n"
                    "STEP 4 — NOTE (REQUIRED after EACH fetch): After every fetch_webpage call, "
                    "immediately call add_note to record the key facts found on that page. "
                    "Each note must include: the source URL, source type, and 2–4 key facts.\n\n"
                    "STEP 5 — UPDATE PLAN: If research reveals the topic is broader or different "
                    "than initially thought, call update_plan again to revise your strategy.\n\n"
                    "STEP 6 — DRAFT (REQUIRED before finishing): Once you have read at least 4 pages, "
                    "call update_draft with a structured summary of your best current answer. "
                    "This becomes the working draft for the synthesis stage.\n\n"
                    "STEP 7 — OUTPUT: Return a structured list of all findings grouped by sub-topic, "
                    "each attributed to its source URL and source type tag."
                ),
                expected_output=(
                    "A structured list of findings grouped by sub-topic. Each finding must include "
                    "the claim, its source URL, and source type. Aim for at least 6 distinct sources "
                    "with a mix of types. The add_note and update_draft tools MUST have been called."
                ),
                agent=researcher,
            )
            verification_task = Task(
                description=(
                    f"Review the research findings above and verify the key claims.{_CLARIF_BLOCK}\n"
                    "You MUST use tools in this sequence:\n\n"
                    "STEP 1 — IDENTIFY: Pick the 5 most important claims from the research.\n\n"
                    "STEP 2 — VERIFY (REQUIRED): For each claim, run at least one independent "
                    "web_search and call fetch_webpage on the most authoritative result found. "
                    "Actively seek a higher-tier source than what the researcher used — prefer "
                    "[Academic], [Government], or [Primary] sources over [News] or [Social/UGC].\n\n"
                    "STEP 3 — NOTE (REQUIRED): After each verification search, call add_note with: "
                    "the claim being verified, your confidence label, and what the new source says.\n\n"
                    "STEP 4 — LABEL each claim: [VERIFIED], [LIKELY], [CONTESTED], or [UNVERIFIED].\n\n"
                    "STEP 5 — FLAG any claim supported only by [Social/UGC] or a single source.\n\n"
                    "STEP 6 — OUTPUT: Return the full verification report."
                ),
                expected_output=(
                    "A verification report listing each key claim with:\n"
                    "  - Confidence label ([VERIFIED] / [LIKELY] / [CONTESTED] / [UNVERIFIED])\n"
                    "  - Source tier (PRIMARY / SECONDARY / TERTIARY)\n"
                    "  - The corroborating or contradicting evidence found\n"
                    "  - Any source quality caveats\n"
                    "The add_note tool MUST have been called at least once."
                ),
                agent=analyst, context=[research_task],
            )

            _current_agent[0] = "Research Specialist"
            _scratchpad.stream_event({"type": "iteration_tick", "agent": "Research Specialist", "stage": 1, "pass": 1})
            _scratchpad.stream_event({"type": "agent_switch", "agent": "Research Specialist", "stage": 1})

            phase1_crew = Crew(
                agents=[researcher, analyst],
                tasks=[research_task, verification_task],
                process=Process.sequential,
                verbose=verbose,
                task_callback=_stage_callback,
                step_callback=_step_callback,
            )
            phase1_crew.kickoff()

            # ── Gap loop: Identification → Targeted Research (up to N passes) ──
            # The Gap Analyst is ANALYTICAL ONLY — it reads findings and lists gaps.
            # The Researcher then fills STILL OPEN gaps with targeted searches.
            # Repeats until no critical gaps remain or MAX_GAP_PASSES is reached.
            MAX_GAP_PASSES = 2
            all_gap_id_tasks:   list[Task] = []
            all_gap_fill_tasks: list[Task] = []

            for gap_pass in range(1, MAX_GAP_PASSES + 1):
                # ── 3a: Gap Analyst identifies gaps (no searches) ──────────────
                _is_gap_fill_pass[0] = False
                _current_agent[0] = "Gap Analyst"
                pass_label = f"pass {gap_pass}/{MAX_GAP_PASSES}"
                prior_fill = bool(all_gap_fill_tasks)
                log(f"Stage 3/4 — Gap Analyst: identifying research gaps ({pass_label})", agent="Gap Analyst")
                _scratchpad.stream_event({"type": "iteration_tick", "agent": "Gap Analyst", "stage": 3, "pass": gap_pass})
                _scratchpad.stream_event({"type": "agent_switch", "agent": "Gap Analyst", "stage": 3})

                gap_id_task = Task(
                    description=(
                        f"[Gap Analysis — {pass_label}] Review ALL findings: the initial research, "
                        f"the analyst's verification"
                        + (", and the targeted gap research from the previous pass" if prior_fill else "")
                        + f".\n{_CLARIF_BLOCK}\n"
                        "ANALYTICAL ONLY — do not run searches. Read what has been gathered and identify what is still missing.\n\n"
                        "STEP 1 — REVIEW: Read the full set of research notes, verification findings"
                        + (", and prior gap research results" if prior_fill else "")
                        + ".\n\n"
                        "STEP 2 — IDENTIFY GAPS: List the 2–4 most important gaps:\n"
                        "  - Unanswered questions raised by the research\n"
                        "  - Claims labelled [UNVERIFIED] or [CONTESTED] lacking sufficient evidence\n"
                        "  - Missing counter-arguments or opposing viewpoints\n"
                        "  - Missing context that would materially strengthen the final report\n\n"
                        "STEP 3 — CLASSIFY each gap with one of:\n"
                        "  RESOLVED — fully addressed by available research\n"
                        "  PARTIALLY RESOLVED — touched but incomplete\n"
                        "  STILL OPEN — critical, unaddressed, would meaningfully improve the report\n\n"
                        "STEP 4 — NOTE (optional): Call add_note to record your gap analysis summary.\n\n"
                        "STEP 5 — OUTPUT: List each gap with its classification and a one-line explanation.\n"
                        "  Your final line MUST be exactly: STILL OPEN: N  (N = integer count of STILL OPEN gaps)"
                    ),
                    expected_output=(
                        "A prioritised gap list. Each gap labelled RESOLVED / PARTIALLY RESOLVED / STILL OPEN. "
                        "Last line MUST be: STILL OPEN: N"
                    ),
                    agent=gap_analyst,
                    context=[research_task, verification_task] + all_gap_fill_tasks,
                )

                gap_id_crew = Crew(
                    agents=[gap_analyst],
                    tasks=[gap_id_task],
                    process=Process.sequential,
                    verbose=verbose,
                    task_callback=_stage_callback,
                    step_callback=_step_callback,
                )
                gap_id_crew.kickoff()
                all_gap_id_tasks.append(gap_id_task)

                gap_output = (
                    (gap_id_task.output.raw if gap_id_task.output else "") or ""
                ).strip()

                # ── Satisfaction check ─────────────────────────────────────────
                if not _has_open_gaps(gap_output):
                    log("Gap Analyst satisfied — no critical gaps remain. Proceeding to synthesis.", agent="Gap Analyst")
                    break
                if gap_pass >= MAX_GAP_PASSES:
                    log(
                        f"Gap Analyst: max passes ({MAX_GAP_PASSES}) reached. "
                        "Proceeding to synthesis — remaining open items noted in Caveats.",
                        agent="Gap Analyst",
                    )
                    break

                # ── 3b: Researcher fills STILL OPEN gaps with targeted searches ─
                _is_gap_fill_pass[0] = True
                _current_agent[0] = "Research Specialist"
                log(f"Stage 3/4 — Research Specialist: targeted gap research ({pass_label})", agent="Research Specialist")
                _scratchpad.stream_event({"type": "iteration_tick", "agent": "Research Specialist", "stage": 3, "pass": gap_pass})
                _scratchpad.stream_event({"type": "agent_switch", "agent": "Research Specialist", "stage": 3})

                gap_fill_task = Task(
                    description=(
                        f"TARGETED GAP RESEARCH — {pass_label}\n\n"
                        "The Gap Analyst reviewed all research and identified these open issues:\n\n"
                        f"{gap_output}\n\n"
                        "Your job: fill ONLY the gaps labelled STILL OPEN above. "
                        "Skip RESOLVED and PARTIALLY RESOLVED items — they are already covered.\n\n"
                        "For each STILL OPEN gap:\n"
                        "STEP 1 — RECORD THOUGHT (REQUIRED): Call record_thought naming the gap you are addressing "
                        "(e.g. 'Filling gap: funding breakdown not found').\n"
                        "STEP 2 — SEARCH: Run 2–3 targeted web_search queries specific to this gap. "
                        "Use precise, gap-focused phrasing. Do not repeat searches already done.\n"
                        "STEP 3 — FETCH: Call fetch_webpage on the 2 most relevant results.\n"
                        "STEP 4 — NOTE (REQUIRED): Call add_note with:\n"
                        "  - The gap label (copy from the list above)\n"
                        "  - What you found (or 'Not found after targeted search' if nothing relevant)\n"
                        "  - Your resolution assessment: RESOLVED / PARTIALLY RESOLVED / STILL OPEN\n\n"
                        "Do not re-research already resolved items. Focus exclusively on STILL OPEN gaps."
                    ),
                    expected_output=(
                        "A summary of targeted findings for each STILL OPEN gap with resolution assessment. "
                        "record_thought and add_note MUST have been called for each gap researched."
                    ),
                    agent=researcher,
                    context=[research_task, verification_task] + all_gap_id_tasks + all_gap_fill_tasks,
                )

                gap_fill_crew = Crew(
                    agents=[researcher],
                    tasks=[gap_fill_task],
                    process=Process.sequential,
                    verbose=verbose,
                    task_callback=_stage_callback,
                    step_callback=_step_callback,
                )
                gap_fill_crew.kickoff()
                all_gap_fill_tasks.append(gap_fill_task)

            # ── Phase 3: Synthesis ─────────────────────────────────────────────
            _is_gap_fill_pass[0] = False
            _current_agent[0] = "Report Synthesizer"
            log("Stage 4/4 — Report Synthesizer: writing final report", agent="Report Synthesizer")
            _scratchpad.stream_event({"type": "iteration_tick", "agent": "Report Synthesizer", "stage": 4, "pass": 1})
            _scratchpad.stream_event({"type": "agent_switch", "agent": "Report Synthesizer", "stage": 4})

            synthesis_task = Task(
                description=(
                    f"Write the final research report answering: '{q}'\n{_CLARIF_BLOCK}\n"
                    "You have access to the full research output including any targeted gap-filling research.\n\n"
                    "You MUST use tools in this sequence:\n\n"
                    "STEP 1 — DRAFT (REQUIRED FIRST): Before writing anything, call update_draft "
                    "with a complete Markdown draft of the report. Structure it as:\n"
                    "  ## Summary\n  ## Key Findings\n  ## Detailed Analysis\n"
                    "  ## Caveats & Uncertainties\n  ## Sources\n\n"
                    "STEP 2 — REFINE: Review the draft. If any section is thin, call update_draft "
                    "again with an improved version.\n\n"
                    "STEP 3 — OUTPUT: Return the final polished report text.\n\n"
                    "Rules for ALL content:\n"
                    "- Be factual and direct. Every claim must have a citation.\n"
                    "- If information is insufficient, say so clearly.\n"
                    "- The gap analysis identified remaining unknowns — acknowledge these explicitly in Caveats.\n"
                    "- CITATION INTEGRITY: Only cite URLs that were explicitly returned by "
                    "web_search or fetch_webpage during this session. NEVER invent or guess URLs. "
                    "Write [source not retrieved] instead of fabricating a link."
                ),
                expected_output=(
                    "A complete Markdown research report with Summary, Key Findings, Detailed "
                    "Analysis, Caveats, and a numbered Sources list. All cited URLs must have been "
                    "explicitly found during this session. The update_draft tool MUST have been called."
                ),
                agent=synthesizer,
                context=[research_task, verification_task] + all_gap_id_tasks + all_gap_fill_tasks,
            )

            synthesis_crew = Crew(
                agents=[synthesizer],
                tasks=[synthesis_task],
                process=Process.sequential,
                verbose=verbose,
                task_callback=_stage_callback,
                step_callback=_step_callback,
            )
            return str(synthesis_crew.kickoff())

        result = _instrumented_run(query, clarifications=clarifications)
        from tools import _search_cache
        stats = _search_cache._cache_stats()
        log(f"Search cache stats — hits: {stats['hits']}, misses: {stats['misses']}")
        log("Pipeline complete — report ready.", agent="Report Synthesizer")
        _scratchpad.stream_event({"type": "done", "status": "complete", "result": result})

        # Read current job data to preserve the log, then write final result
        current = json.loads(job_file.read_text(encoding="utf-8"))
        current["status"] = "complete"
        current["result"] = result
        job_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")

    except Exception as exc:
        try:
            current = json.loads(job_file.read_text(encoding="utf-8"))
        except Exception:
            current = {"query": query, "log": []}
        if current.get("status") != "cancelled":
            current["status"] = "error"
            current["result"] = str(exc)
            log(f"Pipeline failed: {exc}")
            _scratchpad.stream_event({"type": "done", "status": "error", "result": str(exc)})
            job_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
