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
from dotenv import dotenv_values, find_dotenv, set_key
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .cli_helpers import QUESTIONARY_STYLE

console = Console()

_NOTION_API = "https://api.notion.com/v1"
_GITHUB_API  = "https://api.github.com"

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
    "NOTION_WEBHOOK_SECRET":      "Notion webhook secret",
    "REFRESH_ENABLED":            "Scheduled refresh",
    "REFRESH_TIMEZONE":           "Refresh timezone",
}

_SECRET_KEYS = {"NOTION_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GEMINI_API_KEY", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SMTP_PASSWORD", "NOTION_WEBHOOK_SECRET"}


# ---------------------------------------------------------------------------
# Reusable prompt helpers
# ---------------------------------------------------------------------------

def _mask_value(key: str, value: str) -> str:
    """Return a display-safe preview of a config value — masked for secrets, truncated otherwise."""
    if not value:
        return ""
    if key in _SECRET_KEYS and len(value) > 8:
        return value[:4] + "..." + value[-4:]
    if len(value) > 28:
        return value[:25] + "..."
    return value


def _prompt_validated_secret(
    label: str,
    validator: callable | None = None,
    *,
    required: bool = False,
    success_template: str = "  [green]✔[/green]  {detail}",
    existing: str | None = None,
) -> str | None:
    """
    Prompt for a secret (password input) with optional live validation.

    Args:
        label:            The prompt text.
        validator:        ``(value) -> (ok: bool, detail: str)`` or None.
        required:         If True, empty input shows an error and retries.
        success_template: Format string with ``{detail}`` placeholder.

    Returns:
        The validated value, or None if the user provides an empty string
        (and ``required`` is False).
    """
    display_label = label
    if existing:
        masked = existing[:4] + "..." + existing[-4:] if len(existing) > 8 else "***"
        display_label = f"{label}  (current: {masked} — Enter to keep)"

    while True:
        value = questionary.password(display_label, style=QUESTIONARY_STYLE).ask()
        if value is None:
            _cancelled()
        value = value.strip()
        if not value:
            if existing:
                return existing
            if required:
                console.print(f"  [red]{label.split('(')[0].strip()} is required.[/red]")
                continue
            return None
        if validator is None:
            return value
        console.print("  [dim]Validating...[/dim]", end="\r")
        ok, detail = validator(value)
        if ok:
            console.print(success_template.format(detail=detail))
            return value
        console.print(f"  [red]✗[/red]  {detail} — try again")


# ---------------------------------------------------------------------------
# Notion ID parser
# ---------------------------------------------------------------------------

def _parse_notion_id(raw: str) -> str:
    """Extract a 32-char hex Notion page ID from a URL, dashed UUID, or plain hex."""
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", raw.strip()):
        return raw.strip().replace("-", "")
    if re.fullmatch(r"[0-9a-fA-F]{32}", raw.strip()):
        return raw.strip()
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
    """Create New Hire Requests DB and Wiki Archive inside the given page."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=15) as client:
        resp = client.post(f"{_NOTION_API}/databases", headers=headers, json={
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"text": {"content": "New Hire Requests"}}],
            "properties": {
                "Name":           {"title": {}},
                "Role":           {"rich_text": {}},
                "GitHub Repos":   {"rich_text": {}},
                "Agent Notes":    {"rich_text": {}},
                "Status": {"select": {"options": [
                    {"name": "Pending",    "color": "gray"},
                    {"name": "Ready",      "color": "yellow"},
                    {"name": "Processing", "color": "blue"},
                    {"name": "Done",       "color": "green"},
                ]}},
                "Wiki URL":                {"url": {}},
                "Contact Email":           {"email": {}},
                "Slack Handle":            {"rich_text": {}},
                "Supervisor Slack Handle": {"rich_text": {}},
                "Context Pages":           {"rich_text": {}},
            },
        })
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Could not create database: {resp.status_code} {resp.text[:200]}")
        db_id = resp.json()["id"]

        resp = client.post(f"{_NOTION_API}/pages", headers=headers, json={
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "icon": {"type": "external", "external": {"url": "https://www.notion.so/icons/archive_yellow.svg"}},
            "properties": {"title": [{"text": {"content": "Wiki Archive"}}]},
        })
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Could not create archive page: {resp.status_code} {resp.text[:200]}")
        graveyard_id = resp.json()["id"]

        client.patch(f"{_NOTION_API}/blocks/{parent_page_id}/children", headers=headers, json={
            "children": [{"object": "block", "type": "divider", "divider": {}}]
        })

    return parent_page_id, db_id, graveyard_id


# ---------------------------------------------------------------------------
# Wizard steps
# ---------------------------------------------------------------------------

def _ask_notion(collected: dict) -> None:
    """Step 1: Notion token."""
    console.print()
    token = _prompt_validated_secret(
        "Notion integration token",
        _validate_notion_token,
        required=True,
        success_template="  [green]✔[/green]  Connected to workspace: [bold]{detail}[/bold]",
        existing=collected.get("NOTION_TOKEN"),
    )
    collected["NOTION_TOKEN"] = token


def _ask_notion_workspace(collected: dict) -> None:
    """Step 2: provision or enter Notion resource IDs."""
    console.print()
    already_set = all(collected.get(k) for k in ("NOTION_ONBOARDING_PAGE_ID", "NOTION_DATABASE_ID"))
    choices = [
        questionary.Choice("Create it for me  (recommended)", value="create"),
        questionary.Choice("I already have one — enter IDs manually", value="manual"),
    ]
    if already_set:
        db_hint = _mask_value("NOTION_DATABASE_ID", collected["NOTION_DATABASE_ID"])
        choices.insert(0, questionary.Choice(
            f"Keep existing  (DB: {db_hint})", value="keep"
        ))
    choice = questionary.select(
        "Set up your Notion workspace?",
        choices=choices,
        style=QUESTIONARY_STYLE,
    ).ask()
    if choice is None:
        _cancelled()

    if choice == "keep":
        pass
    elif choice == "create":
        _ask_notion_workspace_create(collected)
    else:
        _ask_notion_ids_manually(collected)

    _ask_public_sharing()


def _ask_notion_workspace_create(collected: dict) -> None:
    """Auto-provision: prompt for hub page, create DB + archive inside it."""
    console.print(
        "\n  [dim]1. Create a page in Notion with whatever name you like\n"
        "     (e.g. 'Engineering Onboarding') — this will be your onboarding hub\n"
        "  2. Open it → click [bold]...[/bold] → [bold]Connect to[/bold] → select your integration\n"
        "  3. Paste its URL or ID below[/dim]"
    )
    while True:
        raw = questionary.text("Onboarding hub page URL or ID", style=QUESTIONARY_STYLE).ask()
        if raw is None:
            _cancelled()
        parent_id = _parse_notion_id(raw.strip())
        if not parent_id:
            console.print("  [red]✗[/red]  Couldn't parse a Notion ID — paste the full URL or 32-char hex ID")
            continue

        console.print("  [dim]Creating workspace structure...[/dim]", end="\r")
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
                    style=QUESTIONARY_STYLE,
                ).ask()
                if next_step is None or next_step == "manual":
                    _ask_notion_ids_manually(collected)
                    return
            continue

        console.print(f"  [green]✔[/green]  Onboarding hub          [dim](your page)[/dim]")
        console.print(f"  [green]✔[/green]  New Hire Requests DB    [dim]{db_id}[/dim]")
        console.print(f"  [green]✔[/green]  Wiki Archive            [dim]{graveyard_id}[/dim]")
        collected["NOTION_ONBOARDING_PAGE_ID"] = onboarding_id
        collected["NOTION_DATABASE_ID"]         = db_id
        collected["NOTION_GRAVEYARD_PAGE_ID"]   = graveyard_id
        break


def _ask_public_sharing() -> None:
    """Remind the user to make the hub page public (if needed)."""
    console.print(
        "\n  [dim]New hires receive a link to their wiki via Slack or email.\n"
        "  If they don't have a Notion account they won't be able to open it\n"
        "  unless the onboarding hub is publicly accessible.[/dim]"
    )
    make_public = questionary.select(
        "Who will be accessing the wikis?",
        choices=[
            questionary.Choice(
                "New hires without Notion accounts — make the hub public",
                value="public",
            ),
            questionary.Choice(
                "New hires are already Notion workspace members — keep it private",
                value="private",
            ),
        ],
        style=QUESTIONARY_STYLE,
    ).ask()
    if make_public is None:
        _cancelled()

    if make_public == "public":
        console.print(
            "\n  [dim]1. Open your onboarding hub page in Notion\n"
            "  2. Click Share (top right) → Share to web\n"
            "  3. Set to 'Anyone with the link can view'[/dim]"
        )
        confirmed = questionary.confirm(
            "I've made it public",
            default=True,
            style=QUESTIONARY_STYLE,
        ).ask()
        if confirmed is None:
            _cancelled()


def _ask_notion_ids_manually(collected: dict) -> None:
    """Prompt for the three Notion IDs when not auto-provisioning."""
    for label, key in [
        ("Engineering Onboarding page ID", "NOTION_ONBOARDING_PAGE_ID"),
        ("New Hire Requests database ID", "NOTION_DATABASE_ID"),
    ]:
        existing = collected.get(key, "")
        display_label = f"{label}  (current: {_mask_value(key, existing)} — Enter to keep)" if existing else label
        while True:
            raw = questionary.text(display_label, default=existing, style=QUESTIONARY_STYLE).ask()
            if raw is None:
                _cancelled()
            pid = _parse_notion_id(raw.strip())
            if pid:
                collected[key] = pid
                break
            console.print("  [red]✗[/red]  Invalid ID — paste the page URL or 32-char hex ID")

    raw = questionary.text(
        "Wiki Archive page ID  (optional — leave blank to skip)",
        style=QUESTIONARY_STYLE,
    ).ask()
    if raw is None:
        _cancelled()
    gid = _parse_notion_id(raw.strip())
    if gid:
        collected["NOTION_GRAVEYARD_PAGE_ID"] = gid


def _ask_anthropic(collected: dict) -> None:
    """Step 3: Anthropic API key (required)."""
    console.print()
    key = _prompt_validated_secret(
        "Anthropic API key  (console.anthropic.com)",
        required=True,
        success_template="  [green]✔[/green]  Anthropic API key saved",
        existing=collected.get("ANTHROPIC_API_KEY"),
    )
    collected["ANTHROPIC_API_KEY"] = key


def _ask_github(collected: dict) -> None:
    """Step 4: GitHub token (optional)."""
    console.print()
    existing = collected.get("GITHUB_TOKEN")
    choices = [
        questionary.Choice("Yes — add a personal access token", value="yes"),
        questionary.Choice("Skip  (60 req/hour, public repos only)", value="skip"),
    ]
    if existing:
        choices.insert(0, questionary.Choice(
            f"Keep existing  ({_mask_value('GITHUB_TOKEN', existing)})", value="keep"
        ))
    choice = questionary.select("Set up GitHub?", choices=choices, style=QUESTIONARY_STYLE).ask()
    if choice is None:
        _cancelled()
    if choice == "keep":
        return
    if choice == "skip":
        collected.pop("GITHUB_TOKEN", None)
        return

    token = _prompt_validated_secret(
        "GitHub personal access token",
        _validate_github_token,
        required=True,
        success_template="  [green]✔[/green]  Authenticated as [bold]{detail}[/bold]",
        existing=existing,
    )
    if token:
        collected["GITHUB_TOKEN"] = token


def _ask_gemini(collected: dict) -> None:
    """Step 5: Gemini API key (optional)."""
    console.print()
    existing = collected.get("GEMINI_API_KEY")
    choices = [
        questionary.Choice("Yes — add a Gemini API key", value="yes"),
        questionary.Choice("Skip — Slack bot will run without wiki Q&A", value="skip"),
    ]
    if existing:
        choices.insert(0, questionary.Choice(
            f"Keep existing  ({_mask_value('GEMINI_API_KEY', existing)})", value="keep"
        ))
    choice = questionary.select(
        "Set up Gemini?  (embeds wikis for RAG — lets the Slack bot answer questions about repos)",
        choices=choices,
        style=QUESTIONARY_STYLE,
    ).ask()
    if choice is None:
        _cancelled()
    if choice == "keep":
        return
    if choice == "skip":
        collected.pop("GEMINI_API_KEY", None)
        return

    key = _prompt_validated_secret(
        "Gemini API key  (aistudio.google.com)",
        existing=existing,
    )
    if key:
        collected["GEMINI_API_KEY"] = key


def _ask_slack(collected: dict) -> None:
    """Step 6: Slack (optional)."""
    console.print()
    existing_bot = collected.get("SLACK_BOT_TOKEN")
    existing_app = collected.get("SLACK_APP_TOKEN")
    choices = [
        questionary.Choice("Yes — configure Slack", value="yes"),
        questionary.Choice("Skip", value="skip"),
    ]
    if existing_bot:
        choices.insert(0, questionary.Choice(
            f"Keep existing  (bot: {_mask_value('SLACK_BOT_TOKEN', existing_bot)})", value="keep"
        ))
    choice = questionary.select(
        "Set up Slack notifications?",
        choices=choices,
        style=QUESTIONARY_STYLE,
    ).ask()
    if choice is None:
        _cancelled()
    if choice == "keep":
        return
    if choice == "skip":
        for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
            collected.pop(k, None)
        return

    bot = _prompt_validated_secret("Slack bot token  (xoxb-...)", existing=existing_bot)
    if bot:
        collected["SLACK_BOT_TOKEN"] = bot

    gemini_configured = bool(collected.get("GEMINI_API_KEY"))
    if gemini_configured:
        console.print(
            "\n  [bold yellow]App-level token required:[/bold yellow]\n"
            "  [dim]Gemini is configured for wiki Q&A, but the bot can't receive\n"
            "  messages without socket mode. New hires won't be able to ask questions\n"
            "  unless you provide this token.\n"
            "  Get one at api.slack.com/apps → your app → Basic Information → App-Level Tokens.[/dim]"
        )
        app_tok_prompt = "Slack app-level token  (xapp-...)"
    else:
        console.print(
            "\n  [dim]An app-level token (xapp-...) enables the bot to receive messages.\n"
            "  Without it, Rosetta can only send notifications (wiki ready, refresh alerts).\n"
            "  Get one at api.slack.com/apps → your app → Basic Information → App-Level Tokens.[/dim]"
        )
        app_tok_prompt = "Slack app-level token  (xapp-... leave blank to skip)"

    app_tok = _prompt_validated_secret(app_tok_prompt, existing=existing_app)
    if app_tok:
        collected["SLACK_APP_TOKEN"] = app_tok
    elif gemini_configured:
        console.print(
            "  [yellow]Warning:[/yellow] Gemini is set up but the app-level token was skipped —\n"
            "  new hires won't be able to DM the bot to ask wiki questions."
        )


_SMTP_PROVIDERS = {
    "gmail":    ("smtp.gmail.com",        "587"),
    "outlook":  ("smtp-mail.outlook.com", "587"),
    "sendgrid": ("smtp.sendgrid.net",     "587"),
    "other":    ("",                      "587"),
}

_SMTP_PROVIDER_NOTES = {
    "gmail": (
        "Gmail requires an App Password — your regular password won't work.\n"
        "  Get one at myaccount.google.com/apppasswords\n"
        "  (Google Account → Security → 2-Step Verification → App passwords)"
    ),
    "outlook": (
        "Use your full Outlook/Hotmail address as the username\n"
        "  and your regular account password (or an app password if 2FA is on)."
    ),
    "sendgrid": (
        "SendGrid SMTP: username is literally the word  apikey\n"
        "  and the password is your SendGrid API key."
    ),
    "other": None,
}


def _ask_smtp(collected: dict) -> None:
    """Step 7: SMTP email notifications (optional)."""
    console.print()
    existing_host = collected.get("SMTP_HOST")
    existing_user = collected.get("SMTP_USER")
    choices = [
        questionary.Choice("Gmail",              value="gmail"),
        questionary.Choice("Outlook / Hotmail",  value="outlook"),
        questionary.Choice("SendGrid",           value="sendgrid"),
        questionary.Choice("Other SMTP server",  value="other"),
        questionary.Choice("Skip",               value="skip"),
    ]
    if existing_host and existing_user:
        choices.insert(0, questionary.Choice(
            f"Keep existing  ({existing_user} via {existing_host})", value="keep"
        ))
    provider = questionary.select(
        "Set up email notifications?",
        choices=choices,
        style=QUESTIONARY_STYLE,
    ).ask()
    if provider is None:
        _cancelled()
    if provider == "keep":
        return
    if provider == "skip":
        for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"):
            collected.pop(k, None)
        return

    host_default, port_default = _SMTP_PROVIDERS[provider]
    note = _SMTP_PROVIDER_NOTES[provider]
    if note:
        console.print(f"\n  [dim]{note}[/dim]")

    host = questionary.text("SMTP host", default=existing_host or host_default, style=QUESTIONARY_STYLE).ask()
    if host is None:
        _cancelled()
    collected["SMTP_HOST"] = host.strip() or host_default

    port = questionary.text("SMTP port", default=collected.get("SMTP_PORT") or port_default, style=QUESTIONARY_STYLE).ask()
    if port is None:
        _cancelled()
    collected["SMTP_PORT"] = port.strip() or port_default

    user_prompt = "SendGrid username  (literally: apikey)" if provider == "sendgrid" else "Your email address"
    user = questionary.text(user_prompt, default=existing_user or "", style=QUESTIONARY_STYLE).ask()
    if user is None:
        _cancelled()
    if user.strip():
        collected["SMTP_USER"] = user.strip()

    pw_prompt = "SendGrid API key" if provider == "sendgrid" else "App password" if provider in ("gmail", "outlook") else "SMTP password"
    pw = _prompt_validated_secret(pw_prompt, existing=collected.get("SMTP_PASSWORD"))
    if pw:
        collected["SMTP_PASSWORD"] = pw


def _ask_refresh(collected: dict) -> None:
    """Step 8: scheduled Friday refresh (optional)."""
    console.print()
    choice = questionary.select(
        "Enable scheduled Friday wiki refresh?",
        choices=[
            questionary.Choice("Yes — refresh wikis every Friday automatically", value="yes"),
            questionary.Choice("No — I'll run rosetta refresh manually", value="no"),
        ],
        style=QUESTIONARY_STYLE,
    ).ask()
    if choice is None:
        _cancelled()

    if choice == "no":
        collected["REFRESH_ENABLED"] = "false"
        collected.pop("REFRESH_TIMEZONE", None)
        return

    collected["REFRESH_ENABLED"] = "true"
    _TZ_CHOICES = [
        questionary.Choice("UTC",                     value="UTC"),
        questionary.Separator("── Americas ──"),
        questionary.Choice("US/Eastern  (New York)",  value="America/New_York"),
        questionary.Choice("US/Central  (Chicago)",   value="America/Chicago"),
        questionary.Choice("US/Mountain (Denver)",    value="America/Denver"),
        questionary.Choice("US/Pacific  (LA)",        value="America/Los_Angeles"),
        questionary.Choice("US/Alaska",               value="America/Anchorage"),
        questionary.Choice("US/Hawaii",               value="Pacific/Honolulu"),
        questionary.Choice("Canada/Toronto",          value="America/Toronto"),
        questionary.Choice("Canada/Vancouver",        value="America/Vancouver"),
        questionary.Choice("Brazil/São Paulo",        value="America/Sao_Paulo"),
        questionary.Separator("── Europe ──"),
        questionary.Choice("UK/Ireland  (London)",    value="Europe/London"),
        questionary.Choice("Central EU  (Paris/Berlin)", value="Europe/Paris"),
        questionary.Choice("Eastern EU  (Helsinki)",  value="Europe/Helsinki"),
        questionary.Choice("Moscow",                  value="Europe/Moscow"),
        questionary.Separator("── Asia / Pacific ──"),
        questionary.Choice("India       (Kolkata)",   value="Asia/Kolkata"),
        questionary.Choice("China       (Shanghai)",  value="Asia/Shanghai"),
        questionary.Choice("Japan       (Tokyo)",     value="Asia/Tokyo"),
        questionary.Choice("Korea       (Seoul)",     value="Asia/Seoul"),
        questionary.Choice("Australia   (Sydney)",    value="Australia/Sydney"),
        questionary.Choice("New Zealand (Auckland)",  value="Pacific/Auckland"),
        questionary.Separator("── Middle East / Africa ──"),
        questionary.Choice("UAE         (Dubai)",     value="Asia/Dubai"),
        questionary.Choice("Israel      (Jerusalem)", value="Asia/Jerusalem"),
        questionary.Choice("South Africa (Johannesburg)", value="Africa/Johannesburg"),
    ]
    current_tz = collected.get("REFRESH_TIMEZONE", "UTC")
    default_choice = next(
        (c for c in _TZ_CHOICES if isinstance(c, questionary.Choice) and c.value == current_tz),
        _TZ_CHOICES[0],
    )
    tz = questionary.select(
        "Timezone for the Friday refresh",
        choices=_TZ_CHOICES,
        default=default_choice,
        style=QUESTIONARY_STYLE,
    ).ask()
    if tz is None:
        _cancelled()
    collected["REFRESH_TIMEZONE"] = tz


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
            display = val[:4] + "..." + val[-4:]
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

def run() -> None:
    env_path = Path(find_dotenv(usecwd=True) or ".env")
    collected: dict[str, str] = {k: v for k, v in dotenv_values(env_path).items() if v}

    # Welcome banner
    console.print(Panel(
        "[bold cyan]Rosetta[/bold cyan]  Notion Engineer Onboarding Agent\n\n"
        "This wizard configures your .env file.\n"
        "[dim]Press Ctrl+C at any time to cancel — nothing is saved until the end.[/dim]",
        expand=False,
    ))

    # Prereqs banner
    console.print(Panel(
        "[bold]Before you start — have these ready:[/bold]\n\n"
        "  [cyan]Notion integration token[/cyan]  (required)\n"
        "  [dim]notion.so/profile/integrations (check docs\\NOTION.md for info)[/dim]\n\n"
        "  [cyan]Anthropic API key[/cyan]  (required)\n"
        "  [dim]console.anthropic.com[/dim]\n\n"
        "  [cyan]GitHub personal access token[/cyan]  (recommended)\n"
        "  [dim]github.com/settings/tokens[/dim]\n\n"
        "  [cyan]Gemini API key[/cyan]  (recommended — enables Slack bot wiki Q&A)\n"
        "  [dim]aistudio.google.com[/dim]\n\n"
        "  [cyan]Slack bot + app-level tokens[/cyan]  (recommended — notifications + chat)\n"
        "  [dim]api.slack.com/apps (check docs\\SLACK.md for info)[/dim]",
        title="[bold yellow]Prerequisites[/bold yellow]",
        expand=False,
    ))
    questionary.press_any_key_to_continue("  Press any key when you're ready...", style=QUESTIONARY_STYLE).ask()

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

    confirm = questionary.confirm("Write to .env?", default=True, style=QUESTIONARY_STYLE).ask()
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

    console.print()
    console.print(Panel(
        Text.from_markup("\n".join(lines)),
        title="[bold green]Setup complete[/bold green]",
        expand=False,
    ))
    console.print()

    # Reload .env into os.environ before running checks so doctor sees the
    # values just written, not the stale values from process startup.
    from dotenv import load_dotenv
    load_dotenv(override=True)

    # Auto-run doctor
    console.print("[bold]Verifying connections...[/bold]\n")
    from .doctor import run as doctor_run
    doctor_run()
