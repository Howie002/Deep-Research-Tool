"""
crew.py — The CrewAI research crew.

Three specialised agents work in sequence:
  1. Research Specialist  — multi-angle web search + page reading
  2. Critical Analyst     — cross-checks claims, flags confidence levels
  3. Report Synthesizer   — produces a final cited Markdown report

Call run_research(query) to kick off the full pipeline.
"""
from __future__ import annotations

import os
import re

import litellm
from crewai import Agent, Crew, LLM, Process, Task

from config import LM_STUDIO_BASE_URL, LM_STUDIO_MODEL
from tools import FetchPageTool, WebSearchTool

# LiteLLM (used internally by CrewAI) requires OPENAI_API_KEY to be set
# even for local endpoints that don't actually check it.
os.environ.setdefault("OPENAI_API_KEY", "lm-studio-local-no-key-needed")


# ── Think-block monkey-patch ────────────────────────────────────────────────
# Problem: Qwen3 generates <think>...</think> blocks. CrewAI uses "\nObservation:"
# as a stop sequence. When that token appears *inside* a think block, the model
# stops immediately (completion_tokens=1, content=""), causing CrewAI to loop,
# re-inject system prompts, and eventually time out.
#
# Fix: Intercept litellm.completion / acompletion *before* the response reaches
# CrewAI. We temporarily remove "\nObservation:" from the stop list so the model
# can finish its think block, then strip all think tags and re-apply the stop
# truncation ourselves.

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_THINK_TAG_RE = re.compile(r"</?think>")
_OBS_STOP = "\nObservation:"


def _clean_content(content: str, had_obs_stop: bool) -> str:
    content = _THINK_RE.sub("", content)          # strip complete think blocks
    content = _THINK_TAG_RE.sub("", content)       # strip any dangling <think> tags
    content = content.strip()
    if had_obs_stop and _OBS_STOP in content:      # re-apply the stop truncation
        content = content[: content.index(_OBS_STOP)]
    return content.strip()


def _patch_response(response, had_obs_stop: bool):
    try:
        for choice in response.choices:
            if choice.message and choice.message.content is not None:
                choice.message.content = _clean_content(
                    choice.message.content, had_obs_stop
                )
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

# Register the local model so CrewAI uses native function calling instead of
# the verbose ReAct text pattern (which generates 10K+ tokens per step).
litellm.register_model({
    f"openai/{LM_STUDIO_MODEL}": {
        "supports_function_calling": True,
        "max_tokens": 32768,
    }
})


# ── LLM factory ────────────────────────────────────────────────────────────


def _make_llm(temperature: float = 0.3) -> LLM:
    """Return an LLM pointed at the local LM Studio server.

    presence_penalty / frequency_penalty / repetition_penalty are set to
    prevent decoding collapse — small local models (Gemma in particular)
    are prone to emitting the same token until max_tokens runs out,
    corrupting the stage-handoff string.
    """
    return LLM(
        # The "openai/" prefix tells LiteLLM to use the OpenAI-compatible path
        model=f"openai/{LM_STUDIO_MODEL}",
        base_url=LM_STUDIO_BASE_URL,
        api_key="lm-studio-local-no-key-needed",
        temperature=temperature,
        timeout=3600,
        presence_penalty=0.1,
        frequency_penalty=0.3,
        extra_body={
            "enable_thinking": False,
            "repetition_penalty": 1.15,
            "repeat_penalty": 1.15,
        },
    )


# ── Main entry point ────────────────────────────────────────────────────────


