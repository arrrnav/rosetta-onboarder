"""
CLI entry point for the Notion Onboarding Agent.

Install the package with ``pip install -e .`` then run:

    rosetta onboard <notion-db-row-id>

Future commands (added in later milestones):
    rosetta watch     — poll DB every 5 minutes (Milestone 3)
    rosetta refresh   — re-fetch issues/PRs for an existing wiki (Milestone 5)
    rosetta setup     — interactive first-time configuration (Future Goals)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler

app = typer.Typer(
    name="rosetta",
    help="Notion Engineer Onboarding Agent — generate personalised wikis for new hires.",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


def _require_env(key: str) -> str:
    """Read a required environment variable, print a clear error and exit if missing."""
    value = os.environ.get(key)
    if not value:
        console.print(f"[bold red]Error:[/bold red] {key} is not set. "
                      f"Add it to your .env file.")
        raise typer.Exit(code=1)
    return value


# ---------------------------------------------------------------------------
# onboard command
# ---------------------------------------------------------------------------

@app.command()
def onboard(
    row_id: str = typer.Argument(
        ...,
        help="Notion page ID of the New Hire Requests DB row to process. "
             "Find it in the row's URL: notion.so/.../<page-id>",
    ),
) -> None:
    """
    Generate an onboarding wiki for a single new hire.

    Reads the specified DB row, fetches the assigned GitHub repos, runs the
    Claude agent, and creates a wiki page under the Engineering Onboarding
    parent page.  Updates the DB row with Status=Done and the wiki URL when
    complete.
    """
    load_dotenv(dotenv_path=Path(__file__).parents[2] / ".env")          # secrets (A:\Programming\.env)
    load_dotenv(dotenv_path=Path(__file__).parents[1] / ".env")          # project config (rosetta-onboarder\.env)
    _setup_logging(os.getenv("LOG_LEVEL", "INFO"))

    notion_token = _require_env("NOTION_TOKEN")
    parent_page_id = _require_env("NOTION_ONBOARDING_PAGE_ID")
    github_token = os.environ.get("GITHUB_TOKEN")   # optional but strongly recommended
    model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

    if not github_token:
        console.print("[yellow]Warning:[/yellow] GITHUB_TOKEN not set — "
                      "using unauthenticated GitHub API (60 req/hour limit).")

    asyncio.run(_run_onboard(row_id, notion_token, parent_page_id, github_token, model))


async def _run_onboard(
    row_id: str,
    notion_token: str,
    parent_page_id: str,
    github_token: str | None,
    model: str,
) -> None:
    """Async implementation of the onboard command."""
    from .agent import run_onboarding_agent
    from .github.fetcher import GithubFetcher
    from .notion.mcp_session import NotionMCPSession

    fetcher = GithubFetcher(token=github_token)

    async with NotionMCPSession(token=notion_token) as session:
        # Fetch and validate the DB row
        console.print(f"[dim]Fetching row {row_id}...[/dim]")
        try:
            hire = await session.fetch_hire_row(row_id)
        except Exception as exc:
            console.print(f"[bold red]Error:[/bold red] Could not read DB row — {exc}")
            raise typer.Exit(code=1)

        console.print(
            f"[bold green]Starting:[/bold green] {hire.name} "
            f"[dim]({hire.role})[/dim] — "
            f"{len(hire.repo_urls)} repo(s)"
        )
        for url in hire.repo_urls:
            console.print(f"  [dim]{url}[/dim]")

        # Mark as Processing so the poller (M3) doesn't pick it up again
        await session.update_hire_row(row_id, "Processing")

        try:
            wiki_url, _wiki = await run_onboarding_agent(
                hire=hire,
                fetcher=fetcher,
                notion_session=session,
                parent_page_id=parent_page_id,
                model=model,
            )
        except Exception as exc:
            # Roll back to Ready so the team lead can retry
            console.print(f"[bold red]Error during generation:[/bold red] {exc}")
            logging.getLogger(__name__).exception("Agent failed for %s", hire.name)
            await session.update_hire_row(row_id, "Ready")
            raise typer.Exit(code=1)

        # Write results back to the DB row
        await session.update_hire_row(row_id, "Done", wiki_url=wiki_url)
        console.print(f"\n[bold green]Done![/bold green] Wiki created for {hire.name}")
        console.print(f"  {wiki_url}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def cli() -> None:
    """Package entry point — called by the ``rosetta`` script."""
    app()


if __name__ == "__main__":
    cli()
