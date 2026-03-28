"""
rosetta setup — interactive first-run configuration wizard.

Uses questionary for Vite-style sequential prompts (arrow-key selects,
password inputs, inline validation).  All Notion API calls are direct
httpx requests — no MCP server required at setup time.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx
import questionary
from dotenv import dotenv_values, set_key
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

_NOTION_API = "https://api.notion.com/v1"
_GITHUB_API  = "https://api.github.com"

_STYLE = questionary.Style([
    ("qmark",        "fg:#00d7ff bold"),
    ("question",     "bold"),
    ("answer",       "fg:#00ff87 bold"),
    ("pointer",      "fg:#00d7ff bold"),
    ("highlighted",  "fg:#00d7ff bold"),
    ("selected",     "fg:#00ff87"),
    ("separator",    "fg:#555555"),
    ("instruction",  "fg:#555555"),
])

_KEY_LABELS: dict[str, str] = {
    "NOTION_TOKEN":               "Notion token",
    "ANTHROPIC_API_KEY":          "Anthropic API key",
    "NOTION_ONBOARDING_PAGE_ID":  "Onboarding page ID",
    "NOTION_DATABASE_ID":         "Database ID",
    "NOTION_GRAVEYARD_PAGE_ID":   "Wiki archive page ID",
    "GITHUB_TOKEN":               "GitHub token",
    "GEMINI_API_KEY":             "Gemini API key",
    "SLACK_BOT_TOKEN":            "Slack bot token",
    "SLACK_APP_TOKEN":            "Slack app token",
    "SMTP_HOST":                  "SMTP host",
    "SMTP_PORT":                  "SMTP port",
    "SMTP_USER":                  "SMTP user",
    "SMTP_PASSWORD":              "SMTP password",
    "REFRESH_ENABLED":            "Scheduled refresh",
    "REFRESH_TIMEZONE":           "Refresh timezone",
}

_SECRET_KEYS = {"NOTION_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GEMINI_API_KEY", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SMTP_PASSWORD"}


# ---------------------------------------------------------------------------
# Notion ID parser
# ---------------------------------------------------------------------------

def _parse_notion_id(raw: str) -> str:
    """Extract a 32-char hex Notion page ID from a URL, dashed UUID, or plain hex."""
    # Plain dashed UUID
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", raw.strip()):
        return raw.strip().replace("-", "")
    # Plain 32-char hex
    if re.fullmatch(r"[0-9a-fA-F]{32}", raw.strip()):
        return raw.strip()
    # URL: ID is the trailing 32-char hex in the last path segment
    clean = raw.split("?")[0].split("#")[0].rstrip("/")
    segment = clean.rsplit("/", 1)[-1]
    m = re.search(r"([0-9a-fA-F]{32})$", segment)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Live API validators
# ---------------------------------------------------------------------------

def _validate_notion_token(token: str) -> tuple[bool, str]:
    try:
        resp = httpx.get(
            f"{_NOTION_API}/users/me",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            workspace = data.get("name") or data.get("bot", {}).get("workspace_name", "") or "connected"
            return True, workspace
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:
        return False, str(exc)[:60]


def _validate_github_token(token: str) -> tuple[bool, str]:
    try:
        resp = httpx.get(
            f"{_GITHUB_API}/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=8,
        )
        if resp.status_code == 200:
            login = resp.json().get("login", "")
            return True, f"github.com/{login}"
        return False, f"HTTP {resp.status_code}"
    except Exception as exc:
        return False, str(exc)[:60]


# ---------------------------------------------------------------------------
# Notion workspace provisioner
# ---------------------------------------------------------------------------

def _provision_notion_workspace(token: str, parent_page_id: str) -> tuple[str, str, str]:
    """
    Create Engineering Onboarding page, New Hire Requests DB, and Wiki Archive
    under the given parent page.  Returns (onboarding_id, db_id, graveyard_id).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=15) as client:
        # Engineering Onboarding page
        resp = client.post(f"{_NOTION_API}/pages", headers=headers, json={
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {"title": [{"text": {"content": "Engineering Onboarding"}}]},
        })
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Could not create onboarding page: {resp.status_code} {resp.text[:200]}")
        onboarding_id = resp.json()["id"]

        # New Hire Requests database
        resp = client.post(f"{_NOTION_API}/databases", headers=headers, json={
            "parent": {"type": "page_id", "page_id": onboarding_id},
            "title": [{"text": {"content": "New Hire Requests"}}],
            "properties": {
                "Name":           {"title": {}},
                "Role":           {"rich_text": {}},
                "GitHub Repos":   {"rich_text": {}},
                "Notes":          {"rich_text": {}},
                "Status": {"select": {"options": [
                    {"name": "Pending",    "color": "gray"},
                    {"name": "Ready",      "color": "yellow"},
                    {"name": "Processing", "color": "blue"},
                    {"name": "Done",       "color": "green"},
                ]}},
                "Wiki URL":       {"url": {}},
                "Contact Email":  {"email": {}},
                "Slack Handle":   {"rich_text": {}},
            },
        })
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Could not create database: {resp.status_code} {resp.text[:200]}")
        db_id = resp.json()["id"]

        # Wiki Archive page
        resp = client.post(f"{_NOTION_API}/pages", headers=headers, json={
            "parent": {"type": "page_id", "page_id": onboarding_id},
            "properties": {"title": [{"text": {"content": "Wiki Archive"}}]},
        })
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Could not create archive page: {resp.status_code} {resp.text[:200]}")
        graveyard_id = resp.json()["id"]

    return onboarding_id, db_id, graveyard_id


