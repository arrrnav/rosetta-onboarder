from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OnboardingInput:
    name: str
    role: str
    repo_urls: list[str]
    notes: str
    db_row_id: str  # Notion page ID of the DB row — used to write Status + wiki URL back

    @classmethod
    def empty(cls, db_row_id: str) -> "OnboardingInput":
        return cls(name="", role="", repo_urls=[], notes="", db_row_id=db_row_id)


@dataclass
class WikiSection:
    heading: str
    content: str


@dataclass
class WikiPage:
    title: str
    sections: list[WikiSection] = field(default_factory=list)

    def to_notion_blocks(self) -> list[dict]:
        """Convert wiki sections to Notion API block format."""
        blocks = []
        for section in self.sections:
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": section.heading}}]
                },
            })
            # Split content into paragraphs (Notion paragraphs have a 2000-char limit)
            paragraphs = section.content.split("\n\n")
            for para in paragraphs:
                para = para.strip()
                if not para:
                    continue
                # Chunk to stay under Notion's 2000-char rich_text limit
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
    return [text[i : i + size] for i in range(0, len(text), size)]