def run_research(query: str, verbose: bool = False) -> str:
    """
    Run a full research pipeline for *query*.

    Returns a Markdown-formatted research report as a string.
    """
    llm = _make_llm()
    search_tool = WebSearchTool()
    fetch_tool = FetchPageTool()

    # ── Agents ─────────────────────────────────────────────────────────────

    researcher = Agent(
        role="Research Specialist",
        goal=(
            "Comprehensively gather information about '{query}' by searching "
            "the web from multiple angles and reading the most relevant pages."
        ),
        backstory=(
            "You are a senior research analyst with a talent for finding the "
            "most relevant, credible sources on any topic. You formulate "
            "diverse search queries, identify authoritative sources, and extract "
            "key facts with their URLs. You never rely on a single source."
        ),
        tools=[search_tool, fetch_tool],
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
        max_iter=10,
        max_rpm=10,
    )

    analyst = Agent(
        role="Critical Analyst & Fact-Checker",
        goal=(
            "Scrutinise the research findings for accuracy, consistency, and "
            "credibility. Verify key claims with independent searches, prioritising "
            "primary and authoritative sources over secondary or user-generated ones."
        ),
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
        tools=[search_tool, fetch_tool],
        llm=llm,
        verbose=verbose,
        allow_delegation=False,
        max_iter=6,
        max_rpm=10,
    )

    synthesizer = Agent(
        role="Report Synthesizer",
        goal=(
            "Produce a clear, well-structured, fully cited Markdown report "
            "that directly answers the research query."
        ),
        backstory=(
            "You are a professional technical writer and editor. You excel at "
            "turning raw research into structured, readable reports. You cite "
            "every claim, note confidence levels inline, and organise "
            "information so readers can trust and act on it immediately."
        ),
        tools=[],  # Synthesizer only writes — no searching
        llm=_make_llm(temperature=0.5),
        verbose=verbose,
        allow_delegation=False,
        max_iter=4,
        max_rpm=10,
    )

    # ── Tasks ───────────────────────────────────────────────────────────────

    research_task = Task(
        description=(
            f"Research the following query as thoroughly as possible:\n\n"
            f"  QUERY: {query}\n\n"
            "Steps:\n"
            "1. Search from at least FOUR different phrasings / angles of the query.\n"
            "2. Prioritise fetching [Academic], [Government], and [Non-profit/NGO] sources "
            "   when they appear — these carry the highest credibility.\n"
            "3. Use fetch_webpage on the 3–4 most promising URLs to read full content.\n"
            "4. Collect key facts, figures, dates, quotes, and source URLs.\n"
            "5. Note the apparent recency and source type of each result.\n"
            "6. If search results are mostly [News] or [Social/UGC], run an additional "
            "   search with 'site:.edu', 'site:.gov', or 'scholar' to find primary sources.\n"
            "7. Return a structured list of findings grouped by sub-topic, "
            "   each attributed to its source URL and source type."
        ),
        expected_output=(
            "A structured list of findings, grouped by sub-topic. "
            "Each finding must include the claim, its source URL, and source type tag. "
            "Aim for at least 6 distinct sources with a mix of source types."
        ),
        agent=researcher,
    )

    verification_task = Task(
        description=(
            "Review the research findings above and verify the key claims.\n\n"
            "Steps:\n"
            "1. Identify the 5 most important claims from the research.\n"
            "2. For each claim, note its source type: PRIMARY (original research, official "
            "   records, government data, academic papers) or SECONDARY (news, summaries) "
            "   or TERTIARY (blogs, social media, UGC).\n"
            "3. Run at least one independent search per claim, actively seeking a higher-tier "
            "   source than currently cited if only secondary/tertiary sources exist.\n"
            "4. Label each claim: [VERIFIED], [LIKELY], [CONTESTED], or [UNVERIFIED].\n"
            "5. Note any significant contradictions between sources.\n"
            "6. Flag claims supported only by [Social/UGC] or single-source evidence as "
            "   requiring stronger corroboration."
        ),
        expected_output=(
            "A verification report listing each key claim with:\n"
            "  - Its confidence label ([VERIFIED] / [LIKELY] / [CONTESTED] / [UNVERIFIED])\n"
            "  - Its source tier (PRIMARY / SECONDARY / TERTIARY)\n"
            "  - The corroborating or contradicting evidence found\n"
            "  - Any source quality caveats"
        ),
        agent=analyst,
        context=[research_task],
    )

    synthesis_task = Task(
        description=(
            f"Write the final research report answering: '{query}'\n\n"
            "Format (Markdown):\n"
            "## Summary\n"
            "2–4 sentence direct answer to the query.\n\n"
            "## Key Findings\n"
            "The most important facts, each with inline citation [Source: URL] "
            "and a confidence label where relevant.\n\n"
            "## Detailed Analysis\n"
            "Deeper breakdown by sub-topic with citations.\n\n"
            "## Caveats & Uncertainties\n"
            "What is contested, unknown, or requires further investigation.\n\n"
            "## Sources\n"
            "Numbered list of all URLs cited.\n\n"
            "Rules:\n"
            "- Be factual and direct; avoid filler phrases.\n"
            "- Every claim must have a citation.\n"
            "- If information is insufficient, say so clearly.\n"
            "- CRITICAL — CITATION INTEGRITY: Only cite URLs that were explicitly "
            "returned by web_search or fetch_webpage tools during this research session. "
            "NEVER construct, guess, infer, or fabricate a URL. If you do not have "
            "a real source URL from the research above, write [source not retrieved] "
            "instead of inventing a link. A missing citation is far better than a "
            "hallucinated one."
        ),
        expected_output=(
            "A complete Markdown research report with Summary, Key Findings, "
            "Detailed Analysis, Caveats, and a numbered Sources list. "
            "All claims must be cited inline. All cited URLs must have been "
            "explicitly found during this research session."
        ),
        agent=synthesizer,
        context=[research_task, verification_task],
    )

    # ── Crew ────────────────────────────────────────────────────────────────

    crew = Crew(
        agents=[researcher, analyst, synthesizer],
        tasks=[research_task, verification_task, synthesis_task],
        process=Process.sequential,
        verbose=verbose,
    )

    result = crew.kickoff()
    return str(result)
