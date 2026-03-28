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
    "NOTION_WEBHOOK_SECRET":      "Notion webhook secret",
    "REFRESH_ENABLED":            "Scheduled refresh",
    "REFRESH_TIMEZONE":           "Refresh timezone",
}

_SECRET_KEYS = {"NOTION_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GEMINI_API_KEY", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SMTP_PASSWORD", "NOTION_WEBHOOK_SECRET"}


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
    Create New Hire Requests DB and Wiki Archive inside the given page.

    The page the user created and shared IS the onboarding hub
    (NOTION_ONBOARDING_PAGE_ID) — we don't create another layer on top.
    Returns (onboarding_id, db_id, graveyard_id) where onboarding_id == parent_page_id.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=15) as client:
        # New Hire Requests database — direct child of the onboarding hub
        resp = client.post(f"{_NOTION_API}/databases", headers=headers, json={
            "parent": {"type": "page_id", "page_id": parent_page_id},
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

        # Wiki Archive page — sibling of the DB, inside the onboarding hub
        resp = client.post(f"{_NOTION_API}/pages", headers=headers, json={
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "icon": {"type": "external", "external": {"url": "https://www.notion.so/icons/archive_yellow.svg"}},
            "properties": {"title": [{"text": {"content": "Wiki Archive"}}]},
        })
        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Could not create archive page: {resp.status_code} {resp.text[:200]}")
        graveyard_id = resp.json()["id"]

        # Divider on the parent page, after the Wiki Archive
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
            "\n  [dim]1. Create a page in Notion with whatever name you like\n"
            "     (e.g. 'Engineering Onboarding') — this will be your onboarding hub\n"
            "  2. Open it → click [bold]...[/bold] → [bold]Connect to[/bold] → select your integration\n"
            "  3. Paste its URL or ID below[/dim]"
        )
        while True:
            raw = questionary.text(
                "Onboarding hub page URL or ID",
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

            console.print(f"  [green]✔[/green]  Onboarding hub          [dim](your page)[/dim]")
            console.print(f"  [green]✔[/green]  New Hire Requests DB    [dim]{db_id}[/dim]")
            console.print(f"  [green]✔[/green]  Wiki Archive            [dim]{graveyard_id}[/dim]")
            collected["NOTION_ONBOARDING_PAGE_ID"] = onboarding_id
            collected["NOTION_DATABASE_ID"]         = db_id
            collected["NOTION_GRAVEYARD_PAGE_ID"]   = graveyard_id
            break

    else:
        _ask_notion_ids_manually(collected)

    # Public sharing reminder — new hires receive a Notion link via Slack/email
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
        style=_STYLE,
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

    gemini_configured = bool(collected.get("GEMINI_API_KEY"))
    if gemini_configured:
        console.print(
            "\n  [bold yellow]App-level token required:[/bold yellow]\n"
            "  [dim]Gemini is configured for wiki Q&A, but the bot can't receive\n"
            "  messages without socket mode. New hires won't be able to ask questions\n"
            "  unless you provide this token.\n"
            "  Get one at api.slack.com/apps → your app → Basic Information → App-Level Tokens.[/dim]"
        )
        app_tok_prompt = "Slack app-level token  (xapp-…)"
    else:
        console.print(
            "\n  [dim]An app-level token (xapp-…) enables the bot to receive messages.\n"
            "  Without it, Rosetta can only send notifications (wiki ready, refresh alerts).\n"
            "  Get one at api.slack.com/apps → your app → Basic Information → App-Level Tokens.[/dim]"
        )
        app_tok_prompt = "Slack app-level token  (xapp-… leave blank to skip)"

    app_tok = questionary.password(app_tok_prompt, style=_STYLE).ask()
    if app_tok is None:
        _cancelled()
    if app_tok.strip():
        collected["SLACK_APP_TOKEN"] = app_tok.strip()
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
    """Step 6: SMTP email notifications (optional)."""
    console.print()
    provider = questionary.select(
        "Set up email notifications?",
        choices=[
            questionary.Choice("Gmail",              value="gmail"),
            questionary.Choice("Outlook / Hotmail",  value="outlook"),
            questionary.Choice("SendGrid",           value="sendgrid"),
            questionary.Choice("Other SMTP server",  value="other"),
            questionary.Choice("Skip",               value="skip"),
        ],
        style=_STYLE,
    ).ask()
    if provider is None:
        _cancelled()
    if provider == "skip":
        for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"):
            collected.pop(k, None)
        return

    host_default, port_default = _SMTP_PROVIDERS[provider]
    note = _SMTP_PROVIDER_NOTES[provider]
    if note:
        console.print(f"\n  [dim]{note}[/dim]")

    host = questionary.text(
        "SMTP host",
        default=host_default,
        style=_STYLE,
    ).ask()
    if host is None:
        _cancelled()
    collected["SMTP_HOST"] = host.strip() or host_default

    port = questionary.text(
        "SMTP port",
        default=port_default,
        style=_STYLE,
    ).ask()
    if port is None:
        _cancelled()
    collected["SMTP_PORT"] = port.strip() or port_default

    user_prompt = "SendGrid username  (literally: apikey)" if provider == "sendgrid" else "Your email address"
    user = questionary.text(user_prompt, style=_STYLE).ask()
    if user is None:
        _cancelled()
    if user.strip():
        collected["SMTP_USER"] = user.strip()

    pw_prompt = "SendGrid API key" if provider == "sendgrid" else "App password" if provider in ("gmail", "outlook") else "SMTP password"
    pw = questionary.password(pw_prompt, style=_STYLE).ask()
    if pw is None:
        _cancelled()
    if pw.strip():
        collected["SMTP_PASSWORD"] = pw.strip()


def _ask_webhook(collected: dict) -> None:
    """Step 3: ngrok + Notion webhook (optional — enables instant auto-trigger)."""
    console.print()
    choice = questionary.select(
        "Set up Notion webhook auto-trigger?",
        choices=[
            questionary.Choice("Yes — trigger wiki generation the moment a row is set to Ready", value="yes"),
            questionary.Choice("Skip — rosetta serve will poll every 5 minutes instead", value="skip"),
        ],
        style=_STYLE,
    ).ask()
    if choice is None:
        _cancelled()
    if choice == "skip":
        collected.pop("NOTION_WEBHOOK_SECRET", None)
        return

    # Step 1: ngrok
    console.print(
        "\n  [bold]Step 1 — start a public tunnel with ngrok[/bold]\n"
        "  [dim]ngrok exposes your local rosetta serve to the internet so Notion\n"
        "  can POST webhook events to it.\n\n"
        "  If you don't have ngrok: https://ngrok.com/download\n\n"
        "  In a separate terminal, run:\n"
        "    ngrok http 8000\n\n"
        "  Copy the Forwarding URL — it looks like:\n"
        "    https://abc123.ngrok-free.app[/dim]"
    )
    questionary.press_any_key_to_continue("  Press any key once ngrok is running…", style=_STYLE).ask()

    # Step 2: register webhook in Notion + get the secret
    console.print(
        "\n  [bold]Step 2 — register the webhook in Notion[/bold]\n"
        "  [dim]1. Go to notion.so/profile/integrations → select your integration\n"
        "  2. Click Add webhook\n"
        "  3. URL: https://<your-ngrok-url>/webhook/notion\n"
        "  4. Subscribe to the page.properties_updated event\n"
        "  5. Start rosetta serve in another terminal\n"
        "  6. Click Create subscription — Notion POSTs a verification token to rosetta serve\n"
        "  7. Rosetta prints the token and writes it to .env automatically\n"
        "  8. Paste the token into the Notion verification form[/dim]"
    )

    console.print(
        "\n  [dim]Once rosetta serve receives the token it writes NOTION_WEBHOOK_SECRET\n"
        "  to .env automatically. You can skip this field if that already happened.[/dim]"
    )
    secret = questionary.password(
        "Verification token  (leave blank if rosetta serve already wrote it)",
        style=_STYLE,
    ).ask()
    if secret is None:
        _cancelled()
    if secret.strip():
        collected["NOTION_WEBHOOK_SECRET"] = secret.strip()
        console.print("  [green]✔[/green]  Webhook secret saved")
    else:
        console.print("  [dim]Skipped — rosetta serve will have written it automatically.[/dim]")


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

def run() -> None:
    env_path = Path(find_dotenv(usecwd=True) or ".env")
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
        _ask_webhook(collected)
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
