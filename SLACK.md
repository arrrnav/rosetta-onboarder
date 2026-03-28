# Slack Bot Setup

One-time setup to enable Rosetta's Slack notifications and in-DM chat.

---

## 1. Create the app

Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**.
Name it (e.g. "Rosetta") and select your workspace.

---

## 2. Add bot token scopes

**OAuth & Permissions** → **Bot Token Scopes** → Add:

| Scope | Purpose |
|---|---|
| `chat:write` | Send DMs |
| `users:read` | Resolve Slack handles to user IDs |
| `im:write` | Open DM channels |
| `im:read` | Read DM metadata |
| `im:history` | Receive DM message events |

---

## 3. Install to workspace

**OAuth & Permissions** → **Install to Workspace**.
Copy the **Bot User OAuth Token** (`xoxb-...`) into `SLACK_BOT_TOKEN` in your `.env`.

---

## 4. Enable Socket Mode

**Settings** → **Socket Mode** → toggle on.

Then **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**.
Name it (e.g. `rosetta-socket`), add the `connections:write` scope, generate, and copy the token (`xapp-...`) into `SLACK_APP_TOKEN`.

---

## 5. Enable App Home messaging

**App Home** → **Show Tabs** → check **"Allow users to send Slash commands and messages from the messages tab"**.

Without this, the message input box in the DM is disabled.

---

## 6. Subscribe to DM events

**Event Subscriptions** → toggle on → **Subscribe to bot events** → **Add Bot User Event** → `message.im`. Save changes.

Reinstall the app when prompted.

---

## 7. Set env vars

Paste both tokens into your secrets `.env`:

```
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

---

## 8. Test

```bash
rosetta serve
```

Look for `Slack bot connected.` in the logs. Open a DM with the bot — you should see a message input box. Send a message; if a wiki has been generated for your Slack user, the bot will answer using RAG context.

---

## Troubleshooting

**"Sending messages to this app has been turned off"** — Step 5 is missing. Enable App Home messaging, then close and reopen the DM in Slack.

**Bot does not respond** — `rosetta serve` must be running. DMs sent while it is offline are not replayed.

**"No onboarding wiki found for your account"** — Your Slack user ID is not mapped to a wiki. Re-run onboard with your Slack handle in the DB row.
