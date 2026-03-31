# Rosetta Onboarder

> Built for the MLH Notion AI Challenge.

[![Demo video](https://img.youtube.com/vi/8XT58HjEju8/maxresdefault.jpg)](https://www.youtube.com/watch?v=8XT58HjEju8)
⬆️ Click to watch demo video.


Onboarding new engineers is repetitive work. A team lead writes the same wiki every time: here are the repos, here is how to run them, here are the open issues to start with, here is how we review code. The content is different per hire but the structure is always the same — and every piece of it already exists somewhere: in the GitHub README, in the issue tracker, in the PR history.

**Rosetta automates this.** A team lead fills in a Notion database row, marks it Ready, and walks away. Within minutes a personalized onboarding wiki is live in Notion, embeddings are indexed for RAG, the new hire has a Slack DM with their link, and the supervisor has a provisioning checklist.

---

## Features

- **Agentic wiki generation** — Claude researches every assigned GitHub repo on demand (README, structure, issues, PRs, CONTRIBUTING) and writes a personalized 8-section wiki directly to Notion
- **Supervisor notifications** — infers what the new hire will need provisioned (cloud access, DB credentials, internal tooling) from repo signals and DMs the supervisor a checklist
- **Context pages** — attach internal Notion docs (runbooks, ADRs, team norms) to a hire's row; Rosetta fetches and injects them into Claude's context before writing the wiki
- **Multimodal RAG** — embeds wiki sections and README images (architecture diagrams, screenshots) into the same vector space using Gemini; powers the Slack chat bot
- **Slack chat bot** — new hire can DM the bot immediately after wiki creation and ask questions about their codebase; answered by Claude with RAG context
- **Automatic weekly refreshes** — odd ISO weeks run a light update (new issues + PRs); even ISO weeks fully regenerate the wiki and archive the old one
- **Interactive setup wizard** — `rosetta setup` provisions the entire Notion workspace and walks through all config with masked previews of existing values

---

## How It Works

```
Team lead fills DB row → sets Status = Ready
  → Poller / Notion webhook fires
    → Claude agent fetches GitHub context (README, structure, issues, PRs)
    → Claude writes wiki to Notion (8 sections)
    → Gemini embeds wiki + README images for RAG
    → Slack DM → new hire (wiki link)
    → Slack DM → supervisor (provisioning checklist)
  → Status = Done, Wiki URL written back to DB row

New hire DMs the Slack bot → Claude answers using RAG context

Every Friday at 17:00 (configurable timezone):
  → Odd week:  light refresh (append new issues + PRs)
  → Even week: full regeneration (new wiki, old page archived)
```

---

## Stack

| Component | Technology |
|---|---|
| Orchestration agent | Claude `claude-sonnet-4-6` (Anthropic SDK) |
| Notion read/write | `@notionhq/notion-mcp-server` via stdio MCP |
| GitHub data | PyGithub |
| Embeddings | Gemini `gemini-embedding-2-preview` (multimodal) |
| Slack bot | slack-sdk (Socket Mode) |
| Chat server | FastAPI + Uvicorn |
| Vector store | numpy cosine similarity (no external DB) |
| CLI | Typer + Questionary |

---

## Notion Database Schema

| Property | Type | Purpose |
|---|---|---|
| Name | title | New hire's full name |
| Role | rich_text | e.g. "Backend Engineer" |
| GitHub Repos | rich_text | One GitHub URL per line |
| Agent Notes | rich_text | Context for the agent from the team lead |
| Supervisor Slack Handle | rich_text | Slack handle of the hire's direct manager |
| Context Pages | rich_text | Notion page URLs to inject as extra context |
| Status | select | Pending → Ready → Processing → Done |
| Wiki URL | url | Written back by agent after generation |
| Contact Email | email | Optional — for email notification |
| Slack Handle | rich_text | Optional — for Slack DM notification |

---

## Wiki Sections Generated

Eight sections, written by Claude based on actual repo content — not templates:

1. **Welcome & Overview** — warm, personalised intro; role context; what to expect in week 1
2. **Your Repositories** — purpose and scope of each assigned repo
3. **Codebase Architecture** — how the code is organised; key directories
4. **Getting Started** — local dev setup synthesised from READMEs; step-by-step
5. **Good First Issues** — specific issues to look at first, with context
6. **Team Conventions** — coding standards, PR process, commit style, review culture
7. **Recent Activity** — what the team has been working on (from recent PRs)
8. **Resources & Links** — links to repos, issue trackers, docs

---

## Agent Tools

Claude is given 7 tools and runs an agentic loop until it calls `create_notion_wiki`:

| Tool | What it fetches |
|---|---|
| `fetch_github_metadata` | Repo description, language, stars |
| `fetch_github_readme` | Full README content |
| `fetch_github_structure` | Directory tree (max depth 2) |
| `fetch_github_issues` | Open issues, filterable by label |
| `fetch_github_prs` | Recent pull requests |
| `fetch_github_contributing` | CONTRIBUTING.md if present |
| `create_notion_wiki` | Writes the final wiki to Notion — terminates the loop |

Typical runs complete in 3–6 iterations. Claude decides what to fetch and in what order — smaller repos finish faster, larger repos get more calls automatically.

---

## Architecture

Three layers, each independently replaceable:

**1. Ingestion** — `rosetta/github/fetcher.py`
PyGithub wrappers called on demand by Claude. No front-loading — tokens are spent proportionally to repo complexity.

**2. Generation** — `rosetta/agent.py` + `rosetta/notion/mcp_session.py`
Claude runs an agentic loop with 7 tools. The final tool call (`create_notion_wiki`) hands structured sections to the MCP session, which writes them to Notion via stdio transport. After creation, `access_requirements` (inferred by Claude from repo signals) are forwarded to the supervisor as a Slack DM.

**3. Chat** — `rosetta/embeddings.py` + `rosetta/slack_bot.py`
After wiki creation, sections and README images are embedded with Gemini's multimodal model into a per-hire numpy vector store on disk. The Slack bot (Socket Mode) runs alongside uvicorn, resolves incoming DMs to the correct wiki, retrieves top-3 chunks by cosine similarity, and answers via Claude.

---

## Setup

```bash
pip install rosetta-onboarder

rosetta setup    # interactive wizard — provisions Notion workspace + writes .env
rosetta serve    # starts FastAPI server, Slack bot, poller, and scheduler
```

To manually trigger a wiki:

```bash
rosetta onboard              # interactive prompt for hire details
rosetta onboard <notion-row-id>  # from an existing DB row
```

See [USAGE.md](docs/USAGE.md) for full configuration reference.

---

## Design Decisions

**MCP via stdio, not HTTP**
The Notion HTTP MCP endpoint (`mcp.notion.com`) requires OAuth — not suitable for headless automation. `@notionhq/notion-mcp-server` accepts a bearer token directly over stdio, which works cleanly with internal integrations.

**Claude decides what to fetch**
Passing all GitHub data upfront wastes tokens on repos where most of it isn't relevant. Claude calls tools on demand and stops when it has enough. This keeps costs proportional to repo complexity.

**Gemini for embeddings, Claude for generation**
Gemini's `gemini-embedding-2-preview` maps text and images into the same vector space, so architecture diagrams in READMEs are retrievable alongside prose without any special handling. Claude handles answer generation where reasoning quality matters.

**No external vector DB**
Each wiki has ~8 sections — numpy cosine similarity over a handful of vectors is sufficient for a single-hire scope. At scale the right move is embedding raw repo content (not just the wiki summary) alongside sections, at which point a dedicated vector DB becomes the correct choice.

**Slack Socket Mode over Notion iframe**
The original design embedded a chat widget as a Notion iframe. This hit hard limits: Notion doesn't expose iframe dimensions via the API, the ngrok URL had to be kept in sync every session, and the page had to be public. Socket Mode opens an outbound WebSocket from the server to Slack — no inbound connection, no public URL, no per-session config.

**Webhook + polling**
`rosetta serve` handles both Notion webhook triggers (`page.properties_updated`) and a 60-second polling loop in the same process. The webhook gives near-instant response; the poller is a fallback when the webhook isn't configured.
