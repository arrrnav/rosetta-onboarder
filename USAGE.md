# Usage Guide

Setup, configuration, and day-to-day usage for Rosetta Onboarder.

---

## Prerequisites

- Python 3.13+
- Node.js (for `notion-mcp-server`)
- [ngrok](https://ngrok.com/download) (for the Notion webhook endpoint — not needed for chat)
- A Notion account and a Slack workspace

---

## Installation

```bash
pip install -e .
npm install -g notion-mcp-server
```

---

## Configuration

Two `.env` files are used. Secrets live outside the repo to keep them out of version control.

**`.env`** (secret keys — lives outside the repo)
```
ANTHROPIC_API_KEY=sk-ant-...
NOTION_TOKEN=ntn_...
GITHUB_TOKEN=github_pat_...
GEMINI_API_KEY=AIza...
NOTION_WEBHOOK_SECRET=<verification token from webhook setup>
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

**`.env`** (project config — lives in the repo root)
```
NOTION_ONBOARDING_PAGE_ID=...
NOTION_DATABASE_ID=...
CHAT_SERVER_URL=https://<ngrok-url>   # only needed for webhook auto-trigger
CHAT_DATA_DIR=data
CLAUDE_MODEL=claude-haiku-4-5-20251001
GITHUB_MAX_ISSUES=3
GITHUB_MAX_PRS=2
GITHUB_TREE_DEPTH=1
```

### Environment variable reference

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key |
| `NOTION_TOKEN` | Yes | Notion internal integration token (`ntn_...`) |
| `GITHUB_TOKEN` | Recommended | GitHub PAT — needed for private repos, raises rate limit from 60 to 5000 req/hr |
| `GEMINI_API_KEY` | Yes (for chat) | Google AI Studio key — free tier is sufficient |
| `NOTION_WEBHOOK_SECRET` | Yes (for auto-trigger) | Verification token captured during webhook setup |
| `SLACK_BOT_TOKEN` | Yes (for notifications + chat) | Slack bot OAuth token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Yes (for in-DM chat) | Slack app-level token (`xapp-...`) with `connections:write` — enables Socket Mode bot in `rosetta serve` |
| `NOTION_ONBOARDING_PAGE_ID` | Yes | Page ID of the top-level "Engineering Onboarding" page |
| `NOTION_DATABASE_ID` | Yes | Page ID of the "New Hire Requests" database |
| `CHAT_SERVER_URL` | Yes (for auto-trigger) | Public ngrok URL pointing to this server — only needed for the Notion webhook endpoint |
| `CHAT_DATA_DIR` | No | Directory for vector store pickles (default: `data`) |
| `CLAUDE_MODEL` | No | Claude model to use (default: `claude-haiku-4-5-20251001`) |
| `GITHUB_MAX_ISSUES` | No | Max issues to fetch per repo (default: `3`) |
| `GITHUB_MAX_PRS` | No | Max PRs to fetch per repo (default: `2`) |
| `GITHUB_TREE_DEPTH` | No | Directory tree depth (default: `1`) |

---

## Notion workspace setup (one-time)

**1. Create the pages**

Duplicate the [public page link](https://www.notion.so/Engineering-Onboarding-32ef78cab142810d8353ca62c3a9e6ae?source=copy_link). End goal is for this to be a publicly available template on the Marketplace.

Alternatively, in Notion manually create a top-level page called **Engineering Onboarding**. Inside it, create a database called **New Hire Requests** with the following schema:

| Property | Type | Purpose |
|---|---|---|
| Name | title | New hire's full name |
| Role | rich_text | e.g. "Backend Engineer" |
| GitHub Repos | rich_text | One GitHub URL per line |
| Notes | rich_text | Additional context for the agent |
| Status | select | `Pending` → `Ready` → `Processing` → `Done` |
| Wiki URL | url | Written back by agent after generation |
| Contact Email | email | Optional |
| Slack Handle | rich_text | Optional |

![Engineering Onboarding page](docs/img/notion_onboarding_page.png)
---
![New Hire Requests database](docs/img/notion_new_hire_DB.png)

**2. Grant the integration content access**

Go to [notion.so/profile/integrations](https://notion.so/profile/integrations) → your integration → **Content access** tab. Add the **Engineering Onboarding** page. The database inside it is automatically included.

![Integration setup — Content access tab](docs/img/integration_setup.png)

![Engineering Onboarding page linked to integration](docs/img/link_page_to_integration.png)

**3. Set the page to public**

On the Engineering Onboarding page: **Share → Share to web → Anyone with the link can view**.

Child wiki pages inherit this setting — do it once and all generated wikis will be publicly accessible without a Notion account.

![Sharing settings](docs/img/sharing_settings.png)

**4. Copy the page IDs**

The page ID is the UUID at the end of the Notion URL. Set `NOTION_ONBOARDING_PAGE_ID` and `NOTION_DATABASE_ID` in your `.env`.

---

## Slack app setup (one-time)

The Slack bot sends DM notifications and answers new hire questions via Socket Mode — no public URL required.

**1. Create a Slack app**

Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**. Name it (e.g. "Rosetta Onboarder") and select your workspace.

**2. Add bot token scopes**

In the app settings: **OAuth & Permissions** → **Bot Token Scopes**. Add:

| Scope | Purpose |
|---|---|
| `chat:write` | Send DMs |
| `users:read` | Resolve Slack handles to user IDs |
| `im:write` | Open DM channels |
| `im:read` | Read DM metadata |
| `im:history` | Read DM history (required for Socket Mode message events) |

**3. Install to workspace**

**OAuth & Permissions** → **Install to Workspace**. Copy the **Bot User OAuth Token** (`xoxb-...`) into `SLACK_BOT_TOKEN`.

**4. Enable Socket Mode**

**Settings** → **Socket Mode** → toggle on. Then **Basic Information** → **App-Level Tokens** → **Generate Token and Scopes**. Name it (e.g. `rosetta-socket`), add the `connections:write` scope, generate, and copy the token (`xapp-...`) into `SLACK_APP_TOKEN`.

**5. Enable Event Subscriptions**

**Event Subscriptions** → toggle on → **Subscribe to bot events** → add `message.im`. Save changes.

---

## Webhook setup (one-time)

The webhook lets Rosetta auto-trigger when a DB row is set to Ready. Requires `rosetta serve` and ngrok to be running.

1. Start ngrok: `ngrok http 8000` — copy the `https://` URL into `CHAT_SERVER_URL` in `.env`
2. Start the server: `rosetta serve`
3. Go to [notion.so/profile/integrations](https://notion.so/profile/integrations) → your integration → **Webhooks**
4. Add webhook URL: `{CHAT_SERVER_URL}/webhook/notion`
5. Subscribe to `page.properties_updated` only — uncheck all other events

![Webhook setup — subscribing to page.properties_updated](docs/img/webhook_setup.png)

6. Click **Create subscription** — Notion immediately POSTs a `verification_token` to your endpoint
7. Copy the token from the `rosetta serve` terminal logs (look for `NOTION WEBHOOK VERIFICATION TOKEN: ...`)
8. Paste it into the Notion verification form and submit
9. Set `NOTION_WEBHOOK_SECRET=<that token>` in your secrets `.env` and restart `rosetta serve`

The verification token is also the HMAC-SHA256 signing key used to authenticate all future webhook payloads.

**Note:** ngrok free tier assigns a new URL each session. When you restart ngrok, update `CHAT_SERVER_URL` in `.env` and the webhook URL in the Notion dashboard, then restart `rosetta serve`.

---

## Running

**Every session (with webhook auto-trigger):**

```bash
# Terminal 1 — public tunnel for the Notion webhook endpoint
ngrok http 8000
# Update CHAT_SERVER_URL in .env with the new ngrok URL

# Terminal 2 — chat server + webhook listener + Slack bot
rosetta serve
```

`rosetta serve` starts three things in one process: the uvicorn HTTP server, the Notion webhook listener, and the Slack Socket Mode bot (if `SLACK_APP_TOKEN` is set). The Slack bot connects outbound to Slack — no ngrok needed for it.

**Manual onboard (no ngrok needed):**

```bash
rosetta onboard <notion-page-id>
```

The page ID is the UUID at the end of the DB row's Notion URL. Runs the full flow — wiki generation, embeddings, Slack DM — without needing a running server or public URL.

**Automatic onboard (webhook-triggered):**

Set a DB row's Status to `Ready`. Rosetta picks it up via the Notion webhook and generates the wiki automatically. The row status updates to `Processing` then `Done`, with the Wiki URL written back.

---

## Slack chat

After wiki generation, the new hire receives a Slack DM with their wiki link and an invitation to ask questions. They reply directly in that DM thread — no browser, no Notion account required.

- `rosetta serve` must be running with `SLACK_APP_TOKEN` set for the bot to respond
- The bot answers using RAG over the generated wiki and README content (top-3 chunks, Gemini embeddings)
- If `GEMINI_API_KEY` is not set, embeddings are skipped and the bot will not have wiki context
- The `Slack Handle` field on the DB row must be filled in (with or without the leading `@`) to send the DM