# ---------------------------------------------------------------------------
# Wizard steps
# ---------------------------------------------------------------------------

def _ask_notion(collected: dict) -> None:
    """Step 1: Notion token."""
    console.print()
    while True:
        token = questionary.password(
            "Notion integration token",
            style=_STYLE,
        ).ask()
        if token is None:
            _cancelled()
        token = token.strip()
        if not token:
            console.print("  [red]Token cannot be empty.[/red]")
            continue
        console.print("  [dim]Validating…[/dim]", end="\r")
        ok, detail = _validate_notion_token(token)
        if ok:
            console.print(f"  [green]✔[/green]  Connected to workspace: [bold]{detail}[/bold]")
            collected["NOTION_TOKEN"] = token
            return
        console.print(f"  [red]✗[/red]  {detail} — try again")


def _ask_notion_workspace(collected: dict) -> None:
    """Step 2: provision or enter Notion resource IDs."""
    console.print()
    choice = questionary.select(
        "Set up your Notion workspace?",
        choices=[
            questionary.Choice("Create it for me  (recommended)", value="create"),
            questionary.Choice("I already have one — enter IDs manually", value="manual"),
        ],
        style=_STYLE,
    ).ask()
    if choice is None:
        _cancelled()

    if choice == "create":
        console.print(
            "\n  [dim]1. Create a blank page in Notion (e.g. 'Rosetta')\n"
            "  2. Open it → click [bold]...[/bold] → [bold]Connect to[/bold] → select your integration\n"
            "  3. Paste its URL or ID below[/dim]"
        )
        while True:
            raw = questionary.text(
                "Parent page URL or ID",
                style=_STYLE,
            ).ask()
            if raw is None:
                _cancelled()
            parent_id = _parse_notion_id(raw.strip())
            if not parent_id:
                console.print("  [red]✗[/red]  Couldn't parse a Notion ID — paste the full URL or 32-char hex ID")
                continue

            console.print("  [dim]Creating workspace structure…[/dim]", end="\r")
            try:
                onboarding_id, db_id, graveyard_id = _provision_notion_workspace(
                    collected["NOTION_TOKEN"], parent_id
                )
            except RuntimeError as exc:
                msg = str(exc)
                if "404" in msg or "object_not_found" in msg:
                    console.print(
                        "  [red]✗[/red]  Page not found — your integration doesn't have access to it yet.\n\n"
                        "  [bold]To fix:[/bold]\n"
                        "  [dim]1. Open that page in Notion\n"
                        "  2. Click [bold]...[/bold] (top right) → [bold]Connect to[/bold]\n"
                        "  3. Select your integration from the list\n"
                        "  Then paste the URL again below.[/dim]"
                    )
                else:
                    console.print(f"  [red]✗[/red]  {msg}")
                    next_step = questionary.select(
                        "What would you like to do?",
                        choices=[
                            questionary.Choice("Try a different page", value="retry"),
                            questionary.Choice("Enter IDs manually instead", value="manual"),
                        ],
                        style=_STYLE,
                    ).ask()
                    if next_step is None or next_step == "manual":
                        _ask_notion_ids_manually(collected)
                        return
                continue  # loop back to URL prompt

            console.print(f"  [green]✔[/green]  Engineering Onboarding  [dim]{onboarding_id}[/dim]")
            console.print(f"  [green]✔[/green]  New Hire Requests DB    [dim]{db_id}[/dim]")
            console.print(f"  [green]✔[/green]  Wiki Archive            [dim]{graveyard_id}[/dim]")
            collected["NOTION_ONBOARDING_PAGE_ID"] = onboarding_id
            collected["NOTION_DATABASE_ID"]         = db_id
            collected["NOTION_GRAVEYARD_PAGE_ID"]   = graveyard_id
            break

    else:
        _ask_notion_ids_manually(collected)

    # Public sharing reminder — required for chat widget + new hire access
    console.print(
        "\n  [bold yellow]Make the Engineering Onboarding page public:[/bold yellow]\n"
        "  [dim]1. Open Engineering Onboarding in Notion\n"
        "  2. Click Share (top right) → Share to web\n"
        "  3. Set to 'Anyone with the link can view'\n"
        "  Required so new hires can open their wikis and use the chat widget.[/dim]"
    )
    confirmed = questionary.confirm(
        "I've made it public",
        default=True,
        style=_STYLE,
    ).ask()
    if confirmed is None:
        _cancelled()


