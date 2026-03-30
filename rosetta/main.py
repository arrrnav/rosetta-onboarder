"""
CLI entry point for the Notion Onboarding Agent.

Install the package with ``pip install -e .`` then run:

    rosetta setup                         — interactive first-run configuration wizard
    rosetta settings                      — view and edit agent & refresh settings
    rosetta serve                         — start the Slack bot + background poller (+ optional webhook listener)
    rosetta onboard                       — add a new hire to the queue interactively
    rosetta onboard <row-id>              — manually trigger wiki generation for a DB row
    rosetta refresh [--light]            — manually trigger a wiki refresh for all Done hires
    rosetta doctor                        — check configuration and API connections
    rosetta ls                            — list all hires in the queue

Auto-trigger (default — polling):
    rosetta serve polls the New Hire Requests database every 60 seconds for rows
    with Status=Ready and kicks off wiki generation automatically.

Webhook auto-trigger (optional — instant):
    Set NOTION_WEBHOOK_SECRET and point your Notion integration's webhook at
    <your-ngrok-url>/webhook/notion.  When a DB row's Status is set to Ready,
    Notion fires page.properties_updated and rosetta serve processes it instantly.

Scheduled refresh (Milestone 5):
    Set REFRESH_ENABLED=true. rosetta serve will automatically run a light refresh
    (issues + PRs) on odd Fridays and a full wiki regeneration on even Fridays at 17:00
    in REFRESH_TIMEZONE (default UTC).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import typer
from dotenv import load_dotenv

from .cli_helpers import VERBOSE_LOGGING, console, require_env, setup_logging
from .config import DATA_DIR, DEFAULT_MODEL, STATUS_STYLES

app = typer.Typer(
    name="rosetta",
    help="Notion Engineer Onboarding Agent — generate personalised wikis for new hires.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    epilog="[bold]First time?[/bold]  [cyan]rosetta setup[/cyan]  →  [cyan]rosetta settings[/cyan]  →  [cyan]rosetta serve[/cyan]",
)


# ---------------------------------------------------------------------------
# setup command
# ---------------------------------------------------------------------------

@app.command()
def setup() -> None:
    """
    Interactive first-run setup wizard.

    Walks through configuring Notion, GitHub, Gemini, Slack, SMTP, and refresh
    settings. Can automatically create the Notion workspace structure for you.
    Writes all values to .env in the project root.
    """
    load_dotenv()

    from .setup_wizard import run as wizard_run
    wizard_run()


# ---------------------------------------------------------------------------
# settings command
# ---------------------------------------------------------------------------

@app.command()
def settings() -> None:
    """
    View and edit agent and refresh settings.

    Covers Claude model, GitHub fetch limits, and the scheduled refresh
    schedule.  Writes changes to .env in the project root.
    """
    load_dotenv()

    from .settings_manager import prompt_and_save
    prompt_and_save()


# ---------------------------------------------------------------------------
# serve command (Milestone 2)
# ---------------------------------------------------------------------------

@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Host to bind the chat server to."),
    port: int = typer.Option(8000, help="Port to listen on."),
) -> None:
    """
    Start the Rosetta server (Slack bot + Notion webhook listener + RAG).

    For webhook auto-trigger, a public URL is required. Run ngrok in a
    separate terminal:

        ngrok http 8000

    Then register the URL in your Notion integration dashboard.
    """
    load_dotenv()
    setup_logging()

    try:
        import uvicorn
    except ImportError:
        console.print("[bold red]Error:[/bold red] uvicorn is not installed. "
                      "Run: pip install uvicorn")
        raise typer.Exit(code=1)

    # Build feature status rows
    gemini_ok = bool(os.environ.get("GEMINI_API_KEY"))
    slack_ok = bool(os.environ.get("SLACK_APP_TOKEN"))
    webhook_ok = bool(os.environ.get("NOTION_WEBHOOK_SECRET", ""))
    refresh_enabled = os.environ.get("REFRESH_ENABLED", "false").lower() == "true"
    refresh_tz = os.environ.get("REFRESH_TIMEZONE", "UTC")

    def _feat(enabled: bool, yes: str, no: str) -> str:
        if enabled:
            return f"[green]✔[/green]  {yes}"
        return f"[dim]–  {no}[/dim]"

    lines = [
        f"  Server        [link]http://{host}:{port}[/link]",
        f"  Health        [link]http://{host}:{port}/health[/link]",
        "",
        f"  Gemini RAG    {_feat(gemini_ok, 'enabled', 'GEMINI_API_KEY not set')}",
        f"  Slack bot     {_feat(slack_ok, 'enabled', 'SLACK_APP_TOKEN not set')}",
        f"  Webhook       {_feat(webhook_ok, 'enabled — instant auto-trigger', 'not configured — polling every 60s')}",
        *([
            "  [dim]             To enable: 1) ngrok http 8000[/dim]",
            "  [dim]             2) register <ngrok-url>/webhook/notion in Notion dashboard[/dim]",
            "  [dim]             3) token auto-writes to .env on first connection[/dim]",
        ] if not webhook_ok else []),
        f"  Refresh       {_feat(refresh_enabled, f'enabled  (Fridays 17:00 {refresh_tz}, alternating)', 'REFRESH_ENABLED=false')}",
        "",
        "  To add a new hire: run [dim]rosetta onboard[/dim] in another terminal,",
        "  or add a row directly to the New Hire Requests database in Notion.",
        "",
        "  [dim]Press Ctrl+C to stop[/dim]",
    ]

    from rich.panel import Panel
    from rich.text import Text
    body = Text.from_markup("\n".join(lines))
    console.print(Panel(body, title="[bold]Rosetta[/bold]", expand=False))
    console.print()

    uvicorn.run(
        "rosetta.chat.server:app",
        host=host,
        port=port,
        log_level="warning" if not VERBOSE_LOGGING else os.getenv("LOG_LEVEL", "info").lower(),
    )


# ---------------------------------------------------------------------------
# onboard command
# ---------------------------------------------------------------------------

@app.command()
def onboard(
    row_id: str | None = typer.Argument(
        None,
        help="Notion page ID of a DB row to process immediately. "
             "Omit to add a new hire interactively instead.",
    ),
) -> None:
    """
    Add a new hire to the queue, or trigger processing for an existing row.

    Without a row ID: prompts for the hire's details and creates a new row in
    the New Hire Requests database (Status=Ready).  rosetta serve picks it up
    automatically.

    With a row ID: manually triggers wiki generation for that specific DB row
    (useful for re-processing or debugging).
    """
    load_dotenv()
    setup_logging()

    notion_token = require_env("NOTION_TOKEN")

    if row_id is None:
        database_id = require_env("NOTION_DATABASE_ID")
        asyncio.run(_run_add_hire(notion_token, database_id))
        return

    # Manual trigger
    parent_page_id = require_env("NOTION_ONBOARDING_PAGE_ID")
    github_token = os.environ.get("GITHUB_TOKEN")
    model = os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)

    if not github_token:
        console.print("[yellow]Warning:[/yellow] GITHUB_TOKEN not set — "
                      "using unauthenticated GitHub API (60 req/hour limit).")

    asyncio.run(_run_onboard(row_id, notion_token, parent_page_id, github_token, model))


async def _run_add_hire(notion_token: str, database_id: str) -> None:
    """Interactive flow: collect hire details and create a Ready DB row."""
    from .notion.mcp_session import NotionMCPSession

    console.print()
    name = typer.prompt("  New hire's full name")
    role = typer.prompt("  Role")

    console.print("  GitHub repo URLs [dim](one per line, empty line to finish)[/dim]")
    repo_urls: list[str] = []
    while True:
        url = typer.prompt("  >", default="", show_default=False)
        if not url:
            break
        repo_urls.append(url.strip())

    notes = typer.prompt("  Extra context for the agent (optional)", default="", show_default=False)
    email = typer.prompt("  Contact email (optional)", default="", show_default=False)
    slack = typer.prompt("  Slack handle (optional, e.g. @jane)", default="", show_default=False)
    supervisor = typer.prompt("  Supervisor Slack handle (optional, e.g. @manager)", default="", show_default=False)

    console.print("  Notion context page URLs [dim](one per line, empty line to finish)[/dim]")
    context_page_urls: list[str] = []
    while True:
        url = typer.prompt("  >", default="", show_default=False)
        if not url:
            break
        context_page_urls.append(url.strip())
    context_pages = "\n".join(context_page_urls)
    console.print()

    async with NotionMCPSession(token=notion_token) as session:
        try:
            page_id = await session.create_hire_row(
                database_id=database_id,
                name=name,
                role=role,
                repo_urls=repo_urls,
                notes=notes,
                contact_email=email.strip(),
                slack_handle=slack.strip(),
                supervisor_slack=supervisor.strip().lstrip("@"),
                context_pages=context_pages,
            )
        except Exception as exc:
            console.print(f"[bold red]Error:[/bold red] Could not create row — {exc}")
            raise typer.Exit(code=1)

    console.print(f"[bold green]✔[/bold green] Row created — {name} is queued [dim](Status: Ready)[/dim]")
    console.print(f"  [dim]Page ID: {page_id}[/dim]")
    console.print()
    console.print("  [dim]rosetta serve[/dim] will pick this up automatically, or")
    console.print(f"  run [dim]rosetta onboard {page_id}[/dim] to trigger manually.")


async def _run_onboard(
    row_id: str,
    notion_token: str,
    parent_page_id: str,
    github_token: str | None,
    model: str,
) -> None:
    """Manual CLI onboard — wraps the shared pipeline with console output."""
    from .pipeline import run_onboard_pipeline

    console.print(f"[dim]Fetching row {row_id}...[/dim]")

    try:
        result = await run_onboard_pipeline(
            page_id=row_id,
            notion_token=notion_token,
            parent_page_id=parent_page_id,
            github_token=github_token,
            model=model,
            on_status=None,  # skip status guard for manual trigger
        )
    except Exception as exc:
        console.print(f"[bold red]Error during generation:[/bold red] {exc}")
        raise typer.Exit(code=1)

    if result is None:
        console.print("[yellow]Warning:[/yellow] Row was skipped (empty name or invalid status).")
        return

    wiki_url, wiki_page_id, _wiki = result
    console.print(f"\n[bold green]Done![/bold green] Wiki created")
    console.print(f"  {wiki_url}")


# ---------------------------------------------------------------------------
# refresh command (Milestone 5)
# ---------------------------------------------------------------------------

@app.command()
def refresh(
    light: bool = typer.Option(
        False,
        "--light/--full",
        help="Light refresh: append updated issues + PRs (default: full refresh).",
    ),
) -> None:
    """
    Manually trigger a wiki refresh for all Done hires.

    Light refresh: appends updated issues and PRs to existing wiki pages.
    Full refresh: regenerates wiki pages from scratch and archives old ones.

    This runs the same logic as the automatic Friday scheduler, but on demand.
    Reads NOTION_TOKEN, NOTION_DATABASE_ID, and NOTION_ONBOARDING_PAGE_ID from .env.
    """
    load_dotenv()
    setup_logging()

    notion_token = require_env("NOTION_TOKEN")
    database_id = require_env("NOTION_DATABASE_ID")
    parent_page_id = require_env("NOTION_ONBOARDING_PAGE_ID")
    model = os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)

    refresh_type = "light" if light else "full"
    console.print(f"[bold green]Starting {refresh_type} refresh for all Done hires...[/bold green]")

    asyncio.run(_run_refresh(light, notion_token, database_id, parent_page_id, model))


async def _run_refresh(
    is_light: bool,
    notion_token: str,
    database_id: str,
    parent_page_id: str,
    model: str,
) -> None:
    """Async implementation of the refresh command."""
    from .scheduler import _do_refresh
    await _do_refresh(
        is_light=is_light,
        notion_token=notion_token,
        data_source_id=database_id,
        parent_page_id=parent_page_id,
        data_dir=DATA_DIR,
        model=model,
    )


# ---------------------------------------------------------------------------
# doctor command
# ---------------------------------------------------------------------------

@app.command()
def doctor() -> None:
    """
    Check configuration and verify live API connections.

    Prints a table showing whether each required and optional service is
    reachable.  Exits with code 1 if any required check fails.
    """
    load_dotenv()

    from .doctor import run as doctor_run
    doctor_run()


# ---------------------------------------------------------------------------
# ls command
# ---------------------------------------------------------------------------

@app.command(name="ls")
def ls_command() -> None:
    """
    List all entries in the New Hire Requests database with their current status.
    """
    load_dotenv()
    setup_logging()

    notion_token = require_env("NOTION_TOKEN")
    database_id = require_env("NOTION_DATABASE_ID")

    asyncio.run(_run_ls(notion_token, database_id))


async def _run_ls(notion_token: str, database_id: str) -> None:
    from .notion.mcp_session import NotionMCPSession
    from rich.table import Table

    async with NotionMCPSession(token=notion_token) as session:
        rows = await session.query_all_hires(database_id)

    if not rows:
        console.print("[dim]No hires found in the database.[/dim]")
        return

    table = Table(title=f"New Hire Requests — {len(rows)} row{'s' if len(rows) != 1 else ''}")
    table.add_column("Name", style="bold")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Wiki")

    for hire, status, wiki_url in rows:
        style = STATUS_STYLES.get(status, "")
        status_cell = f"[{style}]{status}[/{style}]" if style else status
        wiki_cell = wiki_url if wiki_url else "[dim]—[/dim]"
        if len(wiki_cell) > 60:
            wiki_cell = wiki_cell[:57] + "..."
        table.add_row(hire.name, hire.role or "—", status_cell, wiki_cell)

    console.print(table)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def cli() -> None:
    """Package entry point — called by the ``rosetta`` script."""
    app()


if __name__ == "__main__":
    cli()
