# `rosetta/notion/models.py`

## What it is

Pure data structures with no I/O, no network calls, and no third-party imports. Defines the three dataclasses that act as the shared language between every layer of the system: what goes **in** to the agent (`OnboardingInput`), and what comes **out** (`WikiSection`, `WikiPage`).

## Why it exists

Every other module imports from here. Keeping the data model in one place means:
- `mcp_session.py` can produce an `OnboardingInput` without knowing anything about the agent
- `tools.py` can produce a `WikiPage` without knowing anything about Notion's API
- `embeddings.py` can index a `WikiPage` without knowing anything about GitHub

---

## Public API

| Name | Kind | Description |
|---|---|---|
| `OnboardingInput` | dataclass | Parsed contents of one Notion DB row — everything the agent needs to start |
| `WikiSection` | dataclass | One section of the output wiki: a heading and its body text |
| `WikiPage` | dataclass | The complete wiki: title + ordered sections. Knows how to serialise itself to Notion block JSON |
| `WikiPage.to_notion_blocks()` | method | Converts sections to Notion API `children` block format |

---

## Field reference

### `OnboardingInput`

| Field | Type | Source in Notion DB |
|---|---|---|
| `name` | `str` | `Name` — title property |
| `role` | `str` | `Role` — rich_text property |
| `repo_urls` | `list[str]` | `GitHub Repos` — rich_text, one URL per line |
| `notes` | `str` | `Notes` — rich_text, free-form |
| `db_row_id` | `str` | The Notion page ID of the row itself — written back after wiki creation |

### `WikiSection`

| Field | Type | Notes |
|---|---|---|
| `heading` | `str` | Rendered as `heading_2` in Notion |
| `content` | `str` | Multi-paragraph prose; blank lines become paragraph boundaries |

### `WikiPage`

| Field | Type | Notes |
|---|---|---|
| `title` | `str` | Top-level page title in Notion |
| `sections` | `list[WikiSection]` | Rendered in order, top to bottom |

---

## Implementation notes

### Notion's 2 000-character rich_text limit

Notion's API rejects any `rich_text` array whose total character count exceeds 2 000. `to_notion_blocks()` handles this in two steps:

1. Splits `content` on `\n\n` — each logical paragraph becomes its own `paragraph` block
2. Passes each paragraph through `_chunk_text(para, 2000)` — any paragraph longer than 2 000 chars is split into multiple blocks

This is invisible to the reader in Notion; consecutive paragraph blocks render as normal flowing text.

### Why `WikiPage` owns the serialisation

Keeping `to_notion_blocks()` on the model rather than in `mcp_session.py` means the conversion logic is testable without a live Notion connection and stays co-located with the data it describes.

---

## Example

```python
from rosetta.notion.models import OnboardingInput, WikiPage, WikiSection

# Built by mcp_session.parse_db_row() in practice
hire = OnboardingInput(
    name="Jane Smith",
    role="Backend Engineer",
    repo_urls=["https://github.com/acme/payments"],
    notes="Focus on the payments module first.",
    db_row_id="abc123",
)

# Built by ToolDispatcher after Claude calls create_notion_wiki
wiki = WikiPage(
    title="Onboarding Wiki — Jane Smith (Backend Engineer)",
    sections=[
        WikiSection("Welcome", "Hi Jane! Here's everything you need..."),
        WikiSection("Getting Started", "Clone the repo:\n\n```\ngit clone ...\n```"),
    ],
)

# Passed to NotionMCPSession.create_wiki_page()
blocks = wiki.to_notion_blocks()
```