def _ask_notion_ids_manually(collected: dict) -> None:
    """Prompt for the three Notion IDs when not auto-provisioning."""
    while True:
        raw = questionary.text("Engineering Onboarding page ID", style=_STYLE).ask()
        if raw is None:
            _cancelled()
        pid = _parse_notion_id(raw.strip())
        if pid:
            collected["NOTION_ONBOARDING_PAGE_ID"] = pid
            break
        console.print("  [red]✗[/red]  Invalid ID — paste the page URL or 32-char hex ID")

    while True:
        raw = questionary.text("New Hire Requests database ID", style=_STYLE).ask()
        if raw is None:
            _cancelled()
        did = _parse_notion_id(raw.strip())
        if did:
            collected["NOTION_DATABASE_ID"] = did
            break
        console.print("  [red]✗[/red]  Invalid ID")

    raw = questionary.text(
        "Wiki Archive page ID  (optional — leave blank to skip)",
        style=_STYLE,
    ).ask()
    if raw is None:
        _cancelled()
    gid = _parse_notion_id(raw.strip())
    if gid:
        collected["NOTION_GRAVEYARD_PAGE_ID"] = gid


def _ask_anthropic(collected: dict) -> None:
    """Step 3: Anthropic API key (required — powers the Claude wiki agent)."""
    console.print()
    while True:
        key = questionary.password(
            "Anthropic API key  (console.anthropic.com)",
            style=_STYLE,
        ).ask()
        if key is None:
            _cancelled()
        key = key.strip()
        if not key:
            console.print("  [red]Anthropic API key is required — the Claude agent cannot run without it.[/red]")
            continue
        collected["ANTHROPIC_API_KEY"] = key
        console.print("  [green]✔[/green]  Anthropic API key saved")
        return


