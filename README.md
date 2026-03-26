# Rosetta Onboarder

An AI agent that generates personalized onboarding wikis for new engineers — automatically, from a Notion database row.

A team lead fills in a new hire's name, role, and GitHub repos. Rosetta reads it, fetches repo context, and writes a structured wiki directly in Notion. The new hire opens their wiki and can chat with it using a built-in RAG assistant.

Built for the MLH Notion AI Challenge.

---

## How it works

```
Team lead fills DB row → sets Status = Ready
  → Notion webhook fires → rosetta serve receives it
    → Claude agent fetches GitHub context (README, structure, issues, PRs)
    → Claude writes wiki to Notion (8 sections)
    → Gemini embeds wiki for RAG
    → Chat widget embedded in wiki page
  → Status = Done, Wiki URL written back to DB row
New hire opens wiki → asks questions → Claude answers using RAG
```

---

## Stack

| Component | Technology |
|---|---|
| Orchestration agent | Claude (Anthropic SDK) |
| Notion read/write | `@notionhq/notion-mcp-server` via stdio MCP |
| GitHub data | PyGithub |
| Embeddings | Gemini `gemini-embedding-2-preview` (multimodal) |
| Chat server | FastAPI + uvicorn |
| Vector store | numpy cosine similarity (in-memory, no external DB) |
| Public tunnel | ngrok |

---

## Notion workspace layout

```
Engineering Onboarding        ← top-level page, manually created once
├── New Hire Requests         ← database (NOTION_DATABASE_ID)
│   ├── Jane Smith            [Status: Done | Wiki URL: ...]
│   └── John Doe              [Status: Ready]
├── Jane Smith's Onboarding Wiki   ← created by agent
└── John Doe's Onboarding Wiki
```

The "Engineering Onboarding" page must be set to **Share to web → Anyone with the link can view** once so all child wikis are publicly accessible (new hire doesn't need a Notion account).

---

## New Hire Requests database schema

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

---

## Wiki sections generated

1. Welcome & Overview
2. Your Repositories
3. Codebase Architecture
4. Getting Started
5. Good First Issues
6. Team Conventions
7. Recent Activity
8. Resources & Links

---

## Prerequisites

- Python 3.13+
- Node.js (for `notion-mcp-server`)
- ngrok (for public webhook/chat URL)

```bash
npm install -g notion-mcp-server
pip install -e .
```

---

## Environment variables

Two `.env` files are used. Secrets live outside the repo:

**`A:\Programming\.env`** (outside repo — never committed)
```
ANTHROPIC_API_KEY=sk-ant-...
NOTION_TOKEN=ntn_...
GITHUB_TOKEN=github_pat_...
GEMINI_API_KEY=AIza...
```

**`.env`** (project config — committed, no secrets)
```
NOTION_ONBOARDING_PAGE_ID=...
NOTION_DATABASE_ID=...
CHAT_SERVER_URL=https://<ngrok-url>
CHAT_DATA_DIR=data
NOTION_WEBHOOK_SECRET=
CLAUDE_MODEL=claude-haiku-4-5-20251001
GITHUB_MAX_ISSUES=3
GITHUB_MAX_PRS=2
GITHUB_TREE_DEPTH=1
```

---

## One-time setup

**1. Notion workspace**

Create the "Engineering Onboarding" page and "New Hire Requests" database manually with the schema above. Add your Rosetta integration as a connection on both.

Set the Engineering Onboarding page to **Share → Share to web → Anyone with the link can view**. Child wiki pages inherit this — do it once and forget it.

**2. Webhook**

With `rosetta serve` and ngrok running:

1. Go to [notion.so/profile/integrations](https://notion.so/profile/integrations) → your integration → Webhooks
2. Add webhook URL: `{CHAT_SERVER_URL}/webhook/notion`
3. Subscribe to `page.properties_updated` only
4. Click Verify — copy the `NOTION WEBHOOK VERIFICATION TOKEN` from the `rosetta serve` logs
5. Paste it into the Notion verification form
6. Set `NOTION_WEBHOOK_SECRET=<that token>` in `.env` and restart `rosetta serve`

The verification token is also the HMAC-SHA256 signing secret used to authenticate all future webhook payloads.

---

## Usage

**Start the server** (run once, keep running):
```bash
ngrok http 8000          # terminal 1 — copy the https:// URL into CHAT_SERVER_URL in .env
rosetta serve            # terminal 2
```

**Manual onboard** (one hire by DB row ID):
```bash
rosetta onboard <notion-page-id>
```

**Automatic onboard** (webhook-triggered):

Set a DB row's Status to `Ready` — Rosetta picks it up automatically via the Notion webhook.

---

## Claude tool loop

Claude is given 7 tools and iterates until it calls `create_notion_wiki`:

| Tool | What it fetches |
|---|---|
| `fetch_github_metadata` | Repo description, language, stars |
| `fetch_github_readme` | Full README content |
| `fetch_github_structure` | Directory tree |
| `fetch_github_issues` | Open issues (filterable by label) |
| `fetch_github_prs` | Recent pull requests |
| `fetch_github_contributing` | CONTRIBUTING.md if present |
| `create_notion_wiki` | Writes the final wiki to Notion |

Each iteration = one Claude API call. Claude decides which tools to call and in what order based on the repo — smaller repos finish in 2–3 iterations, larger ones in 5–8.

---

## Chat interface (RAG)

After wiki generation, the wiki page gets an embedded chat widget (iframe). No Notion account required for the new hire.

- Wiki sections are embedded with Gemini `gemini-embedding-2-preview` (multimodal — also handles README images)
- Embeddings stored in `data/<wiki_page_id>.pkl`
- Chat query → embed → cosine similarity → top-3 chunks → Claude generates answer
- Chat server caches vector stores in memory after first load

Gemini embedding API is free tier (AI Studio) for typical usage volumes.

---

## Key design decisions

**MCP via stdio, not HTTP** — `@notionhq/notion-mcp-server` uses stdio transport with an internal integration token. The HTTP endpoint requires OAuth, which doesn't suit headless automation.

**Claude decides what to fetch** — rather than front-loading all GitHub data, Claude calls tools on demand. This handles repos of varying complexity without wasting tokens on data that isn't needed.

**Gemini for embeddings, Claude for generation** — Gemini's multimodal embeddings handle architecture diagrams and screenshots in the same vector space as text. Claude handles answer generation where reasoning quality matters.

**No external vector DB** — numpy cosine similarity over a single wiki's chunks is sufficient. Keeps the stack simple and dependency-free.

**Webhook over polling** — Notion's `page.properties_updated` webhook fires within ~1 minute of a Status change. No long-running poll loop needed; `rosetta serve` handles both chat and webhook triggers in one process.
