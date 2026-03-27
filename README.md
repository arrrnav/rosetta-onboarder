# Rosetta Onboarder

> [!NOTE] 
> Built for the MLH Notion AI Challenge.

Onboarding new engineers is repetitive work. A team lead writes the same wiki every time: here are the repos, here is how to run them, here are the open issues to start with, here is how we review code. The content is different per hire but the structure is always the same — and every piece of it already exists somewhere: in the GitHub README, in the issue tracker, in the PR history.

Rosetta automates this. A team lead fills in a Notion database row (name, role, GitHub repos, any notes). Rosetta reads it, fetches the relevant GitHub context, and uses Claude to write a structured onboarding wiki directly in Notion. The new hire gets a Slack DM with their wiki link and can ask questions about the codebase by replying directly in Slack — no extra tools, no browser tabs.

The intended end state is a one-click Notion template — a team lead duplicates the template into their own Notion, clones the repo, drops in their API keys, and Rosetta is running. No manual database construction, no figuring out which properties to create.

---

## How it works

```
Team lead fills DB row → sets Status = Ready
  → Notion webhook fires → rosetta serve receives it
    → Claude agent fetches GitHub context (README, structure, issues, PRs)
    → Claude writes wiki to Notion (8 sections)
    → Gemini embeds wiki + README content for RAG
    → Slack DM sent to new hire: wiki link + "reply here to ask questions"
  → Status = Done, Wiki URL written back to DB row
New hire replies to the Slack DM → Claude answers using RAG context
```

See [USAGE.md](USAGE.md) for setup and configuration.

---

## Stack

| Component | Technology |
|---|---|
| Orchestration agent | Claude Haiku 4.5 (Anthropic SDK) |
| Notion read/write | `@notionhq/notion-mcp-server` via stdio MCP |
| GitHub data | PyGithub |
| Embeddings | Gemini `gemini-embedding-2-preview` (multimodal) |
| Chat | Slack SDK (Socket Mode bot) |
| Chat server | FastAPI + uvicorn |
| Vector store | numpy cosine similarity (in-memory, no external DB) |
| Webhook tunnel | ngrok (dev only — webhook endpoint only) |

*Note: Claude Haiku and in-memory pkl vectors are for demo only. At scale: swap in a more powerful Claude model, a dedicated vector DB, and a permanently deployed server (Fly.io, Cloud Run, or Lambda+API Gateway) to replace ngrok. The Slack bot uses Socket Mode — outbound WebSocket to Slack — so it needs no public URL at any scale.*

---

## Architecture

Three layers, each independently replaceable:

**1. Ingestion** — `rosetta/github/fetcher.py`
PyGithub wrappers that fetch README, directory tree, open issues, recent PRs, and CONTRIBUTING.md on demand. Claude calls these as tools rather than front-loading all data — smaller repos finish faster, larger repos get more context automatically.

**2. Generation** — `rosetta/agent.py` + `rosetta/notion/mcp_session.py`
Claude runs an agentic loop with 7 tools. It decides what to fetch, in what order, and when it has enough to write. The final tool call (`create_notion_wiki`) hands structured sections to the MCP session, which writes them to Notion via the stdio MCP server.

**3. Chat** — `rosetta/embeddings.py` + `rosetta/slack_bot.py`
After wiki creation, each section and README image is embedded with Gemini's multimodal model and stored as a numpy vector store on disk. When `rosetta serve` starts with `SLACK_APP_TOKEN` set, it opens a Socket Mode WebSocket to Slack alongside uvicorn. When the new hire replies to their onboarding DM, the bot embeds the query, retrieves the top-3 chunks by cosine similarity, and answers via Claude — all without a public URL.

---

## How Rosetta uses Notion

**Internal integration**

Rosetta uses a Notion internal integration — scoped to one workspace, authenticated with a bearer token, no OAuth flow. Internal integrations are right for self-hosted tools: the team owns the token, data never leaves their Notion, and setup is a single bearer token in an env file. The integration needs three content capabilities: read, update, and insert. Access is granted once from the integration's Content access tab — adding the top-level Engineering Onboarding page automatically includes the database inside it.

