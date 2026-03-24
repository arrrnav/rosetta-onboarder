"""
Async context manager for communicating with the Notion MCP server via stdio.

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

# Notion property types we read from DB rows
_GITHUB_URL_RE = re.compile(r"https://github\.com/[^\s,]+")


class NotionMCPSession:
    def __init__(self, token: str):
        self._token = token
        self._session: ClientSession | None = None
        self._cm = None

    async def __aenter__(self) -> "NotionMCPSession":
        server_params = StdioServerParameters(
            command="npx",
            args=["-y", "@notionhq/notion-mcp-server"],
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
            await self._session.__aexit__(*exc_info)
        if self._cm:
            await self._cm.__aexit__(*exc_info)

    # ------------------------------------------------------------------
    # Database operations
    # ------------------------------------------------------------------

    async def query_pending_hires(self, database_id: str) -> list[OnboardingInput]:
        """
        Query the New Hire Requests database for rows where Status = 'Ready'.

        Filters server-side using Notion's select filter so only actionable
        rows are returned — the polling loop never has to inspect rows that
        are already Processing or Done.

        Returns a list of fully parsed ``OnboardingInput`` objects. Rows
        whose ``Name`` property is empty are skipped with a warning (they
        are likely template/header rows accidentally set to Ready).
        """
        result = await self._session.call_tool(
            "notion-query-database",
            {
                "database_id": database_id,
                "filter": {
                    "property": "Status",
                    "select": {"equals": "Ready"},
                },
            },
        )
        raw = _extract_json(result)
        hires = []
        for row in raw.get("results", []):
            hire = parse_db_row(row)
            if hire.name:
                hires.append(hire)
            else:
                logger.warning("Skipping DB row %s — no name found", row.get("id"))
        return hires

    async def update_hire_row(
        self,
        row_id: str,
        status: str,
        wiki_url: str | None = None,
    ) -> None:
        """
        Update the Status (and optionally Wiki URL) properties on a DB row.

        Both writes are sent in a single ``notion-update-page`` call to keep
        the row transition atomic — the team lead's board view always shows a
        consistent state.  Typical call sequence:

        1. ``update_hire_row(row_id, "Processing")``   — before generation starts
        2. ``update_hire_row(row_id, "Done", wiki_url)`` — after wiki is created
        """
        properties: dict[str, Any] = {
            "Status": {"select": {"name": status}},
        }
        if wiki_url:
            properties["Wiki URL"] = {"url": wiki_url}

        await self._session.call_tool(
            "notion-update-page",
            {
                "page_id": row_id,
                "properties": properties,
            },
        )
        logger.debug("Updated row %s → Status=%s wiki_url=%s", row_id, status, wiki_url)

    async def fetch_hire_row(self, row_id: str) -> OnboardingInput:
        """
        Fetch a single DB row by its Notion page ID and parse it into an OnboardingInput.

        Used by the ``onboard <row_id>`` CLI command to process one specific hire
        without querying the entire database.  The row_id is the page ID of the
        database entry (the UUID in the Notion page URL).
        """
        result = await self._session.call_tool(
            "notion-retrieve-page",
            {"page_id": row_id},
        )
        row = _extract_json(result)
        hire = parse_db_row(row)
        if not hire.name:
            raise ValueError(f"Could not parse hire data from row {row_id!r} — is it a valid New Hire Requests DB row?")
        return hire

    # ------------------------------------------------------------------
    # Wiki page operations
    # ------------------------------------------------------------------

    async def create_wiki_page(self, wiki: WikiPage, parent_page_id: str) -> str:
        """Create a Notion page from a WikiPage under the given parent. Returns the new page URL."""
        blocks = wiki.to_notion_blocks()
        result = await self._session.call_tool(
            "notion-create-pages",
            {
                "parent": {"page_id": parent_page_id},
                "properties": {
                    "title": [{"text": {"content": wiki.title}}]
                },
                "children": blocks,
            },
        )
        text = _extract_text(result)
        logger.debug("create_wiki_page result: %s", text[:200])
        url_match = re.search(r"https://www\.notion\.so/\S+", text)
        return url_match.group(0) if url_match else text

    async def append_embed_block(self, page_id: str, embed_url: str) -> None:
        """Append an iframe embed block to an existing page (used to add the chat widget)."""
        await self._session.call_tool(
            "notion-append-block-children",
            {
                "block_id": page_id,
                "children": [
                    {"type": "embed", "embed": {"url": embed_url}},
                ],
            },
        )

    async def append_updated_section(
        self, page_id: str, section_heading: str, new_content: str
    ) -> None:
        """Append refreshed content blocks to a page (used by the refresh command)."""
        await self._session.call_tool(
            "notion-append-block-children",
            {
                "block_id": page_id,
                "children": [
                    {
                        "object": "block",
                        "type": "heading_2",
                        "heading_2": {
                            "rich_text": [
                                {"type": "text", "text": {"content": f"[Refreshed] {section_heading}"}}
                            ]
                        },
                    },
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {"type": "text", "text": {"content": new_content[:2000]}}
                            ]
                        },
                    },
                ],
            },
        )


# ------------------------------------------------------------------
# DB row parser
# ------------------------------------------------------------------

def parse_db_row(row: dict[str, Any]) -> OnboardingInput:
    """
    Parse a Notion DB row (as returned by notion-query-database) into an OnboardingInput.

    Expected DB columns:
        Name        — title property (new hire's full name)
        Role        — rich_text property (e.g. "Backend Engineer")
        GitHub Repos — rich_text property (one URL per line, or comma-separated)
        Notes       — rich_text property (free-form onboarding notes)
        Status      — select property ("Pending" | "Ready" | "Processing" | "Done")
        Contact Email — email property (optional)
        Slack Handle  — rich_text property (optional, e.g. "@jane")
    """
    row_id = row.get("id", "")
    props = row.get("properties", {})

    name = _read_title(props, "Name")
    role = _read_rich_text(props, "Role")
    notes = _read_rich_text(props, "Notes")
    repos_raw = _read_rich_text(props, "GitHub Repos")
    repo_urls = _extract_github_urls(repos_raw)

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
    )


# ------------------------------------------------------------------
# Property readers
# ------------------------------------------------------------------

def _read_title(props: dict[str, Any], key: str) -> str:
    """
    Read a Notion title property and return its plain text.

    Notion represents all rich-text fields (including titles) as arrays of
    span objects — e.g. ``[{"plain_text": "Jane"}, {"plain_text": " Smith"}]``.
    We join all spans because a single visible "word" can be split across
    multiple spans (e.g. if part of it is bolded or linked).
    """
    items = props.get(key, {}).get("title", [])
    return "".join(item.get("plain_text", "") for item in items).strip()


def _read_rich_text(props: dict[str, Any], key: str) -> str:
    """
    Read a Notion rich_text property and return its plain text.

    Same span-array behaviour as ``_read_title`` — see that function's
    docstring for why we join spans rather than taking ``[0]["plain_text"]``.
    """
    items = props.get(key, {}).get("rich_text", [])
    return "".join(item.get("plain_text", "") for item in items).strip()


def _extract_github_urls(text: str) -> list[str]:
    """Extract all GitHub repo URLs from a block of text."""
    return list(dict.fromkeys(_GITHUB_URL_RE.findall(text)))  # deduplicate, preserve order


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
    """
    Parse an MCP tool call result as a JSON dict.

    Logs the first 300 characters of the raw response on failure — enough
    to diagnose auth errors or unexpected HTML error pages without flooding
    the log with a full Notion API response body.
    """
    import json
    text = _extract_text(result)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.error("Failed to parse MCP result as JSON: %s", text[:300])
        return {}
