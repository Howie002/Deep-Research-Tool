#!/usr/bin/env python3
"""
run.py — Unified launcher for the Research Agent.

Usage:
    python run.py mcp                             # Start MCP server (stdio)
    python run.py api                             # Start REST API server
    python run.py query "your question here"      # Run the 4-stage pipeline
    python run.py query "your question" --verbose # Show agent reasoning
    python run.py adaptive "your question" --depth light|medium|heavy|ultra
                                                  # Run the adaptive claims-model loop (dev2)
"""
import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="research-agent",
        description="Research Agent — CrewAI + LM Studio multi-agent research pipeline",
    )
    sub = parser.add_subparsers(dest="mode", metavar="MODE")

    # ── mcp ────────────────────────────────────────────────────────────────
    sub.add_parser(
        "mcp",
        help="Start the MCP server (stdio transport, for Claude Desktop / other agents)",
    )

    # ── api ────────────────────────────────────────────────────────────────
    sub.add_parser(
        "api",
        help="Start the FastAPI REST server",
    )

    # ── query ──────────────────────────────────────────────────────────────
    query_parser = sub.add_parser(
        "query",
        help="Run a single research query via the 4-stage pipeline",
    )
    query_parser.add_argument(
        "question",
        help="The research question or topic",
    )
    query_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show agent reasoning steps in the terminal",
    )

    # ── adaptive ───────────────────────────────────────────────────────────
    adaptive_parser = sub.add_parser(
        "adaptive",
        help="Run the adaptive claims-model loop (dev2 architecture)",
    )
    adaptive_parser.add_argument(
        "question",
        help="The research question or topic",
    )
    adaptive_parser.add_argument(
        "--depth",
        choices=["light", "medium", "heavy", "ultra"],
        default="medium",
        help="Budget preset (controls max fetches / searches / LLM calls / wall-clock)",
    )

    args = parser.parse_args()

    if args.mode == "mcp":
        from mcp_server import mcp
        print("Starting Research Agent MCP server (stdio)…", file=sys.stderr)
        mcp.run()

    elif args.mode == "api":
        from api_server import start
        print("Starting Research Agent REST API…", file=sys.stderr)
        start()

    elif args.mode == "query":
        from crew import run_research
        print(f"\nResearching: {args.question}")
        print("─" * 60)
        print("(This takes 30–120 minutes on local hardware — the crew is working…)\n")
        result = run_research(args.question, verbose=args.verbose)
        print(result)

    elif args.mode == "adaptive":
        from adaptive_worker import main_cli
        sys.exit(main_cli(args.question, depth=args.depth))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
