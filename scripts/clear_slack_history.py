"""
Clear all messages sent by the Rosetta bot — useful before a demo.

Finds every DM conversation the bot is part of, fetches all messages
posted by the bot itself, and deletes them.

Usage:
    python scripts/clear_slack_history.py

Requires SLACK_BOT_TOKEN in the environment (or .env file).
Required bot scopes: im:history, im:read, chat:write
"""
from __future__ import annotations

import os
import sys
import time

from dotenv import load_dotenv

load_dotenv(override=True)

token = os.getenv("SLACK_BOT_TOKEN", "")
if not token:
    print("ERROR: SLACK_BOT_TOKEN is not set.")
    sys.exit(1)

try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
except ImportError:
    print("ERROR: slack-sdk is not installed. Run: pip install slack-sdk")
    sys.exit(1)

client = WebClient(token=token)

# ------------------------------------------------------------------
# 1. Find the bot's own user ID
# ------------------------------------------------------------------
auth = client.auth_test()
bot_user_id: str = auth["user_id"]
print(f"Signed in as bot user: {bot_user_id} ({auth.get('user')})")

# ------------------------------------------------------------------
# 2. List all DM (im) conversations the bot is in
# ------------------------------------------------------------------
print("\nFetching DM conversations...")
conversations: list[dict] = []
cursor: str | None = None
while True:
    kwargs: dict = {"types": "im", "limit": 200}
    if cursor:
        kwargs["cursor"] = cursor
    resp = client.conversations_list(**kwargs)
    conversations.extend(resp["channels"])
    cursor = resp.get("response_metadata", {}).get("next_cursor")
    if not cursor:
        break

print(f"Found {len(conversations)} DM conversation(s).")

# ------------------------------------------------------------------
# 3. For each DM, fetch and delete bot messages
# ------------------------------------------------------------------
total_deleted = 0
total_failed = 0

for conv in conversations:
    channel_id: str = conv["id"]

    # Fetch all messages in this DM
    messages: list[dict] = []
    cursor = None
    while True:
        kwargs = {"channel": channel_id, "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = client.conversations_history(**kwargs)
        except SlackApiError as e:
            print(f"  Could not fetch history for {channel_id}: {e.response['error']}")
            break
        messages.extend(resp.get("messages", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    # Filter to messages posted by this bot
    bot_messages = [m for m in messages if m.get("bot_id") or m.get("user") == bot_user_id]

    if not bot_messages:
        continue

    print(f"\nChannel {channel_id}: deleting {len(bot_messages)} bot message(s)...")
    for msg in bot_messages:
        ts = msg["ts"]
        try:
            client.chat_delete(channel=channel_id, ts=ts)
            total_deleted += 1
            # Slack rate-limits chat.delete to ~50 req/min on free workspaces
            time.sleep(0.5)
        except SlackApiError as e:
            err = e.response.get("error", "unknown")
            print(f"  Failed to delete message {ts}: {err}")
            total_failed += 1

# ------------------------------------------------------------------
# 4. Summary
# ------------------------------------------------------------------
print(f"\nDone. Deleted {total_deleted} message(s). Failed: {total_failed}.")