**MCP via stdio**

Rosetta writes to Notion through `@notionhq/notion-mcp-server`, an npm package that exposes the Notion REST API as MCP tools over stdio transport. Each tool maps directly to a Notion API endpoint with an `API-` prefix (`API-retrieve-a-page`, `API-post-page`, `API-patch-block-children`, etc.). Python's `mcp` library spawns the server as a subprocess and handles the JSON-RPC plumbing. The HTTP MCP endpoint (`mcp.notion.com`) was considered and rejected — it requires OAuth, which is unsuitable for headless automation.

**Webhooks**

When a team lead sets a row to Ready, Notion fires a `page.properties_updated` event to a registered endpoint. `rosetta serve` handles this alongside chat requests — one process, one public URL. Notion's verification flow sends a `verification_token` to the endpoint on subscription creation; that same token becomes the HMAC-SHA256 signing key for all subsequent payloads. Rosetta verifies the signature on every incoming request to ensure it originated from Notion before doing any work.

---

## What gets generated

Eight wiki sections, written by Claude based on the actual repo content:

1. Welcome & Overview
2. Your Repositories
3. Codebase Architecture
4. Getting Started
5. Good First Issues
6. Team Conventions
7. Recent Activity
8. Resources & Links

Claude is given 7 tools and decides which to call and in what order:

| Tool | What it fetches |
|---|---|
| `fetch_github_metadata` | Repo description, language, stars |
| `fetch_github_readme` | Full README content |
| `fetch_github_structure` | Directory tree |
| `fetch_github_issues` | Open issues, filterable by label |
| `fetch_github_prs` | Recent pull requests |
| `fetch_github_contributing` | CONTRIBUTING.md if present |
| `create_notion_wiki` | Writes the final wiki to Notion |

Each agent iteration is one Claude API call. Typical runs complete in 3–6 iterations.

---

## Design decisions

**MCP via stdio, not HTTP**

The Notion HTTP MCP endpoint (`mcp.notion.com`) requires OAuth — not suitable for headless automation. `@notionhq/notion-mcp-server` uses stdio transport and accepts a bearer token directly, which works cleanly with internal integrations and `subprocess` plumbing.

**Claude decides what to fetch**

Passing all GitHub data upfront wastes tokens on repos where most of it isn't relevant. Claude calls tools on demand: if there's no CONTRIBUTING.md, it doesn't call `fetch_github_contributing`; if the repo is tiny, it skips issues entirely. This keeps costs proportional to repo complexity.

**Gemini for embeddings, Claude for generation**

Gemini's `gemini-embedding-2-preview` maps text and images into the same vector space, so architecture diagrams and screenshots in the README are embedded alongside text sections without any special handling. Claude handles answer generation where reasoning quality matters more than embedding speed. Outside of the demo, a better model would be used in lieu of Haiku 4.5.

**No external vector DB (for now)**

Each wiki has ~8 sections — numpy cosine similarity over a handful of vectors is sufficient. At scale, the right approach is to embed the raw repo content (READMEs, code structure, docs) alongside wiki sections for richer retrieval, at which point a dedicated vector DB becomes the correct choice.

**Webhook over polling**

Notion's `page.properties_updated` webhook fires within ~1 minute of a Status change. `rosetta serve` handles both chat requests and webhook triggers in one process on one public URL — no separate long-running poll loop, no cron job, no second process to manage.

**Signed webhook payloads**

Without signature verification, anyone who discovers the webhook URL could POST fake events to trigger wiki generation — burning API credits and spamming the workspace. Every payload from Notion is signed with HMAC-SHA256, so the server can reject anything that didn't originate from Notion before doing any work.

**Slack Socket Mode over Notion iframe**

The original design embedded a chat widget as an iframe in the wiki page. This hit hard limits: Notion doesn't expose iframe dimensions via the API, the ngrok URL had to be kept in sync in two places every session, and the page had to be made public to load the widget. Socket Mode opens an outbound WebSocket from the server to Slack — no inbound connection, no public URL, no per-session config. New hires are already in Slack, conversation history is persistent, and the chat works on any device without touching a browser.
