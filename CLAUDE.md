# Notion Engineer Onboarding Agent

MLH AI Challenge project. A team lead fills a Notion page with new hire info → this agent reads it, fetches GitHub repo context, and generates a personalized onboarding wiki in Notion. The new hire can then chat with their wiki using Gemini-powered RAG.

## Stack

- **Python 3.13**
- **Anthropic SDK** (`anthropic`) — Claude claude-sonnet-4-6 as the orchestrating agent
- **Notion MCP** (`mcp` + `@notionhq/notion-mcp-server`) — read/write Notion via the official MCP server over stdio
- **PyGithub** — fetch README, repo structure, issues, PRs
- **Google Generative AI** (`google-generativeai`) — Gemini multimodal embeddings for the chat RAG layer
- **numpy + Pillow** — in-memory vector store + image handling

## Architecture (bottom-up build order)

```
1. onboarder/notion/models.py        — dataclasses: OnboardingInput, WikiPage, WikiSection
2. onboarder/notion/mcp_session.py   — spawn Notion MCP server, fetch/create pages
3. onboarder/github/fetcher.py       — GitHub API wrappers (readme, structure, issues, PRs)
4. onboarder/tools.py                — Claude tool definitions + ToolDispatcher
5. onboarder/agent.py                — Claude agentic loop (tool-use until wiki is written)
6. onboarder/embeddings.py           — Gemini multimodal embeddings + in-memory vector store
7. onboarder/chat/repl.py            — interactive Q&A REPL using RAG
8. onboarder/main.py                 — CLI: `onboard`, `chat`, `refresh` commands
```

## Notion Workspace Layout (Option B)

```
Engineering Onboarding  (top-level page — manually created once, ID in NOTION_ONBOARDING_PAGE_ID)
├── New Hire Requests   (database — ID in NOTION_DATABASE_ID)
│   ├── Jane Smith      [Status: Done | Role: Backend | Wiki URL: 🔗 | Contact Email: jane@co.com]
│   └── John Doe        [Status: Ready | Role: Frontend]
└── Jane Smith's Onboarding Wiki   ← created by agent as child page
└── John Doe's Onboarding Wiki
```

## New Hire Requests Database Schema

| Property | Type | Purpose |
|---|---|---|
| Name | title | New hire's full name |
| Role | rich_text | e.g. "Backend Engineer" |
| GitHub Repos | rich_text | One GitHub URL per line |
| Notes | rich_text | Additional context for the agent |
| Status | select | Pending → Ready → Processing → Done |
| Wiki URL | url | Written back by agent after generation |
| Contact Email | email | Optional — for email notification |
| Slack Handle | rich_text | Optional — for Slack DM notification |

Team lead creates a row, fills in Name/Role/GitHub Repos/Notes, sets Status to **Ready**. The agent picks it up within 5 minutes.

## Wiki Sections Generated

1. Welcome & Overview
2. Your Repositories
3. Codebase Architecture
4. Getting Started
5. Good First Issues
6. Team Conventions
7. Recent Activity
8. Resources & Links

## Claude Tool Loop

Claude is given 7 tools and runs an agentic loop until it calls `create_notion_wiki`:

- `fetch_github_metadata` — repo description, language, stars
- `fetch_github_readme` — full README content
- `fetch_github_structure` — directory tree (max_depth=2)
- `fetch_github_issues` — open issues, filterable by label
- `fetch_github_prs` — recent pull requests
- `fetch_github_contributing` — CONTRIBUTING.md if it exists
- `create_notion_wiki` — write the final wiki to Notion (called once at the end)

## Chat Interface (RAG) — Core Feature

The chat widget is embedded directly in the generated wiki page as a Notion iframe block. New hire opens their wiki and the chat is right there — no separate tool, no Notion account required (public page + iframe).

After wiki generation:

- Wiki sections + README images are embedded using Gemini `multimodalembedding@001`
- Text query → embed → cosine similarity → retrieve top-k chunks
- Retrieved chunks passed to Claude as context for answer generation
- Falls back to full-context mode for small wikis (< ~50k tokens)

## Environment Variables

See `.env.example`. Required:
- `ANTHROPIC_API_KEY`
- `NOTION_TOKEN` — Notion internal integration token
- `GITHUB_TOKEN` — GitHub PAT (needed for private repos and higher rate limits)
- `GEMINI_API_KEY` — from Google AI Studio

## Key Design Decisions

- **MCP via stdio, not HTTP**: `@notionhq/notion-mcp-server` uses stdio transport. The `mcp` Python library handles subprocess + JSON-RPC plumbing.
- **Claude decides what to fetch**: rather than front-loading all GitHub data, Claude calls tools on demand. Handles repos of varying complexity without wasting tokens.
- **Gemini for embeddings, Claude for generation**: best-of-both-worlds. Gemini's multimodal embeddings handle images (architecture diagrams, screenshots) in the same vector space as text.
- **No external vector DB**: numpy cosine similarity is sufficient for a single wiki. Keeps the stack simple.
- **`create_notion_wiki` takes structured sections**: Claude outputs `{heading, content}` pairs rather than raw Notion block JSON. Python handles the conversion.
