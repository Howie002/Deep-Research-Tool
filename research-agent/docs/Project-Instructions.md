# Deep Research Agent — Project Instructions

## Working Agreement

This is a Foundation work project. The tool serves all Foundation staff for ad-hoc research, with power users in the Investment Team and Development Officers.

## Repository

**Repo:** [Howie002/Deep-Research-Agent](https://github.com/Howie002/Deep-Research-Agent) (private)
**Local path:** `~/Documents/VS Code Projects/Deep Research Tool/research-agent/`
**Active branch:** `dev2` (adaptive loop — this is where all new work goes)
**Stable branch:** `main` (linear pipeline — preserved but not actively developed)

## Tech Stack Rules

- **Local-AI only** — LM Studio is the LLM backend. No OpenAI, no Anthropic API calls from the agent itself. Air-gapped.
- **LangSearch for search** — AI-summarized results. DuckDuckGo as free fallback. No Google API.
- **No build system** — frontend is vanilla JS served by FastAPI. No React, no webpack.
- **CrewAI** for agent orchestration — native function calling, not ReAct text generation.

## Session Protocol

When working on this project:
1. `cd ~/Documents/VS Code Projects/Deep Research Tool/research-agent/`
2. Check branch: should be on `dev2` for active work
3. `git fetch --all --prune && git status` — verify clean working tree
4. Read `BRANCH_NOTES.md` for dev2-specific context
5. Read current SB Roadmap.md for active work items
6. Work, commit to `dev2`, push
7. After work: update SB Notes.md + Roadmap.md, sync to `repo/docs/`

## Data Sensitivity

- Reports may contain research on real people (donors, prospects, faculty)
- `jobs/` and `reports/` directories contain runtime artifacts — gitignored
- `.env` contains API keys (LangSearch) — gitignored
- `learning_store.json` is runtime state — gitignored

## Sync Protocol

Per Second Brain Agent Instructions:
- SB leads: Overview.md, Project-Instructions.md
- Bidirectional: Roadmap.md
- Append-only: Notes.md
- Repo mirror: `repo/docs/`
- Old `To-Do.md` is superseded by `Roadmap.md`
