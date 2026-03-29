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

import json
import logging
import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from .config import DATA_DIR
from .notion.models import OnboardingInput

logger = logging.getLogger(__name__)

# Import SlackApiError once at module level (lazy — only if slack_sdk installed)
_SlackApiError: type | None = None


def _get_slack_api_error():
    global _SlackApiError
    if _SlackApiError is None:
        try:
            from slack_sdk.errors import SlackApiError  # type: ignore[import]
            _SlackApiError = SlackApiError
        except ImportError:
            pass
    return _SlackApiError


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def notify_hire(hire: OnboardingInput, wiki_url: str, wiki_page_id: str = "") -> None:
    """Fire email and/or Slack notification after wiki creation."""
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


def notify_light_refresh(hire: OnboardingInput, wiki_url: str) -> None:
    """Slack DM: wiki lightly refreshed."""
    if not hire.slack_handle:
        return
    try:
        _send_slack_dm(
            hire,
            f"Hi {hire.name}! Your onboarding wiki has been updated with the latest "
            f"open issues and recent PRs.\n\n{wiki_url}",
        )
        logger.info("Light refresh DM sent to @%s", hire.slack_handle)
    except Exception:
        logger.exception("notify_light_refresh failed for %s", hire.name)


def notify_full_refresh(hire: OnboardingInput, new_wiki_url: str, new_wiki_page_id: str = "") -> None:
    """Slack DM: wiki fully refreshed."""
    if not hire.slack_handle:
        return
    try:
        user_id = _send_slack_dm(
            hire,
            f"Hi {hire.name}! Your onboarding wiki has been fully refreshed with the "
            f"latest codebase context. Your previous wiki has been archived.\n\n"
            f"New wiki: {new_wiki_url}",
        )
        logger.info("Full refresh DM sent to @%s", hire.slack_handle)
        if user_id and new_wiki_page_id:
            _update_slack_wiki_map(user_id, new_wiki_page_id)
    except Exception:
        logger.exception("notify_full_refresh failed for %s", hire.name)


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
# Slack helpers
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
    SlackApiError = _get_slack_api_error()
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
    except Exception:
        logger.exception("Slack users_list failed for %s", hire.name)
        return None
    if not user_id:
        logger.warning("Could not resolve @%s to a Slack user ID", hire.slack_handle)
    return user_id


def _send_slack_dm(hire: OnboardingInput, message: str) -> str | None:
    """
    Resolve user, send a Slack DM.  Returns user_id on success, None on failure.

    Centralises the repeated resolve → post pattern.
    """
    client = _slack_client()
    if not client:
        logger.warning("Slack notification skipped for %s — SLACK_BOT_TOKEN not set", hire.name)
        return None

    user_id = _resolve_slack_user_id(hire, client)
    if not user_id:
        return None

    try:
        client.chat_postMessage(channel=user_id, text=message)
    except Exception:
        logger.exception("Slack chat_postMessage failed for %s", hire.name)
        return None

    return user_id


def _send_slack(hire: OnboardingInput, wiki_url: str, wiki_page_id: str = "") -> None:
    """Send the initial onboarding notification DM."""
    message = (
        f"Hi {hire.name}! Your onboarding wiki is ready: {wiki_url}\n\n"
        f"It covers your repos, getting started, good first issues, and team conventions.\n\n"
        f"Reply to this message to ask me anything about your codebase."
    )
    user_id = _send_slack_dm(hire, message)
    if user_id:
        logger.info("Slack DM sent to @%s (%s)", hire.slack_handle, hire.name)
        if wiki_page_id:
            _update_slack_wiki_map(user_id, wiki_page_id)


def _update_slack_wiki_map(user_id: str, wiki_page_id: str) -> None:
    """Write user_id → wiki_page_id to data/slack_wiki_map.json."""
    data_dir = DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)
    map_path = data_dir / "slack_wiki_map.json"
    mapping: dict = {}
    if map_path.exists():
        try:
            mapping = json.loads(map_path.read_text())
        except Exception:
            pass
    mapping[user_id] = wiki_page_id
    map_path.write_text(json.dumps(mapping, indent=2))
    logger.info("Slack wiki mapping saved: %s → %s", user_id, wiki_page_id)
