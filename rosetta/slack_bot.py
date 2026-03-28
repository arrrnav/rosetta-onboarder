"""
Slack bot — Socket Mode RAG chat (Milestone 4).

New hires receive a DM when their wiki is created. Replies to that DM are
answered using the same Gemini RAG pipeline as the HTTP chat endpoint.

Socket Mode is used so no public URL or ngrok tunnel is needed for the bot —
it connects outbound to Slack's WebSocket endpoint using the app-level token.

Required env vars:
    SLACK_APP_TOKEN   — xapp-... App-level token with connections:write scope
    SLACK_BOT_TOKEN   — xoxb-... Bot token (same as used for notifications)

Required Slack app scopes (in addition to chat:write + users:read):
    im:history   — read messages in DM channels
    im:write     — open DM channels
    im:read      — list DM channels

Socket Mode must be enabled in the Slack app settings.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a helpful onboarding assistant for a new software engineer.
Answer questions about their assigned repositories, codebase setup,
team conventions, and onboarding wiki using the provided context.

Be concise, friendly, and practical. Use numbered lists for sequential
steps, bullet lists for non-ordered items, and code blocks for commands
or code snippets. If the answer isn't in the context, say so honestly
and suggest where the engineer might find more information (e.g. README,
docs, or asking a teammate).

Format responses using Markdown with these rules:
- **Bold** for emphasis and section labels instead of headers — # and ## are not supported
- Numbered lists for sequential steps, bullet lists for non-ordered items
- `inline code` for commands, file names, and identifiers
- Fenced code blocks for multi-line code or terminal output
- > blockquotes for important notes
- Italic and ~~strikethrough~~ are supported\
"""


def _md_to_mrkdwn(text: str) -> str:
    """Convert Markdown formatting to Slack mrkdwn."""
    import re
    # ## Heading → *Heading* (Slack doesn't render Markdown headings)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    # **bold** and __bold__ → *bold*  (must come before single-* pass)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"*\1*", text)
    # *italic* → _italic_  (single * not preceded/followed by another *)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"_\1_", text)
    # ~~strikethrough~~ → ~strikethrough~
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    # [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
    return text


def _load_mapping(data_dir: Path) -> dict[str, str]:
    """Load the Slack user_id → wiki_page_id mapping from disk."""
    path = data_dir / "slack_wiki_map.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        logger.warning("Could not read slack_wiki_map.json")
        return {}


async def start_bot(data_dir: Path) -> None:
    """
    Start the async Socket Mode handler.

    Runs until cancelled (e.g. when rosetta serve shuts down).
    Skips silently if SLACK_APP_TOKEN or SLACK_BOT_TOKEN are not set.
    """
    app_token = os.getenv("SLACK_APP_TOKEN", "")
    bot_token = os.getenv("SLACK_BOT_TOKEN", "")

    if not app_token or not bot_token:
        logger.info(
            "Slack bot not started — SLACK_APP_TOKEN and/or SLACK_BOT_TOKEN not set"
        )
        return

    try:
        from slack_sdk.web.async_client import AsyncWebClient
        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse
    except ImportError:
        logger.warning(
            "Slack bot not started — slack-sdk not installed. Run: pip install slack-sdk"
        )
        return

    web_client = AsyncWebClient(token=bot_token)

    # Fetch bot's own user ID so we can ignore our own messages
    try:
        auth = await web_client.auth_test()
        bot_user_id = auth["user_id"]
    except Exception:
        logger.exception("Slack auth_test failed — bot not started")
        return

    async def _process_event(client: SocketModeClient, req: SocketModeRequest) -> None:
        # Acknowledge immediately so Slack doesn't retry
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

        if req.type != "events_api":
            return

        event = req.payload.get("event", {})

        # Only handle DMs (channel_type == "im"), ignore bot's own messages
        if event.get("type") != "message":
            return
        if event.get("channel_type") != "im":
            return
        if event.get("subtype"):  # bot_message, message_changed, etc.
            return
        if event.get("user") == bot_user_id:
            return

        user_id = event.get("user", "")
        channel = event.get("channel", "")
        question = event.get("text", "").strip()

        if not question:
            return

        # Look up which wiki belongs to this user
        mapping = _load_mapping(data_dir)
        wiki_page_id = mapping.get(user_id)

        if not wiki_page_id:
            await web_client.chat_postMessage(
                channel=channel,
                text=(
                    "I don't have an onboarding wiki linked to your account yet. "
                    "Ask your team lead to generate one for you."
                ),
            )
            return

        # Load VectorStore
        pkl_path = data_dir / f"{wiki_page_id}.pkl"
        if not pkl_path.exists():
            await web_client.chat_postMessage(
                channel=channel,
                text="Your wiki exists but the chat index isn't ready yet. Try again in a minute.",
            )
            return

        # Post placeholder while processing
        placeholder = await web_client.chat_postMessage(
            channel=channel,
            text="_Rosetta is thinking..._",
        )
        placeholder_ts = placeholder["ts"]

        try:
            from .embeddings import VectorStore
            store = VectorStore.load(pkl_path)
            chunks = store.retrieve(question, top_k=3)
            context = "\n\n---\n\n".join(chunks)
        except Exception:
            logger.exception("RAG retrieval failed for user %s", user_id)
            await web_client.chat_update(
                channel=channel,
                ts=placeholder_ts,
                text="Sorry, I ran into an error retrieving context. Please try again.",
            )
            return

        # Generate answer with Claude
        try:
            claude = anthropic.Anthropic()
            model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
            response = claude.messages.create(
                model=model,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"Context from the onboarding wiki:\n\n{context}\n\n"
                            f"Question: {question}"
                        ),
                    }
                ],
            )
            answer = response.content[0].text
        except Exception:
            logger.exception("Claude response failed for user %s", user_id)
            await web_client.chat_update(
                channel=channel,
                ts=placeholder_ts,
                text="Sorry, I couldn't generate a response right now. Please try again.",
            )
            return

        await web_client.chat_update(
            channel=channel,
            ts=placeholder_ts,
            text=_md_to_mrkdwn(answer),
        )
        logger.info("Slack bot answered question for user %s (wiki %s)", user_id, wiki_page_id)

    socket_client = SocketModeClient(app_token=app_token, web_client=web_client)
    socket_client.socket_mode_request_listeners.append(_process_event)

    logger.info("Slack bot starting (Socket Mode)…")
    await socket_client.connect()
    logger.info("Slack bot connected.")

    # Keep running until cancelled
    import asyncio
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await socket_client.close()
        logger.info("Slack bot stopped.")
