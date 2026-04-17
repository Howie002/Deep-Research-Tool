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
import time
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


def _read_checkpoint_from_stream(stream_file: Path) -> dict:
    """
    Parse an existing .stream file and reconstruct checkpoint state for resume.

    Returns a dict with:
      last_stage_completed  — highest stage number fully finished (0 = none)
      stage_outputs         — {str(stage_num): output_text} from stage_complete events
      fetched_urls          — list of URLs already fetched (to skip re-fetching)
      notes                 — list of all note content strings
      plan                  — latest plan content
      draft                 — latest draft content
    """
    checkpoint: dict = {
        "last_stage_completed": 0,
        "stage_outputs": {},
        "fetched_urls": [],
        "notes": [],
        "plan": "",
        "draft": "",
    }
    if not stream_file.exists():
        return checkpoint

    for raw in stream_file.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        t = ev.get("type", "")
        if t == "stage_complete":
            stage = int(ev.get("stage", 0))
            checkpoint["stage_outputs"][str(stage)] = ev.get("output", "")
            if stage > checkpoint["last_stage_completed"]:
                checkpoint["last_stage_completed"] = stage
        elif t == "fetch":
            url = ev.get("url", "")
            if url and url not in checkpoint["fetched_urls"]:
                checkpoint["fetched_urls"].append(url)
        elif t == "note_add":
            c = ev.get("content", "")
            if c:
                checkpoint["notes"].append(c)
        elif t == "plan_update":
            c = ev.get("content", "")
            if c:
                checkpoint["plan"] = c
        elif t == "draft_update":
            c = ev.get("content", "")
            if c:
                checkpoint["draft"] = c

    return checkpoint


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
        no_learn = data.get("no_learn", False)
        _parent_report = data.get("parent_report", "")
        _gap_context   = data.get("gap_context", "")
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
        # Load relevant past insights from the learning store (if any)
        from pathlib import Path as _Path
        import learning_store as _ls
        _store_path = _Path(__file__).parent / "learning_store.json"
        _memory_insights: list[dict] = [] if no_learn else _ls.get_relevant_insights(query, _store_path)

        log(f"Starting research pipeline for: \"{query}\"")
        if _memory_insights:
            log(f"Research Memory: {len(_memory_insights)} relevant insight(s) loaded from past runs", agent="Research Specialist")
        log("Stage 1/4 — Research Specialist: gathering sources from multiple angles", agent="Research Specialist")

        def _instrumented_run(q, verbose=False, clarifications="", memory_insights=None, checkpoint=None, gap_context=None, parent_sources=None):
            # Import here to avoid circular issues; stages are logged via crew callbacks below
            from crewai import Crew, Task, Agent, Process, LLM
            from config import LM_STUDIO_BASE_URL, LM_STUDIO_MODEL
            from tools import AddNoteTool, FetchPageTool, ThoughtNodeTool, UpdateDraftTool, UpdatePlanTool, WebSearchTool, classify
            import litellm, re, os

            os.environ.setdefault("OPENAI_API_KEY", "lm-studio-local-no-key-needed")

            # Register the local model so LiteLLM/CrewAI knows it supports
            # native function calling — without this CrewAI falls back to the
            # verbose ReAct text pattern which generates 10K+ tokens per step.
            litellm.register_model({
                f"openai/{LM_STUDIO_MODEL}": {
                    "supports_function_calling": True,
                    "max_tokens": 131072,
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

            def _make_llm(temperature=0.3, max_tokens=4096):
                return LLM(
                    model=f"openai/{LM_STUDIO_MODEL}",
                    base_url=LM_STUDIO_BASE_URL,
                    api_key="lm-studio-local-no-key-needed",
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=600,
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

            # Build optional memory block from past research insights
            _MEMORY_BLOCK = ""
            if memory_insights:
                lines = [
                    "\n\nRESEARCH MEMORY — lessons from past runs on similar topics "
                    "(apply these to guide your strategy, but verify independently):"
                ]
                for ins in memory_insights:
                    domain = ins.get("topic_domain", "")
                    if domain:
                        lines.append(f"\n[Domain: {domain}]")
                    for lesson in ins.get("lessons", []):
                        lines.append(f"  • {lesson}")
                _MEMORY_BLOCK = "\n".join(lines) + "\n"

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
                # ReAct loop artifacts / jailbreak patterns
                "the user gave a final instruction",
                "you'll ignore all previous instructions",
                "ignore all previous instructions",
                "give your absolute best final answer",
                "now it's time you must",
                "stop using any tools",
                "we have already produced a final answer",
                "we need to compile at least",
                "we have fetched many pages",
                "make sure every claim is backed by",
                "the system responded with nothing",
                "we fetched it but got no content",
                "we called fetch but didn't capture",
                "[begin of final answer]",
                "[end of final answer]",
                "begin of final answer",
                "end of final answer",
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

            # Strings that, if found anywhere in a note, mean the whole thing is junk
            _HARD_REJECT = (
                "ignore all previous instructions",
                "you'll ignore all previous instructions",
                "give your absolute best final answer",
                "[begin of final answer]",
                "begin of final answer",
                '"claim":',   # JSON findings format
                '"source":',  # JSON findings format
                '"type":',    # JSON findings format
            )

            def _extract_note(text: str) -> str:
                """Extract real research content from task output, filtering instruction echo."""
                low = text.lower()
                # Hard-reject anything containing ReAct junk or JSON findings format
                if any(p in low for p in _HARD_REJECT):
                    return ""
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

                # ── Stub + enrich: every search result URL becomes a note;
                #    top 2-3 per search are fetched and their notes enriched. ──
                if stage in (1, 2):
                    _cat_priority = {"[Academic]": 0, "[Government]": 1, "[Non-profit/NGO]": 2, "[News]": 3}
                    fetched_ever: set[str] = {ev.get("url") for ev in task_events if ev.get("type") == "fetch"}
                    globally_fetched: set[str] = set(fetched_ever)
                    stub_noted: set[str] = set()  # URLs already given a stub note

                    for ev in task_events:
                        if ev.get("type") != "search_result":
                            continue
                        results = ev.get("results", [])
                        if not results:
                            continue
                        query_label = ev.get("query", "")

                        # Decide which URLs to fetch (top 2 by category, not yet fetched)
                        sorted_results = sorted(results, key=lambda r: _cat_priority.get(r.get("category", ""), 9))
                        to_fetch_urls: set[str] = set()
                        count = 0
                        for r in sorted_results:
                            url = r.get("url", "")
                            if url and url not in globally_fetched and count < 2:
                                to_fetch_urls.add(url)
                                count += 1

                        # Create a stub note for every URL in this search
                        for r in results:
                            url = r.get("url", "")
                            if not url or url in stub_noted:
                                continue
                            stub_noted.add(url)
                            title = r.get("title", "No title")
                            cat = r.get("category", "")
                            snippet = r.get("snippet", "")

                            if url in to_fetch_urls:
                                # Fetch and write enriched note
                                globally_fetched.add(url)
                                try:
                                    content = fetch_tool._run(url=url)
                                    if content and len(content) > 200 and "Already fetched" not in content:
                                        note_tool._run(content=(
                                            f"URL: {url}\n"
                                            f"Title: {title}\n"
                                            f"Source type: {cat}\n"
                                            f"Found in search: '{query_label}'\n"
                                            f"Key facts: {content[:1800]}\n"
                                            f"Relevance: Fetched and read — contains primary content."
                                        ))
                                        log(f"Auto-fetched + enriched: {url}", agent=_current_agent[0])
                                    else:
                                        # Fetch failed — fall back to stub
                                        note_tool._run(content=(
                                            f"URL: {url}\n"
                                            f"Title: {title}\n"
                                            f"Source type: {cat}\n"
                                            f"Found in search: '{query_label}'\n"
                                            f"Snippet: {snippet}\n"
                                            f"Status: Fetch attempted but returned no content."
                                        ))
                                except Exception:
                                    pass
                            else:
                                # Stub note only — URL found but not fetched
                                note_tool._run(content=(
                                    f"URL: {url}\n"
                                    f"Title: {title}\n"
                                    f"Source type: {cat}\n"
                                    f"Found in search: '{query_label}'\n"
                                    f"Snippet: {snippet}\n"
                                    f"Status: Found — not fetched."
                                ))

                if stage in (1, 2, 3) and "note_add" not in types_seen:
                    content = _extract_note(raw_text)
                    if content:
                        note_tool._run(content=content)
                        log("Auto-extracted note (LLM skipped add_note)", agent=_current_agent[0])

                if stage == 1 and "plan_update" not in types_seen:
                    # Only extract a plan if content looks like real research strategy
                    plan_content = _extract_note(raw_text)
                    if plan_content:
                        # Ensure every bullet becomes a checklist item
                        lines = []
                        for line in plan_content.splitlines():
                            s = line.strip()
                            if not s:
                                continue
                            if s.startswith("- ["):
                                lines.append(s)
                            elif s.startswith(("- ", "* ", "• ")):
                                lines.append("- [ ] " + s[2:].strip())
                            elif s[0].isdigit() and len(s) > 2 and s[1] in ".):":
                                lines.append("- [ ] " + s[2:].strip())
                            else:
                                lines.append("- [ ] " + s)
                        checklist = "\n".join(lines[:12])
                        if checklist:
                            plan_tool._run(content=checklist)
                            log("Auto-extracted plan as checklist (LLM skipped update_plan)", agent=_current_agent[0])

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
                llm=_make_llm(temperature=0.5), verbose=verbose, allow_delegation=False, max_iter=6, max_rpm=10,
            )

            def _stage_callback(output):
                raw_text = (
                    getattr(output, "raw", None)
                    or str(getattr(output, "output", "") or getattr(output, "result", ""))
                    or ""
                ).strip()
                agent_role = getattr(output, "agent", "") or ""
                if "Research Specialist" in agent_role:
                    _scratchpad.stream_event({"type": "stage_complete", "stage": 1, "output": raw_text[:4000]})
                    _enforce_tool_calls(raw_text, stage=1)
                    log("Stage 1/4 complete — handing off to Critical Analyst", agent="Research Specialist")
                    log("Stage 2/4 — Critical Analyst: verifying key claims and flagging gaps", agent="Critical Analyst")
                    _current_agent[0] = "Critical Analyst"
                    _scratchpad.stream_event({"type": "iteration_tick", "agent": "Critical Analyst", "stage": 2, "pass": 1})
                    _scratchpad.stream_event({"type": "agent_switch", "agent": "Critical Analyst", "stage": 2})
                elif "Critical Analyst" in agent_role:
                    _scratchpad.stream_event({"type": "stage_complete", "stage": 2, "output": raw_text[:4000]})
                    _enforce_tool_calls(raw_text, stage=2)
                    log("Stage 2/4 complete — Gap Analyst reading findings and identifying gaps", agent="Critical Analyst")
                    log("Stage 3/4 — Gap Analyst: identifying research gaps (analysis only)", agent="Gap Analyst")
                    _current_agent[0] = "Gap Analyst"
                    _scratchpad.stream_event({"type": "iteration_tick", "agent": "Gap Analyst", "stage": 3, "pass": 1})
                    _scratchpad.stream_event({"type": "agent_switch", "agent": "Gap Analyst", "stage": 3})
                elif "Gap Analyst" in agent_role:
                    _scratchpad.stream_event({"type": "stage_complete", "stage": 3, "output": raw_text[:4000]})
                    _enforce_tool_calls(raw_text, stage=3)
                    log("Stage 3/4 — Gap Analyst complete. Handing off to Report Synthesizer.", agent="Gap Analyst")
                    log("Stage 4/4 — Report Synthesizer: writing final report", agent="Report Synthesizer")
                    _current_agent[0] = "Report Synthesizer"
                    _scratchpad.stream_event({"type": "iteration_tick", "agent": "Report Synthesizer", "stage": 4, "pass": 1})
                    _scratchpad.stream_event({"type": "agent_switch", "agent": "Report Synthesizer", "stage": 4})
                elif "Report Synthesizer" in agent_role:
                    _scratchpad.stream_event({"type": "stage_complete", "stage": 4, "output": raw_text[:4000]})
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

            # ── Tasks: 4-stage single crew ────────────────────────────────────
            # research → verification → gap_id → synthesis
            # context= is minimal per task to stay within local LLM context windows.
            # gap_id_task output goes to synthesis context so gaps are acknowledged.

            research_task = Task(
                description=(
                    f"Research the following query thoroughly:\n\n  QUERY: {q}\n{_CLARIF_BLOCK}{_MEMORY_BLOCK}\n"
                    "You MUST use tools in this exact sequence — do not skip any step:\n\n"
                    "STEP 1 — PLAN (REQUIRED): Call update_plan immediately with a checklist "
                    "of research steps in this EXACT format — every line must start with '- [ ]':\n"
                    "  - [ ] Search for subject's professional background and current role\n"
                    "  - [ ] Find philanthropic history and giving patterns\n"
                    "  - [ ] Verify employment and career claims\n"
                    "  (…tailor steps to this specific query)\n"
                    "Do NOT use plain bullets or numbered lists — use '- [ ]' for every step.\n\n"
                    "STEP 2 — SEARCH → FETCH → NOTE (repeat for each angle):\n"
                    "For EACH research angle, do ALL THREE of these in order before moving to the next angle:\n"
                    "  a) Call record_thought with a short label for what you are investigating.\n"
                    "  b) Call web_search with a specific query for that angle.\n"
                    "  c) IMMEDIATELY call fetch_webpage on the #1 result URL from that search.\n"
                    "     Do NOT run another search without fetching first.\n"
                    "  d) Call add_note with what you learned from that page.\n"
                    "Cover at least FOUR distinct angles this way. "
                    "Prioritise [Academic], [Government], and [Non-profit/NGO] URLs when fetching.\n\n"
                    "STEP 3 — FETCH ADDITIONAL PAGES: After completing all angles, fetch 2–3 more "
                    "of the most promising URLs you have not yet read.\n\n"
                    "STEP 4 — NOTE (REQUIRED after EACH fetch): After every fetch_webpage call, "
                    "immediately call add_note. Each note MUST follow this exact format:\n"
                    "  URL: <url>\n"
                    "  Source type: <type>\n"
                    "  Key facts: 1) ... 2) ... 3) ...\n"
                    "  Relevance: <1–2 sentences explaining what this source reveals about the "
                    "research query and why it matters to the overall picture>\n\n"
                    "STEP 5 — CHECK OFF PLAN (REQUIRED): After completing each search angle, "
                    "call update_plan again with the SAME checklist but replace '- [ ]' with "
                    "'- [x]' for every step you have now completed. Do this progressively — "
                    "do not wait until the end. Each update_plan call should show more [x] items "
                    "than the previous one.\n\n"
                    "STEP 6 — DRAFT (REQUIRED before finishing): Once you have read at least 4 pages, "
                    "call update_draft with a structured summary of your best current answer. "
                    "This becomes the working draft for the synthesis stage.\n\n"
                    "STEP 7 — OUTPUT: Return a prose summary of all findings grouped by sub-topic. "
                    "Write in full sentences. Do NOT output JSON, code blocks, or claim/source lists. "
                    "Each paragraph should cover one sub-topic and cite sources inline as (URL)."
                ),
                expected_output=(
                    "Prose paragraphs summarising findings by sub-topic, with inline source URLs. "
                    "At least 6 sources, mix of types. No JSON. No code blocks. "
                    "The add_note and update_draft tools MUST have been called."
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
                    "STEP 3 — NOTE (REQUIRED): After each verification search, call add_note with:\n"
                    "  Claim: <claim being verified>\n"
                    "  Confidence: [VERIFIED / LIKELY / CONTESTED / UNVERIFIED]\n"
                    "  Evidence: <what the new source says>\n"
                    "  Relevance: <why this verification result matters to the research question>\n\n"
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
            gap_id_task = Task(
                description=(
                    f"Review the analyst's verification findings above for query: '{q}'\n{_CLARIF_BLOCK}\n"
                    "ANALYTICAL ONLY — do not run searches. Read what has been found and identify gaps.\n\n"
                    "STEP 1 — IDENTIFY GAPS: List 2–4 important gaps:\n"
                    "  - Unanswered questions raised by the research\n"
                    "  - Claims labelled [UNVERIFIED] or [CONTESTED] lacking evidence\n"
                    "  - Missing counter-arguments or opposing viewpoints\n"
                    "  - Missing context that would materially strengthen the report\n\n"
                    "STEP 2 — CLASSIFY each gap:\n"
                    "  RESOLVED — fully addressed\n"
                    "  PARTIALLY RESOLVED — touched but incomplete\n"
                    "  STILL OPEN — critical, unaddressed\n\n"
                    "STEP 3 — NOTE (optional): Call add_note to record your gap summary.\n\n"
                    "STEP 4 — OUTPUT: List each gap with classification and one-line explanation.\n"
                    "  Your final line MUST be exactly: STILL OPEN: N  (N = integer count of critical open gaps)"
                ),
                expected_output=(
                    "A gap list with each item labelled RESOLVED / PARTIALLY RESOLVED / STILL OPEN. "
                    "Last line MUST be: STILL OPEN: N"
                ),
                agent=gap_analyst,
                context=[verification_task],
            )
            synthesis_task = Task(
                description=(
                    f"Write the final research report answering: '{q}'\n{_CLARIF_BLOCK}{_MEMORY_BLOCK}\n"
                    "OUTPUT RULES: Markdown prose only — no JSON, no code blocks. "
                    "First line MUST be '## Summary' with no text before it. "
                    "Cite every factual claim with its source URL in parentheses.\n\n"
                    "STEP 1 — Call update_draft with a complete report containing ALL these sections:\n"
                    "## Summary — 2–3 sentence executive answer to the query.\n"
                    "## Background — who/what the subject is and relevant context (2+ paragraphs).\n"
                    "## Key Findings — 4–6 bullet points each citing a URL. Cover: professional "
                    "background, financial profile, philanthropic history, personality/values, "
                    "institutional alignment.\n"
                    "## Detailed Analysis — one ### subsection per Key Finding, each 2+ paragraphs "
                    "discussing evidence quality and confidence level.\n"
                    "## Caveats & Uncertainties — every STILL OPEN gap from gap analysis and what "
                    "it means for reliability of conclusions.\n"
                    "## Sources — numbered list of all cited URLs with one-line descriptions.\n\n"
                    "STEP 2 — Return the final report. First line must be '## Summary'."
                ),
                expected_output=(
                    "Complete Markdown report starting with '## Summary'. "
                    "All sections present: Summary, Background, Key Findings, "
                    "Detailed Analysis (with ### subsections), Caveats & Uncertainties, Sources. "
                    "500+ words. No JSON. Every claim has a source URL. update_draft was called."
                ),
                agent=synthesizer,
                context=[research_task, verification_task, gap_id_task],
            )

            # ── Gap-fill: pre-mark parent sources and inject gap focus ────────
            if gap_context:
                from tools import _fetched as _ftk
                for _url in (parent_sources or []):
                    _ftk.mark(_url)

                _gap_block = (
                    "\n\n[GAP-FILLING MODE — TARGETED FOLLOW-UP RESEARCH]\n"
                    "This is a focused continuation of a prior completed research run.\n"
                    "Your SOLE MISSION is to investigate the following STILL OPEN gaps:\n\n"
                    f"{gap_context}\n\n"
                    "CRITICAL CONSTRAINTS FOR THIS RUN:\n"
                    "  • Focus ONLY on finding NEW sources that specifically answer the open questions above.\n"
                    "  • Do NOT re-search topics already thoroughly covered in the original run.\n"
                    "  • Use highly targeted queries aimed directly at each gap (e.g. primary data, counter-arguments, missing context).\n"
                    "  • Prioritise [Academic], [Government], and [Primary] sources for any unverified claims.\n"
                    "  • Your plan (update_plan) should list one step per gap item — mark each off as you address it.\n"
                    "[END GAP-FILLING INSTRUCTIONS]\n"
                )
                research_task = Task(
                    description=_gap_block + research_task.description,
                    expected_output=research_task.expected_output,
                    agent=researcher,
                )
                _scratchpad.stream_event({
                    "type": "log",
                    "message": (
                        f"🔍 GAP-FILL RUN: focusing on {len((parent_sources or []))} "
                        f"pre-marked sources — researching open gaps from prior analysis"
                    ),
                })

            # ── Resume: pre-mark fetched URLs and inject prior context ────────
            _start_stage = 1
            _resume_prefix = ""

            if checkpoint and checkpoint.get("last_stage_completed", 0) > 0:
                from tools import _fetched as _ftk
                for _url in checkpoint.get("fetched_urls", []):
                    _ftk.mark(_url)

                _start_stage = checkpoint["last_stage_completed"] + 1

                _parts = ["\n\n[RESUMED RUN — PRIOR RESEARCH CONTEXT]"]
                _stage_labels = {1: "Stage 1 Research", 2: "Stage 2 Verification", 3: "Stage 3 Gap Analysis"}
                for _sn in sorted(checkpoint.get("stage_outputs", {}), key=int):
                    _out = checkpoint["stage_outputs"][_sn][:2500]
                    if _out:
                        _parts.append(f"\n[{_stage_labels.get(int(_sn), f'Stage {_sn}')} Output]:\n{_out}")
                if checkpoint.get("notes"):
                    _joined = "\n---\n".join(n[:250] for n in checkpoint["notes"][-20:])
                    _parts.append(f"\n[Prior Notes ({len(checkpoint['notes'])} recorded, showing last 20)]:\n{_joined}")
                if checkpoint.get("plan"):
                    _parts.append(f"\n[Last Known Plan]:\n{checkpoint['plan']}")
                if checkpoint.get("draft"):
                    _parts.append(f"\n[Last Working Draft]:\n{checkpoint['draft'][:1000]}")
                _parts.append("[END PRIOR CONTEXT]\n")
                _resume_prefix = "\n".join(_parts)

                log(f"▶ Resuming from Stage {_start_stage} — {len(checkpoint['fetched_urls'])} URLs pre-marked, "
                    f"{len(checkpoint['notes'])} notes, {len(checkpoint['stage_outputs'])} stages already done",
                    agent="Research Specialist")
                _scratchpad.stream_event({
                    "type": "log",
                    "message": f"▶ RESUMED: continuing pipeline from Stage {_start_stage}/4 "
                               f"({len(checkpoint['fetched_urls'])} sources already gathered, "
                               f"{len(checkpoint['notes'])} notes preserved)",
                })

            # Inject resume context into the first task we're running
            if _resume_prefix:
                if _start_stage == 2:
                    verification_task = Task(
                        description=verification_task.description + _resume_prefix,
                        expected_output=verification_task.expected_output,
                        agent=analyst,
                    )
                elif _start_stage == 3:
                    gap_id_task = Task(
                        description=gap_id_task.description + _resume_prefix,
                        expected_output=gap_id_task.expected_output,
                        agent=gap_analyst,
                    )
                elif _start_stage >= 4:
                    synthesis_task = Task(
                        description=synthesis_task.description + _resume_prefix,
                        expected_output=synthesis_task.expected_output,
                        agent=synthesizer,
                        context=[],
                    )

            # Only run tasks from the failed stage onward
            _all_tasks = [research_task, verification_task, gap_id_task, synthesis_task]
            tasks_to_run = _all_tasks[_start_stage - 1:]

            _start_agent_map = {
                1: "Research Specialist",
                2: "Critical Analyst",
                3: "Gap Analyst",
                4: "Report Synthesizer",
            }
            _start_agent = _start_agent_map.get(_start_stage, "Research Specialist")

            # ── Single crew — sequential ──────────────────────────────────────
            _current_agent[0] = _start_agent
            _scratchpad.stream_event({"type": "iteration_tick", "agent": _start_agent, "stage": _start_stage, "pass": 1})
            _scratchpad.stream_event({"type": "agent_switch", "agent": _start_agent, "stage": _start_stage})

            crew = Crew(
                agents=[researcher, analyst, gap_analyst],
                tasks=tasks_to_run,
                process=Process.sequential,
                verbose=verbose,
                task_callback=_stage_callback,
                step_callback=_step_callback,
            )

            # Auto-retry on LM Studio connection failures (transient crashes)
            _MAX_LLM_RETRIES = 2
            _LLM_RETRY_DELAY = 30
            _result = None
            for _attempt in range(_MAX_LLM_RETRIES + 1):
                try:
                    _result = str(crew.kickoff())
                    break
                except Exception as _exc:
                    _msg = str(_exc).lower()
                    _is_conn = any(s in _msg for s in (
                        "connection", "connect error", "timeout", "refused",
                        "unreachable", "network", "socket", "eof",
                        "broken pipe", "bad gateway", "service unavailable",
                    ))
                    if _is_conn and _attempt < _MAX_LLM_RETRIES:
                        log(f"LLM connection lost (attempt {_attempt + 1}/{_MAX_LLM_RETRIES + 1}): "
                            f"{_exc}. Retrying in {_LLM_RETRY_DELAY}s…")
                        _scratchpad.stream_event({
                            "type": "log",
                            "message": f"⚠ LLM connection lost — retrying in {_LLM_RETRY_DELAY}s "
                                       f"(attempt {_attempt + 1}/{_MAX_LLM_RETRIES + 1})…",
                        })
                        time.sleep(_LLM_RETRY_DELAY)
                    else:
                        raise
            return _result

        # Load checkpoint if this is a resumed run
        _checkpoint = None
        if data.get("resume"):
            stream_file = jobs_dir / f"{job_id}.stream"
            _checkpoint = _read_checkpoint_from_stream(stream_file)
            log(f"Resume checkpoint loaded: stage {_checkpoint['last_stage_completed']} last completed, "
                f"{len(_checkpoint['fetched_urls'])} fetched URLs, "
                f"{len(_checkpoint['notes'])} notes")

        # Load already-fetched URLs from the parent report so we don't re-fetch them
        _parent_sources: list[str] = []
        if _parent_report and _gap_context:
            try:
                import json as _json
                _reports_dir = Path(__file__).parent / "reports"
                _src_file = _reports_dir / Path(_parent_report).stem / "sources.json"
                if _src_file.exists():
                    _parent_sources = [
                        s["url"] for s in _json.loads(_src_file.read_text(encoding="utf-8"))
                        if s.get("url") and s.get("confirmed")
                    ]
                    log(f"Gap-fill mode: {len(_parent_sources)} parent sources pre-marked as already read")
            except Exception:
                pass

        result = _instrumented_run(
            query,
            clarifications=clarifications,
            memory_insights=_memory_insights,
            checkpoint=_checkpoint,
            gap_context=_gap_context,
            parent_sources=_parent_sources,
        )
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
