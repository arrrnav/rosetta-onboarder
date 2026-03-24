"""
Data models for the Notion onboarding agent.

This module defines the three core dataclasses that flow through the system:

  OnboardingInput  — parsed from a Notion DB row by mcp_session.parse_db_row()
  WikiSection      — a single heading + body block of the output wiki
  WikiPage         — the full agent output: title + ordered list of WikiSections

Nothing in this file makes network calls or imports third-party libraries.
It is imported by mcp_session.py, tools.py, agent.py, and embeddings.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OnboardingInput:
    """
    All the information the agent needs to generate one wiki.

    Populated by ``mcp_session.parse_db_row()`` from a single row in the
    "New Hire Requests" Notion database.  Every field maps directly to a
    database column; see ``docs/reference/mcp_session.md`` for the full
    column-to-field mapping.

    Attributes:
        name:       New hire's full name (Notion title property).
        role:       Job title / team role, e.g. "Backend Engineer".
        repo_urls:  Ordered list of full GitHub URLs assigned to this hire.
        notes:      Free-form onboarding notes written by the team lead.
        db_row_id:  Notion page ID of the source DB row.  Written back to
                    the row by ``mcp_session.update_hire_row()`` once the
                    wiki is created (sets Status → Done, adds Wiki URL).
    """

    name: str
    role: str
    repo_urls: list[str]
    notes: str
    db_row_id: str  # Notion page ID of the DB row — used to write Status + wiki URL back

    @classmethod
    def empty(cls, db_row_id: str) -> "OnboardingInput":
        """Return a blank instance for a given DB row ID (used by the parser as a starting point)."""
        return cls(name="", role="", repo_urls=[], notes="", db_row_id=db_row_id)


@dataclass
class WikiSection:
    """
    A single section of the generated onboarding wiki.

    The agent produces a list of these — one per heading — via the
    ``create_notion_wiki`` tool call.  ``WikiPage.to_notion_blocks()``
    converts them to Notion API block JSON.

    Attributes:
        heading:  Section heading text (rendered as heading_2 in Notion).
        content:  Full prose body for this section.  May be multiple
                  paragraphs separated by blank lines; ``to_notion_blocks``
                  splits on ``\\n\\n`` and chunks at 2 000 chars to stay
                  within Notion's rich_text limit.
    """

    heading: str
    content: str


@dataclass
class WikiPage:
    """
    The complete onboarding wiki ready to be written to Notion.

    Built by ``ToolDispatcher.dispatch()`` from the structured JSON the
    agent passes to ``create_notion_wiki``, then handed to
    ``NotionMCPSession.create_wiki_page()``.

    After creation the object is stored on ``ToolDispatcher.created_wiki``
    so ``agent.py`` can pass it to ``embeddings.py`` for RAG indexing
    without re-fetching from Notion.

    Attributes:
        title:    Page title shown in Notion (e.g. "Onboarding Wiki — Jane Smith").
        sections: Ordered wiki sections.  Rendered top-to-bottom in Notion.
    """

    title: str
    sections: list[WikiSection] = field(default_factory=list)

    def to_notion_blocks(self) -> list[dict]:
        """
        Convert wiki sections into Notion API ``children`` block JSON.

        Each section becomes:
          - one ``heading_2`` block for the heading
          - one or more ``paragraph`` blocks for the content

        Two Notion limits are handled here:
          - Paragraphs are split on blank lines (``\\n\\n``) so each logical
            paragraph is its own block (cleaner rendering).
          - Each paragraph is chunked to ≤ 2000 characters because Notion's
            API rejects ``rich_text`` arrays whose combined text exceeds that.
        """
        blocks = []
        for section in self.sections:
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": section.heading}}]
                },
            })
            paragraphs = section.content.split("\n\n")
            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                for chunk in _chunk_text(para, 2000):
                    blocks.append({
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [{"type": "text", "text": {"content": chunk}}]
                        },
                    })
        return blocks


def _chunk_text(text: str, size: int) -> list[str]:
    """Split ``text`` into substrings of at most ``size`` characters."""
    return [text[i : i + size] for i in range(0, len(text), size)]
