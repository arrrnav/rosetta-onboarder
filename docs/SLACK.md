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
Take note of the **Bot User OAuth Token** (`xoxb-...`).

---

## 4. Enable Socket Mode

**Settings** → **Socket Mode** → toggle on.

Then **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**.
Name it (e.g. `rosetta-socket`), add the `connections:write` scope, generate, and take note of the token (`xapp-...`).

---

## 5. Enable App Home messaging

**App Home** → **Show Tabs** → check **"Allow users to send Slash commands and messages from the messages tab"**.

Without this, the message input box in the DM is disabled.

---

## 6. Subscribe to DM events

**Event Subscriptions** → toggle on → **Subscribe to bot events** → **Add Bot User Event** → `message.im`. Save changes.

Reinstall the app when prompted.

---

## 7. Note env vars

Remember to take note of both tokens for Rosetta setup:

```
Bot User OAuth Token = xoxb-...
App Token = xapp-...
```

---

## Troubleshooting

**"Sending messages to this app has been turned off"** — Step 5 is missing. Enable App Home messaging, then close and reopen the DM in Slack.

**Bot does not respond** — `rosetta serve` must be running. DMs sent while it is offline are not replayed.

**"No onboarding wiki found for your account"** — Your Slack user ID is not mapped to a wiki. Re-run onboard with your Slack handle in the DB row.
