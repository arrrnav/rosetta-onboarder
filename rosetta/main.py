"""
CLI entry point for the Notion Onboarding Agent.

Install the package with ``pip install -e .`` then run:

    rosetta setup                         — interactive first-run configuration wizard
    rosetta settings                      — view and edit agent & refresh settings
    rosetta serve                         — start the Slack bot + Notion webhook listener
    rosetta onboard                       — add a new hire to the queue interactively
    rosetta onboard <row-id>              — manually trigger wiki generation for a DB row
    rosetta refresh [--light]            — manually trigger a wiki refresh for all Done hires
    rosetta doctor                        — check configuration and API connections
    rosetta ls                            — list all hires in the queue

Webhook auto-trigger (Milestone 3):
    Set NOTION_WEBHOOK_SECRET and point your Notion integration's webhook at
    {WEBHOOK_PUBLIC_URL}/webhook/notion.  When a DB row's Status is set to Ready,
    Notion fires page.properties_updated and rosetta serve processes it automatically.

Scheduled refresh (Milestone 5):
    Set REFRESH_ENABLED=true. rosetta serve will automatically run a light refresh
    (issues + PRs) on odd Fridays and a full wiki regeneration on even Fridays at 17:00
    in REFRESH_TIMEZONE (default UTC).
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
    rich_markup_mode="rich",
    epilog="[bold]First time?[/bold]  [cyan]rosetta setup[/cyan]  →  [cyan]rosetta settings[/cyan]  →  [cyan]rosetta serve[/cyan]",
)
console = Console()

# Set to True to show internal debug output and HTTP access logs.
# Can also be enabled by setting LOG_LEVEL=DEBUG in your .env.
VERBOSE_LOGGING = False


def _setup_logging() -> None:
    if VERBOSE_LOGGING or os.getenv("LOG_LEVEL", "").upper() in ("DEBUG", "INFO"):
        level = os.getenv("LOG_LEVEL", "INFO").upper()
    else:
        level = "WARNING"
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
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

_SETTINGS: list[dict] = [
    {
        "key":     "CLAUDE_MODEL",
        "label":   "Claude model",
        "type":    "select",
        "choices": [
            "claude-sonnet-4-5",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-5",
        ],
        "default": "claude-haiku-4-5-20251001",
    },
    {
        "key":     "GITHUB_MAX_ISSUES",
        "label":   "Max GitHub issues fetched per repo",
        "type":    "int",
        "default": "10",
    },
    {
        "key":     "GITHUB_MAX_PRS",
        "label":   "Max GitHub PRs fetched per repo",
        "type":    "int",
        "default": "5",
    },
    {
        "key":     "GITHUB_TREE_DEPTH",
        "label":   "GitHub repo tree depth",
        "type":    "int",
        "default": "2",
    },
    {
        "key":     "REFRESH_ENABLED",
        "label":   "Scheduled Friday refresh",
        "type":    "bool",
        "default": "false",
    },
    {
        "key":     "REFRESH_TIMEZONE",
        "label":   "Refresh timezone",
        "type":    "text",
        "default": "UTC",
    },
]


@app.command()
def settings() -> None:
    """
    View and edit agent and refresh settings.

    Covers Claude model, GitHub fetch limits, and the scheduled refresh
    schedule.  Writes changes to .env in the project root.
    """
    import questionary
    from dotenv import dotenv_values, find_dotenv, set_key

    load_dotenv()
    env_path = Path(find_dotenv(usecwd=True) or ".env")
    current = {k: v for k, v in dotenv_values(env_path).items() if v}

    style = questionary.Style([
        ("qmark",       "fg:#00d7ff bold"),
        ("question",    "bold"),
        ("answer",      "fg:#00ff87 bold"),
        ("pointer",     "fg:#00d7ff bold"),
        ("highlighted", "fg:#00d7ff bold"),
        ("selected",    "fg:#00ff87"),
        ("instruction", "fg:#555555"),
    ])

    console.print("\n[bold]Agent & refresh settings[/bold]  [dim](Enter to keep current value)[/dim]\n")

    updated: dict[str, str] = {}

    for s in _SETTINGS:
        key      = s["key"]
        label    = s["label"]
        kind     = s["type"]
        fallback = s["default"]
        cur      = current.get(key, fallback)

        if kind == "select":
            choices = s["choices"]
            # put current value first so it's the default selection
            ordered = [cur] + [c for c in choices if c != cur]
            val = questionary.select(label, choices=ordered, style=style).ask()
            if val is None:
                console.print("\n[dim]Cancelled — .env was not changed.[/dim]\n")
                raise typer.Exit()

        elif kind == "bool":
            val_bool = questionary.confirm(
                label,
                default=(cur.lower() == "true"),
                style=style,
            ).ask()
            if val_bool is None:
                console.print("\n[dim]Cancelled — .env was not changed.[/dim]\n")
                raise typer.Exit()
            val = "true" if val_bool else "false"

        else:  # text or int
            val = questionary.text(label, default=cur, style=style).ask()
            if val is None:
                console.print("\n[dim]Cancelled — .env was not changed.[/dim]\n")
                raise typer.Exit()
            val = val.strip() or cur
            if kind == "int":
                try:
                    int(val)
                except ValueError:
                    console.print(f"  [red]✗[/red]  {val!r} is not a valid integer — keeping {cur}")
                    val = cur

        if val != cur:
            updated[key] = val

    if not updated:
        console.print("\n[dim]No changes.[/dim]\n")
        return

    console.print()
    for key, val in updated.items():
        label = next(s["label"] for s in _SETTINGS if s["key"] == key)
        old   = current.get(key, next(s["default"] for s in _SETTINGS if s["key"] == key))
        console.print(f"  [green]✔[/green]  {label}: [dim]{old}[/dim] → [bold]{val}[/bold]")

    console.print()
    confirm = questionary.confirm("Save to .env?", default=True, style=style).ask()
    if not confirm:
        console.print("[dim]Cancelled — .env was not changed.[/dim]\n")
        return

    env_path.touch(exist_ok=True)
    for key, val in updated.items():
        set_key(str(env_path), key, val)

    console.print("[bold green]Saved.[/bold green]\n")


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

    Then set WEBHOOK_PUBLIC_URL to the ngrok URL via rosetta setup.
    """
    load_dotenv()
    _setup_logging()

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
        f"  Webhook       {_feat(webhook_ok, 'enabled — instant auto-trigger', 'not set — polling every 5 min')}",
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
    _setup_logging()

    notion_token = _require_env("NOTION_TOKEN")

    if row_id is None:
        # Interactive add-a-hire flow
        database_id = _require_env("NOTION_DATABASE_ID")
        asyncio.run(_run_add_hire(notion_token, database_id))
        return

    # Manual trigger (existing behaviour — kept for debugging / re-processing)
    parent_page_id = _require_env("NOTION_ONBOARDING_PAGE_ID")
    github_token = os.environ.get("GITHUB_TOKEN")
    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

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
            wiki_url, wiki_page_id, wiki = await run_onboarding_agent(
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

        # -- Milestone 2: index wiki for chat RAG --
        gemini_key = os.environ.get("GEMINI_API_KEY")
        if gemini_key:
            from .embeddings import index_wiki
            data_dir = Path("data")
            # Collect README image URLs from all repos for multimodal embedding
            image_urls: list[str] = []
            for repo_url in hire.repo_urls:
                try:
                    image_urls.extend(fetcher.get_image_urls_from_readme(repo_url))
                except Exception:
                    pass
            console.print("[dim]Indexing wiki for chat RAG…[/dim]")
            try:
                index_wiki(wiki, wiki_page_id, data_dir, image_urls=image_urls)
                console.print("[dim]Embeddings saved.[/dim]")
            except Exception as exc:
                console.print(f"[yellow]Warning:[/yellow] Embedding failed — {exc}")
                console.print("[dim]Chat will be unavailable for this wiki.[/dim]")
        else:
            console.print(
                "[yellow]Note:[/yellow] GEMINI_API_KEY not set — skipping chat indexing."
            )

        # -- Milestone 4: notify the new hire --
        from .notify import notify_hire
        notify_hire(hire, wiki_url, wiki_page_id=wiki_page_id)


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
    _setup_logging()

    notion_token = _require_env("NOTION_TOKEN")
    database_id = _require_env("NOTION_DATABASE_ID")
    parent_page_id = _require_env("NOTION_ONBOARDING_PAGE_ID")
    github_token = os.environ.get("GITHUB_TOKEN")
    model = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    data_dir = Path("data")

    refresh_type = "light" if light else "full"
    console.print(f"[bold green]Starting {refresh_type} refresh for all Done hires...[/bold green]")

    asyncio.run(_run_refresh(light, notion_token, database_id, parent_page_id, model, data_dir))


async def _run_refresh(
    is_light: bool,
    notion_token: str,
    database_id: str,
    parent_page_id: str,
    model: str,
    data_dir: Path,
) -> None:
    """Async implementation of the refresh command."""
    from .scheduler import _do_refresh
    await _do_refresh(
        is_light=is_light,
        notion_token=notion_token,
        data_source_id=database_id,
        parent_page_id=parent_page_id,
        data_dir=data_dir,
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
    _setup_logging()

    notion_token = _require_env("NOTION_TOKEN")
    database_id = _require_env("NOTION_DATABASE_ID")

    asyncio.run(_run_ls(notion_token, database_id))


async def _run_ls(notion_token: str, database_id: str) -> None:
    from .notion.mcp_session import NotionMCPSession
    from rich.table import Table

    async with NotionMCPSession(token=notion_token) as session:
        rows = await session.query_all_hires(database_id)

    if not rows:
        console.print("[dim]No hires found in the database.[/dim]")
        return

    _STATUS_STYLE = {
        "Done": "green",
        "Ready": "yellow",
        "Processing": "blue",
        "Pending": "dim",
    }

    table = Table(title=f"New Hire Requests — {len(rows)} row{'s' if len(rows) != 1 else ''}")
    table.add_column("Name", style="bold")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Wiki")

    for hire, status, wiki_url in rows:
        style = _STATUS_STYLE.get(status, "")
        status_cell = f"[{style}]{status}[/{style}]" if style else status
        wiki_cell = wiki_url if wiki_url else "[dim]—[/dim]"
        # Truncate long wiki URLs to keep the table tidy
        if len(wiki_cell) > 60:
            wiki_cell = wiki_cell[:57] + "…"
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