def _ask_github(collected: dict) -> None:
    """Step 3: GitHub token (optional)."""
    console.print()
    choice = questionary.select(
        "Set up GitHub?",
        choices=[
            questionary.Choice("Yes — add a personal access token", value="yes"),
            questionary.Choice("Skip  (60 req/hour, public repos only)", value="skip"),
        ],
        style=_STYLE,
    ).ask()
    if choice is None:
        _cancelled()
    if choice == "skip":
        collected.pop("GITHUB_TOKEN", None)
        return

    while True:
        token = questionary.password("GitHub personal access token", style=_STYLE).ask()
        if token is None:
            _cancelled()
        token = token.strip()
        if not token:
            continue
        console.print("  [dim]Validating…[/dim]", end="\r")
        ok, detail = _validate_github_token(token)
        if ok:
            console.print(f"  [green]✔[/green]  Authenticated as [bold]{detail}[/bold]")
            collected["GITHUB_TOKEN"] = token
            return
        console.print(f"  [red]✗[/red]  {detail} — try again")


def _ask_gemini(collected: dict) -> None:
    """Step 4: Gemini API key (optional)."""
    console.print()
    choice = questionary.select(
        "Set up Gemini?  (embeds wikis for RAG — lets the Slack bot answer questions about repos)",
        choices=[
            questionary.Choice("Yes — add a Gemini API key", value="yes"),
            questionary.Choice("Skip — Slack bot will run without wiki Q&A", value="skip"),
        ],
        style=_STYLE,
    ).ask()
    if choice is None:
        _cancelled()
    if choice == "skip":
        collected.pop("GEMINI_API_KEY", None)
        return

    key = questionary.password(
        "Gemini API key  (aistudio.google.com)",
        style=_STYLE,
    ).ask()
    if key is None:
        _cancelled()
    if key.strip():
        collected["GEMINI_API_KEY"] = key.strip()


def _ask_slack(collected: dict) -> None:
    """Step 5: Slack (optional)."""
    console.print()
    choice = questionary.select(
        "Set up Slack notifications?",
        choices=[
            questionary.Choice("Yes — configure Slack", value="yes"),
            questionary.Choice("Skip", value="skip"),
        ],
        style=_STYLE,
    ).ask()
    if choice is None:
        _cancelled()
    if choice == "skip":
        for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
            collected.pop(k, None)
        return

    bot = questionary.password(
        "Slack bot token  (xoxb-…)",
        style=_STYLE,
    ).ask()
    if bot is None:
        _cancelled()
    if bot.strip():
        collected["SLACK_BOT_TOKEN"] = bot.strip()

    app_tok = questionary.text(
        "Slack app-level token  (xapp-… optional, for socket mode — leave blank to skip)",
        style=_STYLE,
    ).ask()
    if app_tok is None:
        _cancelled()
    if app_tok.strip():
        collected["SLACK_APP_TOKEN"] = app_tok.strip()


def _ask_smtp(collected: dict) -> None:
    """Step 6: SMTP email notifications (optional)."""
    console.print()
    choice = questionary.select(
        "Set up email notifications?",
        choices=[
            questionary.Choice("Yes — configure SMTP", value="yes"),
            questionary.Choice("Skip", value="skip"),
        ],
        style=_STYLE,
    ).ask()
    if choice is None:
        _cancelled()
    if choice == "skip":
        for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"):
            collected.pop(k, None)
        return

    for key, prompt, placeholder in [
        ("SMTP_HOST",     "SMTP host",            "smtp.gmail.com"),
        ("SMTP_PORT",     "SMTP port",             "587"),
        ("SMTP_USER",     "SMTP username",         "you@example.com"),
    ]:
        val = questionary.text(
            f"{prompt}  (e.g. {placeholder})",
            style=_STYLE,
        ).ask()
        if val is None:
            _cancelled()
        if val.strip():
            collected[key] = val.strip()

    pw = questionary.password("SMTP password / app password", style=_STYLE).ask()
    if pw is None:
        _cancelled()
    if pw.strip():
        collected["SMTP_PASSWORD"] = pw.strip()


