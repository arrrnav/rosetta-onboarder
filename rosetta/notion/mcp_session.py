"""
Async context manager for communicating with the Notion MCP server via stdio.

Uses @notionhq/notion-mcp-server (npm package, stdio transport) which exposes
raw Notion REST API wrappers under API-prefixed tool names, e.g.:
  API-retrieve-a-page, API-patch-page, API-post-page, API-patch-block-children

Usage:
    async with NotionMCPSession(token=settings.notion_token) as session:
        hires = await session.query_pending_hires(db_id)
        wiki_url = await session.create_wiki_page(wiki, parent_page_id)
        await session.update_hire_row(row_id, status="Done", wiki_url=wiki_url)
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .models import OnboardingInput, WikiPage

logger = logging.getLogger(__name__)

_GITHUB_URL_RE = re.compile(r"https://github\.com/[^\s,]+")


class NotionMCPSession:
    def __init__(self, token: str):
        self._token = token
        self._session: ClientSession | None = None
        self._cm = None

    async def __aenter__(self) -> "NotionMCPSession":
        server_params = StdioServerParameters(
            command="notion-mcp-server",
            args=[],
            env={
                **os.environ,
                "NOTION_TOKEN": self._token,
                "OPENAPI_MCP_HEADERS": (
                    f'{{"Authorization": "Bearer {self._token}", '
                    f'"Notion-Version": "2022-06-28"}}'
                ),
            },
        )
        self._cm = stdio_client(server_params)
        read, write = await self._cm.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()

        tools = await self._session.list_tools()
        tool_names = [t.name for t in tools.tools]
        logger.debug("Notion MCP tools available: %s", tool_names)
        if not tool_names:
            raise RuntimeError(
                "Notion MCP server connected but returned no tools — check NOTION_TOKEN"
            )
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._session:
            try:
                await self._session.__aexit__(*exc_info)
            except Exception:
                pass  # MCP session teardown errors are not actionable
        if self._cm:
            try:
                await self._cm.__aexit__(*exc_info)
            except RuntimeError as exc:
                # anyio cancel scope exits in a different task than it was entered in —
                # a known limitation when the MCP stdio_client is cleaned up outside
                # its original task context. Safe to ignore.
                if "cancel scope" not in str(exc).lower():
                    raise
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Database queries (shared helper)
    # ------------------------------------------------------------------

    async def _query_database(
        self, database_id: str, filter_payload: dict | None = None
    ) -> list[dict]:
        """
        Query a Notion database via httpx (not MCP — avoids API-query-data-source issues).

        Returns the raw ``results`` list of page objects.
        """
        import httpx
        url = f"https://api.notion.com/v1/databases/{database_id}/query"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        body = filter_payload or {}
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, headers=headers, json=body)
        return resp.json().get("results", [])

    # ------------------------------------------------------------------
    # Database operations
    # ------------------------------------------------------------------

    async def query_pending_hires(self, data_source_id: str) -> list[OnboardingInput]:
        """Query for rows where Status = 'Ready'."""
        rows = await self._query_database(data_source_id, {
            "filter": {"property": "Status", "select": {"equals": "Ready"}}
        })
        hires = []
        for row in rows:
            hire = parse_db_row(row)
            if hire.name:
                hires.append(hire)
            else:
                logger.warning("Skipping DB row %s — no name found", row.get("id"))
        return hires

    async def query_done_hires(
        self, data_source_id: str
    ) -> list[tuple[OnboardingInput, str, str]]:
        """Query for rows where Status = 'Done'. Returns (hire, wiki_url, wiki_page_id)."""
        rows = await self._query_database(data_source_id, {
            "filter": {"property": "Status", "select": {"equals": "Done"}}
        })
        hires = []
        for row in rows:
            hire = parse_db_row(row)
            if not hire.name:
                logger.warning("Skipping DB row %s — no name found", row.get("id"))
                continue
            wiki_url = _read_prop(row.get("properties", {}), "Wiki URL", "url")
            if not wiki_url:
                logger.warning("Skipping %s — no Wiki URL set", hire.name)
                continue
            wiki_page_id = _url_to_page_id(wiki_url)
            if not wiki_page_id:
                logger.warning("Skipping %s — could not parse wiki page ID from %s", hire.name, wiki_url)
                continue
            hires.append((hire, wiki_url, wiki_page_id))
        return hires

    async def query_all_hires(
        self, data_source_id: str
    ) -> list[tuple[OnboardingInput, str, str]]:
        """Query all rows. Returns (hire, status_name, wiki_url)."""
        rows = await self._query_database(data_source_id)
        results = []
        for row in rows:
            hire = parse_db_row(row)
            if not hire.name:
                continue
            status = row.get("properties", {}).get("Status", {}).get("select", {})
            status_name = status.get("name", "Pending") if status else "Pending"
            wiki_url = _read_prop(row.get("properties", {}), "Wiki URL", "url")
            results.append((hire, status_name, wiki_url))
        return results

    async def update_hire_row(
        self,
        row_id: str,
        status: str,
        wiki_url: str | None = None,
    ) -> None:
        """Update Status (and optionally Wiki URL) on a DB row."""
        properties: dict[str, Any] = {
            "Status": {"select": {"name": status}},
        }
        if wiki_url:
            properties["Wiki URL"] = {"url": wiki_url}

        await self._session.call_tool(
            "API-patch-page",
            {"page_id": row_id, "properties": properties},
        )
        logger.debug("Updated row %s → Status=%s wiki_url=%s", row_id, status, wiki_url)

    async def fetch_hire_row(self, row_id: str) -> OnboardingInput:
        """Fetch a single DB row and parse into OnboardingInput."""
        result = await self._session.call_tool(
            "API-retrieve-a-page",
            {"page_id": row_id},
        )
        row = _extract_json(result)
        hire = parse_db_row(row)
        if not hire.name:
            raise ValueError(
                f"Could not parse hire data from row {row_id!r} — "
                "is it a valid New Hire Requests DB row?"
            )
        return hire

    async def fetch_page_status(self, page_id: str) -> str:
        """Return the current Status select value (e.g. 'Ready', 'Done')."""
        try:
            result = await self._session.call_tool(
                "API-retrieve-a-page",
                {"page_id": page_id},
            )
            raw = _extract_json(result)
            props = raw.get("properties") or {}
            status_prop = props.get("Status") or {}
            select = status_prop.get("select") or {}
            return select.get("name", "")
        except Exception:
            logger.exception("fetch_page_status failed for %s", page_id)
            return ""

    # ------------------------------------------------------------------
    # Wiki page operations
    # ------------------------------------------------------------------

    async def create_wiki_page(self, wiki: WikiPage, parent_page_id: str) -> tuple[str, str]:
        """Create a Notion page from a WikiPage. Returns (url, page_id)."""
        BATCH = 95
        blocks = wiki.to_notion_blocks()
        logger.info("Wiki has %d blocks — sending in %d batch(es)",
                     len(blocks), -(-len(blocks) // BATCH))

        result = await self._session.call_tool(
            "API-post-page",
            {
                "parent": {"page_id": parent_page_id},
                "properties": {
                    "title": [{"text": {"content": wiki.title}}]
                },
                "children": blocks[:BATCH],
            },
        )
        raw = _extract_json(result)
        if raw.get("object") == "error":
            raise RuntimeError(
                f"Notion API error creating wiki page: {raw.get('message', raw)}"
            )
        page_id = raw.get("id", "")
        url = raw.get("url", "")
        if not url:
            text = _extract_text(result)
            logger.debug("create_wiki_page result: %s", text[:200])
            url_match = re.search(r"https://www\.notion\.so/\S+", text)
            url = url_match.group(0) if url_match else ""

        for i in range(BATCH, len(blocks), BATCH):
            batch = blocks[i : i + BATCH]
            logger.info("Appending blocks %d–%d of %d",
                        i + 1, i + len(batch), len(blocks))
            await self._session.call_tool(
                "API-patch-block-children",
                {"block_id": page_id, "children": batch},
            )

        return url, page_id

    async def append_embed_block(self, page_id: str, embed_url: str) -> None:
        """Append an iframe embed block to an existing page."""
        await self._session.call_tool(
            "API-patch-block-children",
            {
                "block_id": page_id,
                "children": [
                    {"type": "embed", "embed": {"url": embed_url}},
                ],
            },
        )

    async def create_hire_row(
        self,
        database_id: str,
        name: str,
        role: str,
        repo_urls: list[str],
        notes: str = "",
        contact_email: str = "",
        slack_handle: str = "",
        supervisor_slack: str = "",
        context_pages: str = "",
    ) -> str:
        """Create a new row with Status=Ready. Returns the new page ID."""
        repos_text = "\n".join(repo_urls)
        properties: dict = {
            "Name": {"title": [{"text": {"content": name}}]},
            "Role": {"rich_text": [{"text": {"content": role}}]},
            "GitHub Repos": {"rich_text": [{"text": {"content": repos_text}}]},
            "Agent Notes": {"rich_text": [{"text": {"content": notes}}]},
            "Status": {"select": {"name": "Ready"}},
        }
        if contact_email:
            properties["Contact Email"] = {"email": contact_email}
        if slack_handle:
            properties["Slack Handle"] = {"rich_text": [{"text": {"content": slack_handle}}]}
        if supervisor_slack:
            properties["Supervisor Slack Handle"] = {"rich_text": [{"text": {"content": supervisor_slack}}]}
        if context_pages:
            properties["Context Pages"] = {"rich_text": [{"text": {"content": context_pages}}]}

        result = await self._session.call_tool(
            "API-post-page",
            {"parent": {"database_id": database_id}, "properties": properties},
        )
        data = _extract_json(result)
        return data.get("id", "")

    async def move_page(self, page_id: str, new_parent_page_id: str) -> None:
        """Reparent a Notion page (archive old wikis)."""
        await self._session.call_tool(
            "API-move-page",
            {
                "page_id": page_id,
                "parent": {"type": "page_id", "page_id": new_parent_page_id},
            },
        )
        logger.info("Moved page %s → parent %s", page_id, new_parent_page_id)

    async def append_updated_section(
        self, page_id: str, section_heading: str, new_content: str
    ) -> None:
        """Append refreshed content blocks to a page."""
        children: list[dict[str, Any]] = [
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [
                        {"type": "text", "text": {"content": f"[Refreshed] {section_heading}"}}
                    ]
                },
            },
        ]

        for chunk in _chunk_at_lines(new_content, 2000):
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": chunk}}
                    ]
                },
            })

        await self._session.call_tool(
            "API-patch-block-children",
            {"block_id": page_id, "children": children},
        )

    async def fetch_notion_page_text(self, page_id: str, max_chars: int = 8000) -> str:
        """
        Fetch a Notion page's block content and return it as plain text.

        Paginates through all blocks, converts each to text via ``_block_to_text``,
        and truncates the result at ``max_chars`` to keep token usage bounded.
        """
        import httpx
        url = f"https://api.notion.com/v1/blocks/{page_id}/children"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": "2022-06-28",
        }
        parts: list[str] = []
        cursor: str | None = None

        async with httpx.AsyncClient(timeout=20.0) as client:
            while True:
                params: dict = {"page_size": 100}
                if cursor:
                    params["start_cursor"] = cursor
                resp = await client.get(url, headers=headers, params=params)
                data = resp.json()
                for block in data.get("results", []):
                    text = _block_to_text(block)
                    if text:
                        parts.append(text)
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")

        return "\n".join(parts)[:max_chars]


# ------------------------------------------------------------------
# DB row parser
# ------------------------------------------------------------------

def parse_db_row(row: dict[str, Any]) -> OnboardingInput:
    """Parse a Notion DB row into an OnboardingInput."""
    row_id = row.get("id", "")
    props = row.get("properties", {})

    name = _read_prop(props, "Name", "title")
    role = _read_prop(props, "Role", "rich_text")
    notes = _read_prop(props, "Agent Notes", "rich_text")
    repos_raw = _read_prop(props, "GitHub Repos", "rich_text")
    repo_urls = _extract_github_urls(repos_raw)
    contact_email = _read_prop(props, "Contact Email", "email")
    slack_handle = _read_prop(props, "Slack Handle", "rich_text").lstrip("@")
    supervisor_slack = _read_prop(props, "Supervisor Slack Handle", "rich_text").lstrip("@")
    context_pages_raw = _read_prop(props, "Context Pages", "rich_text")
    context_page_ids = _extract_notion_page_ids(context_pages_raw)
    if context_pages_raw and not context_page_ids:
        logger.warning(
            "DB row %s: 'Context Pages' has text but no Notion URLs could be parsed: %r",
            row_id, context_pages_raw[:200],
        )
    elif context_page_ids:
        logger.debug(
            "DB row %s: parsed %d context page ID(s): %s",
            row_id, len(context_page_ids), context_page_ids,
        )

    if not name:
        logger.warning("DB row %s: 'Name' property is empty", row_id)
    if not role:
        logger.warning("DB row %s: 'Role' property is empty", row_id)
    if not repo_urls:
        logger.warning("DB row %s: no GitHub URLs found in 'GitHub Repos'", row_id)

    return OnboardingInput(
        name=name,
        role=role,
        repo_urls=repo_urls,
        notes=notes,
        db_row_id=row_id,
        contact_email=contact_email,
        slack_handle=slack_handle,
        supervisor_slack=supervisor_slack,
        context_page_ids=context_page_ids,
    )


# ------------------------------------------------------------------
# Unified property reader
# ------------------------------------------------------------------

def _read_prop(props: dict[str, Any], key: str, prop_type: str) -> str:
    """
    Read a Notion property value as a plain string.

    Supports: title, rich_text, email, url.
    """
    prop = props.get(key, {})
    if prop_type in ("title", "rich_text"):
        items = prop.get(prop_type, [])
        return "".join(item.get("plain_text", "") for item in items).strip()
    if prop_type == "email":
        return prop.get("email") or ""
    if prop_type == "url":
        return prop.get("url") or ""
    return ""


# ------------------------------------------------------------------
# URL / text helpers
# ------------------------------------------------------------------

def _url_to_page_id(url: str) -> str:
    """Extract a UUID-formatted page ID from a Notion URL."""
    clean = url.split("?")[0].split("#")[0].rstrip("/")
    uuid_match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
        clean.lower(),
    )
    if uuid_match:
        return uuid_match.group(1)
    hex_match = re.search(r"([0-9a-f]{32})$", clean.lower())
    if hex_match:
        h = hex_match.group(1)
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    return ""


def _chunk_at_lines(text: str, max_size: int) -> list[str]:
    """Split text into chunks of at most max_size chars, breaking at newlines."""
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        if current_len + len(line) > max_size and current:
            chunks.append("".join(current))
            current = []
            current_len = 0
        while len(line) > max_size:
            chunks.append(line[:max_size])
            line = line[max_size:]
        if line:
            current.append(line)
            current_len += len(line)

    if current:
        chunks.append("".join(current))
    return chunks or [text]


def _extract_github_urls(text: str) -> list[str]:
    """Extract all GitHub repo URLs from text."""
    return list(dict.fromkeys(_GITHUB_URL_RE.findall(text)))


def _extract_notion_page_ids(text: str) -> list[str]:
    """Extract Notion page IDs from text containing notion.so URLs."""
    urls = re.findall(r"https://www\.notion\.so/\S+", text)
    ids = []
    for url in urls:
        page_id = _url_to_page_id(url)
        if page_id:
            ids.append(page_id)
    return list(dict.fromkeys(ids))


def _block_to_text(block: dict) -> str:
    """Convert a single Notion block to a plain-text string."""
    block_type = block.get("type", "")
    content = block.get(block_type, {})
    rich_text = content.get("rich_text", [])
    text = "".join(span.get("plain_text", "") for span in rich_text).strip()

    if not text:
        return ""
    if block_type in ("heading_1", "heading_2", "heading_3"):
        return f"## {text}"
    if block_type in ("bulleted_list_item", "numbered_list_item"):
        return f"- {text}"
    if block_type == "code":
        lang = content.get("language", "")
        return f"```{lang}\n{text}\n```"
    if block_type == "quote":
        return f"> {text}"
    return text


# ------------------------------------------------------------------
# MCP result helpers
# ------------------------------------------------------------------

def _extract_text(result: Any) -> str:
    """Pull plain text out of an MCP tool call result."""
    if hasattr(result, "content"):
        return "\n".join(
            item.text for item in result.content if hasattr(item, "text")
        )
    return str(result)


def _extract_json(result: Any) -> dict[str, Any]:
    """Parse an MCP tool call result as JSON."""
    import json
    text = _extract_text(result)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error("Failed to parse MCP result as JSON: %s", text[:300])
        return {}
