# `rosetta/notion/mcp_session.py`

## What it is

The bridge between Python and the Notion API. Rather than calling Notion's REST API directly with `httpx`, this module communicates via the **MCP protocol** — it spawns the official `@notionhq/notion-mcp-server` Node.js process as a subprocess and exchanges JSON-RPC messages over stdin/stdout.

## Why it exists

The Notion MCP server is the authoritative way to interact with Notion in this stack. Using it means:
- The tool surface is identical to what Notion's own integrations use
- Authentication and API versioning are handled by the server, not our code
- If Notion updates their API, upgrading `@notionhq/notion-mcp-server` is sufficient

This module is the only place in the codebase that knows about Notion's wire format. Everything above it works with clean Python objects (`OnboardingInput`, `WikiPage`).

---

## Public API

### `NotionMCPSession` (async context manager)

```python
async with NotionMCPSession(token="secret_...") as session:
    ...
```

Spawns the MCP subprocess on `__aenter__` and shuts it down on `__aexit__`. Always use as a context manager — do not instantiate directly.

| Method | Returns | Description |
|---|---|---|
| `query_pending_hires(database_id)` | `list[OnboardingInput]` | Fetch all DB rows where `Status = Ready` |
| `update_hire_row(row_id, status, wiki_url?)` | `None` | Set `Status` (and optionally `Wiki URL`) on a DB row |
| `create_wiki_page(wiki, parent_page_id)` | `str` (URL) | Create a new Notion page from a `WikiPage`; returns the page URL |
| `append_embed_block(page_id, embed_url)` | `None` | Add an iframe embed block to an existing page (chat widget) |
| `append_updated_section(page_id, heading, content)` | `None` | Append a refreshed section to an existing wiki page |

### Module-level helpers

| Function | Returns | Description |
|---|---|---|
| `parse_db_row(row)` | `OnboardingInput` | Parse one raw Notion DB row dict into an `OnboardingInput` |

---

## Implementation notes

### MCP over stdio, not HTTP

The `mcp` Python library manages the subprocess lifecycle and JSON-RPC framing. The call flow is:

```
Python code
  → mcp.ClientSession.call_tool("notion-query-database", {...})
    → JSON-RPC over stdin to npx @notionhq/notion-mcp-server
      → Notion REST API
    ← JSON-RPC response over stdout
  ← Python dict
```

The `OPENAPI_MCP_HEADERS` environment variable passes the `Authorization` and `Notion-Version` headers to the server — this is required by the `@notionhq/notion-mcp-server` design.

### Why Notion rich_text is an array

Notion stores all text as arrays of *span* objects, not plain strings. A single visible phrase like "Jane Smith" might be:

```json
[
  {"plain_text": "Jane", "annotations": {"bold": true}},
  {"plain_text": " Smith", "annotations": {"bold": false}}
]
```

`_read_title()` and `_read_rich_text()` join all `plain_text` values across the array. Taking only `[0]["plain_text"]` would silently truncate any text that spans multiple formatting regions.

### Atomic row updates

`update_hire_row()` sends both `Status` and `Wiki URL` in a single `notion-update-page` call. This keeps the team lead's board view consistent — a row is never in a state where Status is "Done" but the wiki link is missing, or vice versa.

### Startup validation

On `__aenter__`, after the MCP handshake, the session immediately calls `list_tools()` and raises a `RuntimeError` if the result is empty. An empty tool list is the most common symptom of an invalid `NOTION_TOKEN` — surfacing it at startup avoids a confusing failure deep into a generation run.

---

## Example

```python
import asyncio
from rosetta.notion.mcp_session import NotionMCPSession

async def main():
    async with NotionMCPSession(token="secret_...") as session:
        # Poll for new hires
        hires = await session.query_pending_hires("db_id_here")

        for hire in hires:
            print(hire.name, hire.repo_urls)

            # Mark as processing before starting generation
            await session.update_hire_row(hire.db_row_id, "Processing")

            # ... run agent, get wiki ...

            # Mark done and write back the URL
            await session.update_hire_row(hire.db_row_id, "Done", wiki_url="https://notion.so/...")

asyncio.run(main())
```
