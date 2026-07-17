# Research Agent

> **⚠️ STALE SNAPSHOT (pre-2026-05). Superseded — see the repo-root ROADMAP.md and the Second Brain 'Deep Research Tool' folder.** Production today: FastAPI :8765 + Next.js :3015 (/DeepResearch), Death Star nvfp4 gemma via LiteLLM :4000, DuckDuckGo search (settled), Light/Medium/Heavy depth toggle.

A fully local, multi-agent research pipeline built with **CrewAI** and **LM Studio**.
No paid subscriptions. No mandatory API keys. Runs entirely on your machine.

---

## What it does

Three specialised AI agents collaborate to answer any research query:

| Agent | Role |
|---|---|
| **Research Specialist** | Searches the web from multiple angles, reads source pages |
| **Critical Analyst** | Cross-checks claims, labels each one VERIFIED / LIKELY / CONTESTED / UNVERIFIED |
| **Report Synthesizer** | Writes a structured Markdown report with inline citations |

The result is a cited, confidence-annotated report you can trust — or dig into further.

---

## Requirements

- **Python 3.10+**
- **LM Studio** running with the local server enabled (port 1234)
  - A model loaded with tool-calling support (e.g. Qwen3.5-35B-A3.8B, Qwen2.5-72B, etc.)
- macOS, Linux, or Windows (WSL recommended on Windows)

---

## Setup

### 1. Run the setup script

```bash
cd research-agent
chmod +x setup.sh
./setup.sh
```

This creates a `.venv` virtual environment, installs all dependencies into it, and copies `.env.example` → `.env`.
**Nothing is installed system-wide.**

### 2. Find your LM Studio model name

With LM Studio's local server running, run:

```bash
curl http://localhost:1234/v1/models
```

Copy the `"id"` value from the response (e.g. `"qwen3.5-35b-a3.8b-q4_k_m"`).

### 3. Configure `.env`

```bash
# Open .env and set:
LM_STUDIO_MODEL=qwen3.5-35b-a3.8b-q4_k_m   # ← paste your model id here
```

All other defaults work out of the box.

### 4. Activate the virtual environment

```bash
source .venv/bin/activate
```

Do this once per terminal session.

---

## Usage

### Run a one-off research query

```bash
python run.py query "What is the current state of nuclear fusion energy?"
```

Add `--verbose` to see the agents' reasoning steps as they work:

```bash
python run.py query "Best open source LLMs for coding in 2026" --verbose
```

### Start the MCP server

Exposes the research agent as an MCP tool so Claude Desktop, LM Studio agents,
LangChain, CrewAI, or any MCP-compatible system can call it.

```bash
python run.py mcp
```

#### Connect to Claude Desktop

Add the following to your Claude Desktop config
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "research-agent": {
      "command": "/ABSOLUTE/PATH/TO/research-agent/.venv/bin/python",
      "args": ["/ABSOLUTE/PATH/TO/research-agent/mcp_server.py"],
      "env": {
        "LM_STUDIO_MODEL": "your-model-id-here"
      }
    }
  }
}
```

Restart Claude Desktop. The `research` tool will appear in your tools list.

#### Connect from another CrewAI agent

```python
from crewai import Agent
from crewai_tools import MCPServerStdio

research_mcp = MCPServerStdio(
    command="/path/to/research-agent/.venv/bin/python",
    args=["/path/to/research-agent/mcp_server.py"],
)

agent = Agent(
    role="Orchestrator",
    tools=[research_mcp],
    ...
)
```

### Start the REST API server

Exposes the research agent over HTTP — callable from any language or system.

```bash
python run.py api
```

The server starts at `http://localhost:8000`.
Interactive API docs: `http://localhost:8000/docs`

#### Example request

```bash
curl -X POST http://localhost:8000/research \
     -H "Content-Type: application/json" \
     -d '{"query": "What caused the 2024 global semiconductor shortage?"}'
```

#### Example response

```json
{
  "query": "What caused the 2024 global semiconductor shortage?",
  "report": "## Summary\n...\n## Key Findings\n...",
  "status": "success"
}
```

#### Call from Python

```python
import requests

response = requests.post(
    "http://localhost:8000/research",
    json={"query": "What is the latest in quantum computing?"}
)
print(response.json()["report"])
```

---

## Search backends

| Backend | API key needed? | Quality | Notes |
|---|---|---|---|
| **DuckDuckGo** | ❌ None | Good | Default. Works immediately. |
| **LangSearch** | ✅ Free key | Better | Sign up at [langsearch.com](https://langsearch.com/dashboard) |

To switch to LangSearch, edit `.env`:

```
SEARCH_BACKEND=langsearch
LANGSEARCH_API_KEY=your_free_key_here
```

---

## Configuration reference (`.env`)

| Variable | Default | Description |
|---|---|---|
| `LM_STUDIO_BASE_URL` | `http://localhost:1234/v1` | LM Studio server URL |
| `LM_STUDIO_MODEL` | `local-model` | Model ID from LM Studio |
| `SEARCH_BACKEND` | `duckduckgo` | `duckduckgo` or `langsearch` |
| `LANGSEARCH_API_KEY` | _(empty)_ | Free key from langsearch.com |
| `API_HOST` | `0.0.0.0` | REST API bind address |
| `API_PORT` | `8000` | REST API port |
| `MCP_SERVER_NAME` | `research-agent` | Name shown in MCP clients |
| `MAX_SEARCH_RESULTS` | `5` | Results per search query |
| `MAX_PAGE_CONTENT_LENGTH` | `4000` | Max chars read from each page |

---

## Model recommendations

Any model with solid tool-calling support works. These are tested:

- **Qwen3.5-35B-A3.8B** (MoE, fast, great reasoning — the recommended choice)
- **Qwen2.5-72B** (dense, higher quality if you have the VRAM)
- **Llama 3.3 70B** (strong all-rounder)
- **Mistral Small 3.1** (lighter weight option)

In LM Studio: enable the **local server**, load your model, and confirm it appears at `http://localhost:1234/v1/models`.

---

## Project structure

```
research-agent/
├── run.py           ← Unified launcher (mcp / api / query)
├── crew.py          ← CrewAI agents + tasks pipeline
├── tools.py         ← WebSearchTool + FetchPageTool
├── mcp_server.py    ← FastMCP server (stdio)
├── api_server.py    ← FastAPI REST server
├── config.py        ← Centralised config (reads from .env)
├── requirements.txt ← Dependencies
├── setup.sh         ← One-command setup script
└── .env.example     ← Config template
```

---

## Troubleshooting

**"Connection refused" to LM Studio**
→ Open LM Studio → Developer tab → Start the local server.

**"Model not found" error**
→ Run `curl http://localhost:1234/v1/models` and paste the exact `"id"` into `LM_STUDIO_MODEL` in `.env`.

**Tool calling not working / agents loop without finishing**
→ Some quantised models have weaker tool-calling. Try a less aggressive quantisation (Q5 or Q6 instead of Q4), or switch to a model with stronger instruction following.

**Research takes very long**
→ Normal — the crew runs multiple search rounds and page fetches. Expect 2–8 minutes depending on model speed and query complexity.
