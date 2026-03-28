"""
rosetta doctor — validate configuration and live API connections.

Prints a Rich table with one row per check.  Required checks failing causes
exit code 1; optional gaps are informational (shown with –, not ✗).
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import NamedTuple

import httpx
from rich.console import Console
from rich.table import Table

console = Console()

_NOTION_API = "https://api.notion.com/v1"
_GITHUB_API = "https://api.github.com"


class _Result(NamedTuple):
    label: str
    ok: bool | None   # True=pass, False=fail, None=skipped/optional
    detail: str


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_notion_token(token: str) -> _Result:
    try:
        resp = httpx.get(
            f"{_NOTION_API}/users/me",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            workspace = data.get("name") or data.get("bot", {}).get("workspace_name", "")
            detail = f"workspace: {workspace}" if workspace else "connected"
            return _Result("NOTION_TOKEN", True, detail)
        return _Result("NOTION_TOKEN", False, f"HTTP {resp.status_code}: {resp.text[:80]}")
    except Exception as exc:
        return _Result("NOTION_TOKEN", False, str(exc)[:80])


def _check_notion_page(token: str, page_id: str, label: str) -> _Result:
    try:
        clean = page_id.replace("-", "")
        if len(clean) == 32:
            uuid = f"{clean[:8]}-{clean[8:12]}-{clean[12:16]}-{clean[16:20]}-{clean[20:]}"
        else:
            uuid = page_id
        resp = httpx.get(
            f"{_NOTION_API}/pages/{uuid}",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            props = data.get("properties", {})
            title_spans = (
                props.get("title", {}).get("title", [])
                or props.get("Name", {}).get("title", [])
            )
            title = "".join(s.get("plain_text", "") for s in title_spans).strip()
            return _Result(label, True, title or "found")
        return _Result(label, False, f"HTTP {resp.status_code}: {resp.text[:80]}")
    except Exception as exc:
        return _Result(label, False, str(exc)[:80])


def _check_notion_database(token: str, db_id: str, label: str) -> _Result:
    try:
        clean = db_id.replace("-", "")
        if len(clean) == 32:
            uuid = f"{clean[:8]}-{clean[8:12]}-{clean[12:16]}-{clean[16:20]}-{clean[20:]}"
        else:
            uuid = db_id
        resp = httpx.post(
            f"{_NOTION_API}/databases/{uuid}/query",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            json={"page_size": 1},
            timeout=8,
        )
        if resp.status_code == 200:
            count = len(resp.json().get("results", []))
            return _Result(label, True, f"{count} row(s) visible")
        return _Result(label, False, f"HTTP {resp.status_code}: {resp.text[:80]}")
    except Exception as exc:
        return _Result(label, False, str(exc)[:80])


def _check_anthropic_key(key: str) -> _Result:
    # Just check the key is set — a live call would cost tokens
    if key:
        return _Result("ANTHROPIC_API_KEY", True, "set")
    return _Result("ANTHROPIC_API_KEY", False, "not set")


def _check_github_token(token: str) -> _Result:
    try:
        resp = httpx.get(
            f"{_GITHUB_API}/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=8,
        )
        if resp.status_code == 200:
            login = resp.json().get("login", "")
            return _Result("GITHUB_TOKEN", True, f"github.com/{login}" if login else "connected")
        return _Result("GITHUB_TOKEN", False, f"HTTP {resp.status_code}")
    except Exception as exc:
        return _Result("GITHUB_TOKEN", False, str(exc)[:80])


def _check_gemini_key(key: str) -> _Result:
    if key:
        return _Result("GEMINI_API_KEY", True, "set")
    return _Result("GEMINI_API_KEY", None, "not configured (optional — enables chat RAG)")


def _check_slack(bot_token: str) -> _Result:
    if not bot_token:
        return _Result("Slack (SLACK_BOT_TOKEN)", None, "not configured (optional)")
    return _Result("Slack (SLACK_BOT_TOKEN)", True, "set")


def _check_smtp() -> _Result:
    host = os.environ.get("SMTP_HOST", "")
    user = os.environ.get("SMTP_USER", "")
    if host and user:
        return _Result("SMTP email", True, f"{host} / {user}")
    if not host and not user:
        return _Result("SMTP email", None, "not configured (optional)")
    return _Result("SMTP email", False, "SMTP_HOST or SMTP_USER missing")


def _check_webhook() -> _Result:
    secret = os.environ.get("NOTION_WEBHOOK_SECRET", "")
    if secret:
        return _Result("Notion webhook", True, "secret set — instant auto-trigger enabled")
    return _Result("Notion webhook", None, "not configured (optional — falls back to 60s poll)")


def _check_refresh() -> _Result:
    enabled = os.environ.get("REFRESH_ENABLED", "false").lower() == "true"
    tz = os.environ.get("REFRESH_TIMEZONE", "UTC")
    if enabled:
        return _Result("Scheduled refresh", True, f"enabled — Fridays 17:00 {tz}, alternating")
    return _Result("Scheduled refresh", None, "REFRESH_ENABLED=false")


def _check_mcp_server() -> _Result:
    # Check node_modules/.bin first (project-local install), then PATH
    local = os.path.join(os.getcwd(), "node_modules", ".bin", "notion-mcp-server")
    if os.path.exists(local):
        return _Result("notion-mcp-server (npm)", True, "found in node_modules/.bin")
    if shutil.which("notion-mcp-server"):
        return _Result("notion-mcp-server (npm)", True, "found on PATH")
    return _Result(
        "notion-mcp-server (npm)",
        False,
        "not found — run: npx -y @notionhq/notion-mcp-server",
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run() -> None:
    notion_token = os.environ.get("NOTION_TOKEN", "")
    notion_page_id = os.environ.get("NOTION_ONBOARDING_PAGE_ID", "")
    notion_db_id = os.environ.get("NOTION_DATABASE_ID", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    slack_bot = os.environ.get("SLACK_BOT_TOKEN", "")

    results: list[_Result] = []

    # Required
    if notion_token:
        results.append(_check_notion_token(notion_token))
    else:
        results.append(_Result("NOTION_TOKEN", False, "not set"))

    if notion_page_id:
        if notion_token:
            results.append(_check_notion_page(notion_token, notion_page_id, "NOTION_ONBOARDING_PAGE_ID"))
        else:
            results.append(_Result("NOTION_ONBOARDING_PAGE_ID", False, "NOTION_TOKEN required first"))
    else:
        results.append(_Result("NOTION_ONBOARDING_PAGE_ID", False, "not set"))

    if notion_db_id:
        if notion_token:
            results.append(_check_notion_database(notion_token, notion_db_id, "NOTION_DATABASE_ID"))
        else:
            results.append(_Result("NOTION_DATABASE_ID", False, "NOTION_TOKEN required first"))
    else:
        results.append(_Result("NOTION_DATABASE_ID", False, "not set"))

    results.append(_check_webhook())
    results.append(_check_anthropic_key(anthropic_key))

    if github_token:
        results.append(_check_github_token(github_token))
    else:
        results.append(_Result("GITHUB_TOKEN", None, "not set (optional — 60 req/hour without it)"))

    # Optional
    results.append(_check_gemini_key(gemini_key))
    results.append(_check_slack(slack_bot))
    results.append(_check_smtp())
    results.append(_check_refresh())
    results.append(_check_mcp_server())

    # Render table
    table = Table(title="Rosetta configuration check", show_header=True)
    table.add_column("Check", style="bold", min_width=32)
    table.add_column("Status", min_width=6)
    table.add_column("Detail")

    any_required_failed = False
    for r in results:
        if r.ok is True:
            status_cell = "[green]✔[/green]"
        elif r.ok is False:
            status_cell = "[bold red]✗[/bold red]"
            any_required_failed = True
        else:
            status_cell = "[dim]–[/dim]"
        table.add_row(r.label, status_cell, r.detail)

    console.print()
    console.print(table)
    console.print()

    if any_required_failed:
        console.print("[bold red]One or more required checks failed.[/bold red]  "
                      "Run [dim]rosetta setup[/dim] to fix your configuration.")
        sys.exit(1)
    else:
        console.print("[bold green]All required checks passed.[/bold green]")
