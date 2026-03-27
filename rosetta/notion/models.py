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

import re
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
    db_row_id: str       # Notion page ID of the DB row — used to write Status + wiki URL back
    contact_email: str = ""   # Optional — used to send email notification after wiki creation
    slack_handle: str = ""    # Optional — used to send Slack DM after wiki creation

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
        content:  Full prose body for this section, written by the agent
                  in Markdown.  ``to_notion_blocks`` parses headings, lists,
                  code fences, and inline formatting into proper Notion blocks.
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
          - one ``heading_2`` block for the section heading
          - Notion blocks parsed from the section's Markdown content:
            headings, bulleted/numbered lists, code fences, paragraphs,
            and inline bold/italic/code annotations.

        Notion's hard limits handled here:
          - rich_text content is chunked at 2000 chars per block.
          - Total block count is capped at 95 per call (Notion max is 100;
            we leave a small buffer for the title block added by the caller).
        """
        blocks: list[dict] = []
        for section in self.sections:
            blocks.append(_heading_block(2, section.heading))
            blocks.extend(_markdown_to_blocks(section.content))
        if len(blocks) > 95:
            import logging
            logging.getLogger(__name__).warning(
                "Wiki has %d blocks — truncating to 95 (Notion limit)", len(blocks),
            )
        return blocks[:95]


# ---------------------------------------------------------------------------
# Markdown → Notion blocks
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
_BULLET_RE = re.compile(r"^[-*]\s+(.+)$")
_NUMBERED_RE = re.compile(r"^\d+\.\s+(.+)$")
_CODE_FENCE_RE = re.compile(r"^```(\w*)$")


def _markdown_to_blocks(content: str) -> list[dict]:
    """Parse a Markdown string into a flat list of Notion block dicts."""
    blocks: list[dict] = []
    lines = content.splitlines()
    i = 0
    para_lines: list[str] = []

    def flush_para() -> None:
        if para_lines:
            text = " ".join(para_lines).strip()
            para_lines.clear()
            if text:
                for chunk in _chunk_text(text, 2000):
                    blocks.append(_paragraph_block(chunk))

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── Code fence ──────────────────────────────────────────────────
        fence_match = _CODE_FENCE_RE.match(stripped)
        if fence_match:
            flush_para()
            language = fence_match.group(1) or "plain text"
            code_lines: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            code_text = "\n".join(code_lines)
            for chunk in _chunk_text(code_text, 2000):
                blocks.append({
                    "object": "block",
                    "type": "code",
                    "code": {
                        "rich_text": [{"type": "text", "text": {"content": chunk}}],
                        "language": language,
                    },
                })
            i += 1  # skip closing ```
            continue

        # ── Heading ──────────────────────────────────────────────────────
        h_match = _HEADING_RE.match(stripped)
        if h_match:
            flush_para()
            level = min(len(h_match.group(1)), 3)
            blocks.append(_heading_block(level, h_match.group(2).strip()))
            i += 1
            continue

        # ── Bulleted list item ───────────────────────────────────────────
        b_match = _BULLET_RE.match(stripped)
        if b_match:
            flush_para()
            blocks.append(_list_block("bulleted_list_item", b_match.group(1).strip()))
            i += 1
            continue

        # ── Numbered list item ───────────────────────────────────────────
        n_match = _NUMBERED_RE.match(stripped)
        if n_match:
            flush_para()
            blocks.append(_list_block("numbered_list_item", n_match.group(1).strip()))
            i += 1
            continue

        # ── Blank line → flush paragraph ────────────────────────────────
        if not stripped:
            flush_para()
            i += 1
            continue

        # ── Regular text → accumulate paragraph ─────────────────────────
        para_lines.append(stripped)
        i += 1

    flush_para()
    return blocks


# ---------------------------------------------------------------------------
# Inline Markdown → Notion rich_text spans
# ---------------------------------------------------------------------------

_INLINE_RE = re.compile(
    r"\*\*(.+?)\*\*"   # **bold**
    r"|__(.+?)__"       # __bold__
    r"|\*(.+?)\*"       # *italic*
    r"|_(.+?)_"         # _italic_
    r"|`(.+?)`",        # `code`
    re.DOTALL,
)


def _parse_inline(text: str) -> list[dict]:
    """Convert inline Markdown formatting to a Notion rich_text array."""
    spans: list[dict] = []
    last = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > last:
            spans.append({"type": "text", "text": {"content": text[last:m.start()]}})
        bold1, bold2, italic1, italic2, code = m.groups()
        if bold1 or bold2:
            spans.append({
                "type": "text",
                "text": {"content": bold1 or bold2},
                "annotations": {"bold": True},
            })
        elif italic1 or italic2:
            spans.append({
                "type": "text",
                "text": {"content": italic1 or italic2},
                "annotations": {"italic": True},
            })
        elif code:
            spans.append({
                "type": "text",
                "text": {"content": code},
                "annotations": {"code": True},
            })
        last = m.end()
    if last < len(text):
        spans.append({"type": "text", "text": {"content": text[last:]}})
    return spans or [{"type": "text", "text": {"content": text}}]


# ---------------------------------------------------------------------------
# Block constructors
# ---------------------------------------------------------------------------

def _heading_block(level: int, text: str) -> dict:
    key = f"heading_{level}"
    return {"object": "block", "type": key, key: {"rich_text": _parse_inline(text)}}


def _paragraph_block(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _parse_inline(text)},
    }


def _list_block(block_type: str, text: str) -> dict:
    return {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": _parse_inline(text)},
    }


def _chunk_text(text: str, size: int) -> list[str]:
    """Split ``text`` into substrings of at most ``size`` characters."""
    return [text[i: i + size] for i in range(0, len(text), size)]