def _ask_refresh(collected: dict) -> None:
    """Step 7: scheduled Friday refresh (optional)."""
    console.print()
    choice = questionary.select(
        "Enable scheduled Friday wiki refresh?",
        choices=[
            questionary.Choice("Yes — refresh wikis every Friday automatically", value="yes"),
            questionary.Choice("No — I'll run rosetta refresh manually", value="no"),
        ],
        style=_STYLE,
    ).ask()
    if choice is None:
        _cancelled()

    if choice == "no":
        collected["REFRESH_ENABLED"] = "false"
        collected.pop("REFRESH_TIMEZONE", None)
        return

    collected["REFRESH_ENABLED"] = "true"
    tz = questionary.text(
        "Timezone  (e.g. America/New_York)",
        default=collected.get("REFRESH_TIMEZONE", "UTC"),
        style=_STYLE,
    ).ask()
    if tz is None:
        _cancelled()
    collected["REFRESH_TIMEZONE"] = tz.strip() or "UTC"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cancelled() -> None:
    console.print("\n[dim]Setup cancelled — .env was not changed.[/dim]\n")
    sys.exit(0)


def _print_summary(collected: dict) -> None:
    console.print()
    lines = []
    for key, label in _KEY_LABELS.items():
        val = collected.get(key)
        if val is None:
            continue
        if key in _SECRET_KEYS and len(val) > 8:
            display = val[:4] + "…" + val[-4:]
        else:
            display = val
        lines.append(f"  [dim]{label:<28}[/dim] {display}")

    if not lines:
        console.print("  [dim](nothing configured yet)[/dim]")
        return

    console.print("\n".join(lines))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(env_path: Path) -> None:
    # Load existing .env values as defaults
    collected: dict[str, str] = {k: v for k, v in dotenv_values(env_path).items() if v}

    # Welcome banner
    console.print(Panel(
        "[bold cyan]Rosetta[/bold cyan]  Notion Engineer Onboarding Agent\n\n"
        "This wizard configures your .env file.\n"
        "[dim]Press Ctrl+C at any time to cancel — nothing is saved until the end.[/dim]",
        expand=False,
    ))

    try:
        _ask_notion(collected)
        _ask_notion_workspace(collected)
        _ask_anthropic(collected)
        _ask_github(collected)
        _ask_gemini(collected)
        _ask_slack(collected)
        _ask_smtp(collected)
        _ask_refresh(collected)
    except KeyboardInterrupt:
        _cancelled()

    # Summary before writing
    console.print("\n[bold]Review your configuration:[/bold]")
    _print_summary(collected)
    console.print()

    confirm = questionary.confirm("Write to .env?", default=True, style=_STYLE).ask()
    if not confirm:
        _cancelled()

    # Write
    env_path.touch(exist_ok=True)
    written: list[str] = []
    for key, value in collected.items():
        set_key(str(env_path), key, value)
        written.append(key)

    lines = ["[bold]Written to .env:[/bold]\n"]
    for key in written:
        lines.append(f"  [green]✔[/green]  {_KEY_LABELS.get(key, key)}")
    lines += [
        "",
        "  [bold]Next steps:[/bold]",
        "    [dim]rosetta doctor[/dim]      verify everything is connected",
        "    [dim]rosetta serve[/dim]       start the processing server",
        "    [dim]rosetta ls[/dim]          view the new hire queue",
    ]

    console.print()
    console.print(Panel(
        Text.from_markup("\n".join(lines)),
        title="[bold green]Setup complete[/bold green]",
        expand=False,
    ))
    console.print()
