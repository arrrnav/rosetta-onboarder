# `onboarder/tools.py`

## What it is

Two things in one file: the **tool definitions** that tell Claude what it can do, and the **dispatcher** that executes whatever Claude decides to call. Together they form the interface between Claude's reasoning and the Python functions that actually talk to GitHub and Notion.

## Why it exists

The Anthropic API's tool-use mechanism requires tools to be declared upfront as JSON Schema objects. `TOOL_DEFINITIONS` is that declaration. `ToolDispatcher` is the runtime half — it receives Claude's `tool_use` blocks and routes them to the correct function without the agent loop needing to import `GithubFetcher` or `NotionMCPSession` directly.

---

## Public API

### `TOOL_DEFINITIONS`

A `list[dict]` passed directly as the `tools=` argument to `anthropic.messages.create()`. Contains 7 tool definitions. Claude reads the `description` of each to decide when to call it and the `input_schema` to know what arguments to supply.

| Tool name | Backing method | Called when |
|---|---|---|
| `fetch_github_metadata` | `GithubFetcher.get_repo_metadata()` | First call for each repo — quick orientation |
| `fetch_github_readme` | `GithubFetcher.get_readme()` | Understanding repo purpose and setup |
| `fetch_github_structure` | `GithubFetcher.get_structure()` | Understanding codebase layout |
| `fetch_github_issues` | `GithubFetcher.get_issues()` | Finding good first issues for the hire |
| `fetch_github_prs` | `GithubFetcher.get_recent_prs()` | Understanding team conventions and recent activity |
| `fetch_github_contributing` | `GithubFetcher.get_contributing()` | Contribution guidelines and norms |
| `create_notion_wiki` | `NotionMCPSession.create_wiki_page()` | Once, at the very end, when all research is done |

### `ToolDispatcher`

```python
dispatcher = ToolDispatcher(
    fetcher=fetcher,
    notion_session=session,
    parent_page_id="abc123",  # Engineering Onboarding page ID
)

result_str = await dispatcher.dispatch("fetch_github_readme", {"repo_url": "..."})
```

| Attribute / Method | Type | Description |
|---|---|---|
| `created_wiki` | `WikiPage \| None` | Set when `create_notion_wiki` is dispatched. `None` until then |
| `dispatch(tool_name, tool_input)` | `async → str` | Execute one tool call; return its result as a string |

---

## Implementation notes

### Why tool descriptions matter

Claude does not call tools randomly — it reads the `description` field and decides whether a call is appropriate. The descriptions in `TOOL_DEFINITIONS` are carefully worded to:

- Tell Claude the *purpose* of each tool (not just what it does mechanically)
- Give ordering hints ("Call this first for each repo…", "Call this ONCE when you have gathered all the information you need…")
- Describe the audience ("…find beginner-friendly tasks for the new hire")

Changing a description can meaningfully change how many tool calls Claude makes and in what order.

### `create_notion_wiki` is a terminal tool

Its description explicitly says "Call this ONCE when you have gathered all the information you need." This is the mechanism that terminates the agent loop — the loop in `agent.py` checks for `dispatcher.created_wiki is not None` after each tool result and exits when it's set.

### Tool results must be strings

The Anthropic API's `tool_result` content type is `text`. Every return value from `dispatch()` is therefore either:
- Already a string (README content, CONTRIBUTING.md content)
- Serialised to a JSON string via `json.dumps(result, indent=2)`

Claude receives these strings and incorporates them into its next reasoning turn. Indented JSON is intentional — it's easier for the model to parse structured data when it's human-readable.

### `created_wiki` as post-loop state

After `create_notion_wiki` is dispatched, `dispatcher.created_wiki` holds the full `WikiPage` object. `agent.py` reads this to pass to `embeddings.py` for RAG indexing (Milestone 2). This avoids a second network round-trip to fetch the wiki back from Notion just to embed it.

---

## Example

```python
# In agent.py (simplified)
from onboarder.tools import TOOL_DEFINITIONS, ToolDispatcher

dispatcher = ToolDispatcher(fetcher, session, parent_page_id=NOTION_ONBOARDING_PAGE_ID)

response = await client.messages.create(
    model="claude-sonnet-4-6",
    tools=TOOL_DEFINITIONS,
    messages=[{"role": "user", "content": system_prompt}],
)

for block in response.content:
    if block.type == "tool_use":
        result = await dispatcher.dispatch(block.name, block.input)
        # Feed result back into the next messages turn...

    if dispatcher.created_wiki is not None:
        break  # Wiki is written — exit the loop

wiki = dispatcher.created_wiki  # Pass to embeddings.py
```
