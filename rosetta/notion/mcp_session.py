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

# Notion property types we read from DB rows
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
            await self._session.__aexit__(*exc_info)
        if self._cm:
            await self._cm.__aexit__(*exc_info)

    # ------------------------------------------------------------------
    # Database operations
    # ------------------------------------------------------------------

    async def query_pending_hires(self, data_source_id: str) -> list[OnboardingInput]:
        """
        Query the New Hire Requests database for rows where Status = 'Ready'.

        Args:
            data_source_id: The Notion database ID (NOTION_DATABASE_ID in .env).

        Returns a list of fully parsed ``OnboardingInput`` objects. Rows
        whose ``Name`` property is empty are skipped with a warning (they
        are likely template/header rows accidentally set to Ready).

        Note: Uses httpx directly instead of the MCP tool — see query_done_hires.
        """
        import httpx
        url = f"https://api.notion.com/v1/databases/{data_source_id}/query"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        payload = {
            "filter": {
                "property": "Status",
                "select": {"equals": "Ready"},
            }
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload)
        raw = resp.json()
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

        Both writes are sent in a single API-patch-page call to keep the row
        transition atomic — the team lead's board view always shows a consistent
        state.  Typical call sequence:

        1. ``update_hire_row(row_id, "Processing")``   — before generation starts
        2. ``update_hire_row(row_id, "Done", wiki_url)`` — after wiki is created
        """
        properties: dict[str, Any] = {
            "Status": {"select": {"name": status}},
        }
        if wiki_url:
            properties["Wiki URL"] = {"url": wiki_url}

        await self._session.call_tool(
            "API-patch-page",
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

        API-retrieve-a-page returns the raw Notion REST API JSON, which is parsed
        by ``parse_db_row`` into an OnboardingInput.
        """
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
        """Return the current Status select value for a DB row (e.g. 'Ready', 'Done').

        Returns an empty string if the page cannot be fetched or has no Status property.
        Used by the webhook handler to guard against re-processing rows that are
        already Processing or Done.
        """
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
        """Create a Notion page from a WikiPage under the given parent.

        Returns:
            (url, page_id) — the Notion URL and the raw page ID of the created page.
            The page_id is needed to append blocks (embed, refresh) and for the chat URL.

        Notion accepts at most 100 children per API call.  We use a batch
        size of 95 (leaving headroom for the title block) and append the
        overflow via ``API-patch-block-children``.
        """
        BATCH = 95
        blocks = wiki.to_notion_blocks()
        logger.info("Wiki has %d blocks — sending in %d batch(es)",
                     len(blocks), -(-len(blocks) // BATCH))

        # Create the page with the first batch
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
        page_id = raw.get("id", "")
        url = raw.get("url", "")
        if not url:
            text = _extract_text(result)
            logger.debug("create_wiki_page result: %s", text[:200])
            url_match = re.search(r"https://www\.notion\.so/\S+", text)
            url = url_match.group(0) if url_match else text

        # Append remaining blocks in batches
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
        """Append an iframe embed block to an existing page (used to add the chat widget)."""
        await self._session.call_tool(
            "API-patch-block-children",
            {
                "block_id": page_id,
                "children": [
                    {"type": "embed", "embed": {"url": embed_url}},
                ],
            },
        )

    async def query_done_hires(
        self, data_source_id: str
    ) -> list[tuple[OnboardingInput, str, str]]:
        """
        Query the New Hire Requests database for rows where Status = 'Done'.

        Args:
            data_source_id: The Notion database ID (NOTION_DATABASE_ID in .env).

        Returns a list of (hire, wiki_url, wiki_page_id) tuples. Rows with an
        empty Name or empty Wiki URL are skipped — they have no wiki to refresh.

        Note: Uses httpx directly instead of the MCP tool because API-query-data-source
        consistently returns invalid_request_url regardless of parameter format.
        """
        import httpx
        url = f"https://api.notion.com/v1/databases/{data_source_id}/query"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        payload = {
            "filter": {
                "property": "Status",
                "select": {"equals": "Done"},
            }
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json=payload)
        raw = resp.json()
        hires = []
        for row in raw.get("results", []):
            hire = parse_db_row(row)
            if not hire.name:
                logger.warning("Skipping DB row %s — no name found", row.get("id"))
                continue
            wiki_url = _read_url_prop(row.get("properties", {}), "Wiki URL")
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
        """
        Query the New Hire Requests database for all rows regardless of status.

        Returns a list of (hire, status, wiki_url) tuples. Rows with an empty
        Name are skipped. wiki_url is empty string when not yet set.
        """
        import httpx
        url = f"https://api.notion.com/v1/databases/{data_source_id}/query"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, headers=headers, json={})
        raw = resp.json()
        results = []
        for row in raw.get("results", []):
            hire = parse_db_row(row)
            if not hire.name:
                continue
            status = row.get("properties", {}).get("Status", {}).get("select", {})
            status_name = status.get("name", "Pending") if status else "Pending"
            wiki_url = _read_url_prop(row.get("properties", {}), "Wiki URL")
            results.append((hire, status_name, wiki_url))
        return results

    async def create_hire_row(
        self,
        database_id: str,
        name: str,
        role: str,
        repo_urls: list[str],
        notes: str = "",
        contact_email: str = "",
        slack_handle: str = "",
    ) -> str:
        """
        Create a new row in the New Hire Requests database with Status=Ready.

        Returns the new page ID so the caller can show a confirmation URL.
        """
        repos_text = "\n".join(repo_urls)
        properties: dict = {
            "Name": {
                "title": [{"text": {"content": name}}]
            },
            "Role": {
                "rich_text": [{"text": {"content": role}}]
            },
            "GitHub Repos": {
                "rich_text": [{"text": {"content": repos_text}}]
            },
            "Notes": {
                "rich_text": [{"text": {"content": notes}}]
            },
            "Status": {
                "select": {"name": "Ready"}
            },
        }
        if contact_email:
            properties["Contact Email"] = {"email": contact_email}
        if slack_handle:
            properties["Slack Handle"] = {"rich_text": [{"text": {"content": slack_handle}}]}

        result = await self._session.call_tool(
            "API-post-page",
            {
                "parent": {"database_id": database_id},
                "properties": properties,
            },
        )
        data = _extract_json(result)
        return data.get("id", "")

    async def move_page(self, page_id: str, new_parent_page_id: str) -> None:
        """Reparent a Notion page (used to archive old wikis to the graveyard page).

        Uses the ``notion-move-pages`` MCP tool rather than ``API-patch-page``
        because the Notion REST PATCH endpoint does not support changing a
        page's parent — it silently ignores the ``parent`` field.
        """
        await self._session.call_tool(
            "API-move-page",
            {
                "page_id": page_id,
                "parent": {
                    "type": "page_id",
                    "page_id": new_parent_page_id,
                },
            },
        )
        logger.info("Moved page %s → parent %s", page_id, new_parent_page_id)

    async def append_updated_section(
        self, page_id: str, section_heading: str, new_content: str
    ) -> None:
        """Append refreshed content blocks to a page (used by the refresh command).

        Long content is split into multiple paragraph blocks at line boundaries
        to stay within Notion's 2000-char-per-block limit without cutting text
        mid-line.
        """
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

        # Split content into ≤2000-char chunks at line boundaries
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


# ------------------------------------------------------------------
# DB row parser
# ------------------------------------------------------------------

def parse_db_row(row: dict[str, Any]) -> OnboardingInput:
    """
    Parse a Notion DB row (as returned by API-retrieve-a-page or API-query-data-source)
    into an OnboardingInput.

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
    contact_email = _read_email(props, "Contact Email")
    slack_handle = _read_rich_text(props, "Slack Handle").lstrip("@")

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


def _read_email(props: dict[str, Any], key: str) -> str:
    """Read a Notion email property and return the address string, or empty string."""
    return props.get(key, {}).get("email") or ""


def _read_url_prop(props: dict[str, Any], key: str) -> str:
    """Read a Notion url property and return the URL string, or empty string."""
    return props.get(key, {}).get("url") or ""


def _url_to_page_id(url: str) -> str:
    """
    Extract and reformat a Notion page ID from a Notion URL.

    Notion page URLs end with a 32-char hex string (no hyphens):
        https://www.notion.so/Title-Of-Page-330f78cab1428192bec1d10cc0c8a578
    This function extracts that hex string and reformats it as a UUID:
        330f78ca-b142-8192-bec1-d10cc0c8a578
    """
    clean = url.split("?")[0].split("#")[0].rstrip("/")
    # Already UUID-formatted?
    uuid_match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
        clean.lower(),
    )
    if uuid_match:
        return uuid_match.group(1)
    # 32-char hex at end of path
    hex_match = re.search(r"([0-9a-f]{32})$", clean.lower())
    if hex_match:
        h = hex_match.group(1)
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    return ""


def _chunk_at_lines(text: str, max_size: int) -> list[str]:
    """Split *text* into chunks of at most *max_size* chars, breaking at newlines.

    If a single line exceeds *max_size* it is hard-split as a last resort.
    """
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in text.splitlines(keepends=True):
        if current_len + len(line) > max_size and current:
            chunks.append("".join(current))
            current = []
            current_len = 0
        # Single line longer than max_size — hard-split
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
