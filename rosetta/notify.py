"""
Post-wiki notifications — Milestone 4.

After a wiki is created, optionally notify the new hire by email and/or Slack DM.
Both channels are opt-in: if the relevant DB field is blank or the required env vars
are not set, that channel is silently skipped.

Email env vars (all required for email to send):
    SMTP_HOST       — e.g. smtp.gmail.com
    SMTP_PORT       — default 587 (STARTTLS)
    SMTP_USER       — login username
    SMTP_PASSWORD   — login password
    SMTP_FROM       — From address, e.g. "Rosetta <rosetta@company.com>"

Slack env vars:
    SLACK_BOT_TOKEN — Bot OAuth token (xoxb-...)
                      Bot must have chat:write and users:read scopes.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.text import MIMEText

from .notion.models import OnboardingInput

logger = logging.getLogger(__name__)


def notify_hire(hire: OnboardingInput, wiki_url: str, wiki_page_id: str = "") -> None:
    """
    Fire email and/or Slack notification after wiki creation.

    wiki_page_id is stored in data/slack_wiki_map.json so the Slack bot can
    route follow-up DMs to the correct VectorStore.

    Failures are caught and logged — a notification error never aborts the
    main onboard flow.
    """
    if hire.contact_email:
        try:
            _send_email(hire, wiki_url)
        except Exception:
            logger.exception("Email notification failed for %s", hire.name)

    if hire.slack_handle:
        try:
            _send_slack(hire, wiki_url, wiki_page_id)
        except Exception:
            logger.exception("Slack notification failed for %s", hire.name)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _send_email(hire: OnboardingInput, wiki_url: str) -> None:
    host     = os.getenv("SMTP_HOST", "")
    port     = int(os.getenv("SMTP_PORT", "587"))
    user     = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    from_    = os.getenv("SMTP_FROM", user)

    if not all([host, user, password]):
        logger.warning(
            "Email notification skipped for %s — SMTP_HOST / SMTP_USER / SMTP_PASSWORD not set",
            hire.name,
        )
        return

    body = (
        f"Hi {hire.name},\n\n"
        f"Your personalised onboarding wiki is ready. "
        f"It covers your repos, how to get set up, good first issues, and team conventions.\n\n"
        f"Open it here: {wiki_url}\n\n"
        f"The wiki also has a built-in chat assistant — ask it anything about your repos "
        f"without leaving the page.\n\n"
        f"Welcome to the team!\n"
    )

    msg = MIMEText(body)
    msg["Subject"] = f"Your onboarding wiki is ready, {hire.name}"
    msg["From"]    = from_
    msg["To"]      = hire.contact_email

    with smtplib.SMTP(host, port) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(user, password)
        smtp.sendmail(from_, hire.contact_email, msg.as_string())

    logger.info("Email sent to %s (%s)", hire.name, hire.contact_email)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def _slack_client():
    """Return an initialised WebClient, or None if SLACK_BOT_TOKEN is not set."""
    token = os.getenv("SLACK_BOT_TOKEN", "")
    if not token:
        return None
    try:
        from slack_sdk import WebClient  # type: ignore[import]
        return WebClient(token=token)
    except ImportError:
        logger.warning(
            "slack-sdk not installed — Slack notifications unavailable. "
            "Run: pip install slack-sdk"
        )
        return None


def _resolve_slack_user_id(hire: OnboardingInput, client) -> str | None:
    """Resolve hire.slack_handle to a Slack user ID. Returns None if not found."""
    from slack_sdk.errors import SlackApiError  # type: ignore[import]
    try:
        result = client.users_list()
        user_id = next(
            (
                m["id"]
                for m in result["members"]
                if m.get("name") == hire.slack_handle
                or m.get("profile", {}).get("display_name") == hire.slack_handle
            ),
            None,
        )
    except SlackApiError:
        logger.exception("Slack users_list failed for %s", hire.name)
        return None
    if not user_id:
        logger.warning(
            "Could not resolve @%s to a Slack user ID", hire.slack_handle
        )
    return user_id


def _update_slack_wiki_map(user_id: str, wiki_page_id: str) -> None:
    """Write user_id → wiki_page_id to data/slack_wiki_map.json."""
    import json as _json
    from pathlib import Path as _Path
    data_dir = _Path(os.getenv("CHAT_DATA_DIR", "data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    map_path = data_dir / "slack_wiki_map.json"
    mapping: dict = {}
    if map_path.exists():
        try:
            mapping = _json.loads(map_path.read_text())
        except Exception:
            pass
    mapping[user_id] = wiki_page_id
    map_path.write_text(_json.dumps(mapping, indent=2))
    logger.info("Slack wiki mapping saved: %s → %s", user_id, wiki_page_id)


def _send_slack(hire: OnboardingInput, wiki_url: str, wiki_page_id: str = "") -> None:
    client = _slack_client()
    if not client:
        logger.warning("Slack notification skipped for %s — SLACK_BOT_TOKEN not set", hire.name)
        return

    user_id = _resolve_slack_user_id(hire, client)
    if not user_id:
        return

    message = (
        f"Hi {hire.name}! Your onboarding wiki is ready: {wiki_url}\n\n"
        f"It covers your repos, getting started, good first issues, and team conventions.\n\n"
        f"Reply to this message to ask me anything about your codebase."
    )

    from slack_sdk.errors import SlackApiError  # type: ignore[import]
    try:
        client.chat_postMessage(channel=user_id, text=message)
        logger.info("Slack DM sent to @%s (%s)", hire.slack_handle, hire.name)
    except SlackApiError:
        logger.exception("Slack chat_postMessage failed for %s", hire.name)
        return

    if wiki_page_id:
        _update_slack_wiki_map(user_id, wiki_page_id)


def notify_light_refresh(hire: OnboardingInput, wiki_url: str) -> None:
    """Send a Slack DM notifying the hire that their wiki has been lightly refreshed."""
    if not hire.slack_handle:
        return
    try:
        client = _slack_client()
        if not client:
            return
        user_id = _resolve_slack_user_id(hire, client)
        if not user_id:
            return
        from slack_sdk.errors import SlackApiError  # type: ignore[import]
        message = (
            f"Hi {hire.name}! Your onboarding wiki has been updated with the latest "
            f"open issues and recent PRs.\n\n{wiki_url}"
        )
        client.chat_postMessage(channel=user_id, text=message)
        logger.info("Light refresh DM sent to @%s", hire.slack_handle)
    except Exception:
        logger.exception("notify_light_refresh failed for %s", hire.name)


def notify_full_refresh(hire: OnboardingInput, new_wiki_url: str, new_wiki_page_id: str = "") -> None:
    """Send a Slack DM notifying the hire that their wiki has been fully refreshed."""
    if not hire.slack_handle:
        return
    try:
        client = _slack_client()
        if not client:
            return
        user_id = _resolve_slack_user_id(hire, client)
        if not user_id:
            return
        from slack_sdk.errors import SlackApiError  # type: ignore[import]
        message = (
            f"Hi {hire.name}! Your onboarding wiki has been fully refreshed with the "
            f"latest codebase context. Your previous wiki has been archived.\n\n"
            f"New wiki: {new_wiki_url}"
        )
        client.chat_postMessage(channel=user_id, text=message)
        logger.info("Full refresh DM sent to @%s", hire.slack_handle)
        if new_wiki_page_id:
            _update_slack_wiki_map(user_id, new_wiki_page_id)
    except Exception:
        logger.exception("notify_full_refresh failed for %s", hire.name)
