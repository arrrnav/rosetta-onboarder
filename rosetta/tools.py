"""
Tool definitions for the Claude agentic loop and the dispatcher that routes
Claude's tool_use calls to the correct Python functions.

All tool functions return JSON-serializable values (str, list, dict).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .github.fetcher import GithubFetcher
from .notion.mcp_session import NotionMCPSession
from .notion.models import WikiPage, WikiSection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definitions (passed as `tools=` to anthropic.messages.create)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "fetch_github_readme",
        "description": (
            "Fetch the README file from a GitHub repository. "
            "Use this to understand what the repo does, its setup instructions, and its purpose."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_url": {
                    "type": "string",
                    "description": "Full GitHub URL, e.g. https://github.com/owner/repo",
                }
            },
            "required": ["repo_url"],
        },
    },
    {
        "name": "fetch_github_structure",
        "description": (
            "Fetch the top-level directory and file structure of a GitHub repository. "
            "Use this to understand how the codebase is organized."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_url": {"type": "string"},
                "max_depth": {
                    "type": "integer",
                    "description": "How many directory levels to traverse. Default 2.",
                },
            },
            "required": ["repo_url"],
        },
    },
    {
        "name": "fetch_github_issues",
        "description": (
            "Fetch open issues from a GitHub repository. "
            "Use label='good first issue' to find beginner-friendly tasks for the new hire."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_url": {"type": "string"},
                "label": {
                    "type": "string",
                    "description": "Filter by label, e.g. 'good first issue'. Omit for all open issues.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of issues to return. Default 10.",
                },
            },
            "required": ["repo_url"],
        },
    },
    {
        "name": "fetch_github_prs",
        "description": (
            "Fetch recent pull requests from a GitHub repository. "
            "Use this to show the new hire what active development looks like and team conventions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_url": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": ["open", "closed", "all"],
                    "description": "Filter by PR state. Default 'all'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of PRs to return. Default 5.",
                },
            },
            "required": ["repo_url"],
        },
    },
    {
        "name": "fetch_github_contributing",
        "description": (
            "Fetch the CONTRIBUTING.md file from a GitHub repository if it exists. "
            "Use this to include contribution guidelines and team norms in the wiki."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_url": {"type": "string"},
            },
            "required": ["repo_url"],
        },
    },
    {
        "name": "fetch_github_metadata",
        "description": (
            "Fetch basic metadata for a GitHub repository: description, primary language, "
            "stars, topics. Call this first for each repo to get a quick overview."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo_url": {"type": "string"},
            },
            "required": ["repo_url"],
        },
    },
    {
        "name": "create_notion_wiki",
        "description": (
            "Create the final onboarding wiki as a new Notion page. "
            "Call this ONCE when you have gathered all the information you need "
            "and are ready to write the complete, polished wiki. "
            "Structure the wiki with clear sections tailored to the new hire's role."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Page title, e.g. 'Onboarding Wiki — Jane Smith (Backend Engineer)'",
                },
                "sections": {
                    "type": "array",
                    "description": "Ordered list of wiki sections",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string"},
                            "content": {
                                "type": "string",
                                "description": "Full text content for this section (markdown-style prose)",
                            },
                        },
                        "required": ["heading", "content"],
                    },
                },
            },
            "required": ["title", "sections"],
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

class ToolDispatcher:
    """
    Routes Claude ``tool_use`` blocks to their Python implementations.

    Holds references to both the ``GithubFetcher`` and ``NotionMCPSession``
    so ``dispatch()`` can call either without the agent loop needing to know
    which tool lives where.

    State:
        ``created_wiki`` is set to the ``WikiPage`` object the moment
        ``create_notion_wiki`` is dispatched.  The agent loop reads this
        after the loop ends to pass the wiki content to the Gemini embedder
        (Milestone 2) — avoiding a second round-trip to fetch the page back
        from Notion.
    """

    def __init__(
        self,
        fetcher: GithubFetcher,
        notion_session: NotionMCPSession,
        parent_page_id: str,
        max_issues: int = 10,
        max_prs: int = 5,
        tree_depth: int = 2,
    ):
        self._fetcher = fetcher
        self._notion = notion_session
        self._parent_page_id = parent_page_id
        self._max_issues = max_issues
        self._max_prs = max_prs
        self._tree_depth = tree_depth
        self.created_wiki: WikiPage | None = None
        self.created_wiki_page_id: str = ""

    async def dispatch(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """
        Execute a tool call and return the result as a plain string.

        The Anthropic API requires ``tool_result`` content to be text, so
        every return value is either already a string (README, CONTRIBUTING)
        or serialised to JSON via ``json.dumps``.  Claude reads these strings
        as-is and incorporates them into the next message turn.

        Raises ``ValueError`` for unknown tool names — this should never
        happen in practice because Claude only calls tools from
        ``TOOL_DEFINITIONS``, but the explicit error makes debugging easier
        if a tool name typo ever slips through.
        """
        logger.debug("Tool call: %s(%s)", tool_name, json.dumps(tool_input)[:200])

        if tool_name == "fetch_github_readme":
            result = self._fetcher.get_readme(tool_input["repo_url"])
            return result

        if tool_name == "fetch_github_structure":
            depth = tool_input.get("max_depth", self._tree_depth)
            result = self._fetcher.get_structure(tool_input["repo_url"], max_depth=depth)
            return json.dumps(result, indent=2)

        if tool_name == "fetch_github_issues":
            result = self._fetcher.get_issues(
                tool_input["repo_url"],
                label=tool_input.get("label"),
                limit=tool_input.get("limit", self._max_issues),
            )
            return json.dumps(result, indent=2)

        if tool_name == "fetch_github_prs":
            result = self._fetcher.get_recent_prs(
                tool_input["repo_url"],
                state=tool_input.get("state", "all"),
                limit=tool_input.get("limit", self._max_prs),
            )
            return json.dumps(result, indent=2)

        if tool_name == "fetch_github_contributing":
            result = self._fetcher.get_contributing(tool_input["repo_url"])
            return result if result is not None else "(No CONTRIBUTING.md found)"

        if tool_name == "fetch_github_metadata":
            result = self._fetcher.get_repo_metadata(tool_input["repo_url"])
            return json.dumps(result, indent=2)

        if tool_name == "create_notion_wiki":
            sections = [
                WikiSection(heading=s["heading"], content=s["content"])
                for s in tool_input["sections"]
            ]
            wiki = WikiPage(title=tool_input["title"], sections=sections)
            self.created_wiki = wiki
            url, page_id = await self._notion.create_wiki_page(wiki, self._parent_page_id)
            self.created_wiki_page_id = page_id
            return f"Wiki created successfully. URL: {url}"

        raise ValueError(f"Unknown tool: {tool_name!r}")
