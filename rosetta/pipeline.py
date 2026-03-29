"""
Shared onboarding pipeline — used by both the CLI and the background server.

Encapsulates the full flow: status guard -> fetch hire -> set Processing ->
run agent -> index embeddings -> notify -> set Done.

Both ``main.py`` (manual ``rosetta onboard <id>``) and ``chat/server.py``
(poller + webhook) delegate to this module so the logic is never duplicated.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .config import DATA_DIR, DEFAULT_MODEL

logger = logging.getLogger(__name__)


async def run_onboard_pipeline(
    page_id: str,
    notion_token: str,
    parent_page_id: str,
    github_token: str | None,
    model: str = DEFAULT_MODEL,
    *,
    on_status: str | None = None,
) -> tuple[str, str, str] | None:
    """
    Run the full onboarding flow for a single DB row.

    Args:
        page_id:        Notion page ID of the DB row.
        notion_token:   Notion integration token.
        parent_page_id: Notion page ID of the onboarding hub.
        github_token:   GitHub PAT (can be None for public repos).
        model:          Claude model to use.
        on_status:      If provided, only process if current Status matches.
                        Pass ``"Ready"`` for poller/webhook guard.  Pass
                        ``None`` to skip the guard (manual CLI trigger).

    Returns:
        ``(wiki_url, wiki_page_id, wiki_text)`` on success, or ``None`` if
        the row was skipped (wrong status, empty name, etc.).
    """
    from .agent import run_onboarding_agent
    from .github.fetcher import GithubFetcher
    from .notion.mcp_session import NotionMCPSession

    fetcher = GithubFetcher(token=github_token)

    async with NotionMCPSession(token=notion_token) as session:
        # Optional status guard
        if on_status is not None:
            status = await session.fetch_page_status(page_id)
            if status != on_status:
                logger.info("Pipeline: page %s has Status=%r (expected %r) — skipping",
                            page_id, status, on_status)
                return None

        hire = await session.fetch_hire_row(page_id)
        if not hire.name:
            logger.warning("Pipeline: row %s has no name — skipping", page_id)
            return None

        logger.info("Pipeline: starting onboard for %s (%s)", hire.name, hire.role)
        await session.update_hire_row(page_id, "Processing")

        try:
            wiki_url, wiki_page_id, wiki = await run_onboarding_agent(
                hire=hire,
                fetcher=fetcher,
                notion_session=session,
                parent_page_id=parent_page_id,
                model=model,
            )
        except Exception:
            logger.exception("Pipeline: agent failed for %s — rolling back to Ready", hire.name)
            await session.update_hire_row(page_id, "Ready")
            raise

        await session.update_hire_row(page_id, "Done", wiki_url=wiki_url)
        logger.info("Pipeline: wiki created for %s — %s", hire.name, wiki_url)

        # Index embeddings for RAG chat
        _index_embeddings(wiki, wiki_page_id, hire.repo_urls, fetcher)

        # Notify the new hire (after indexing so the Slack bot can answer immediately)
        from .notify import notify_hire
        notify_hire(hire, wiki_url, wiki_page_id=wiki_page_id)

    return wiki_url, wiki_page_id, wiki


def _index_embeddings(
    wiki, wiki_page_id: str, repo_urls: list[str], fetcher
) -> None:
    """Best-effort: embed the wiki for RAG chat. Failures are logged, never fatal."""
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        logger.info("Pipeline: GEMINI_API_KEY not set — skipping embeddings")
        return
    if not wiki_page_id:
        return

    try:
        from .embeddings import index_wiki

        image_urls: list[str] = []
        for repo_url in repo_urls:
            try:
                image_urls.extend(fetcher.get_image_urls_from_readme(repo_url))
            except Exception:
                pass

        data_dir = DATA_DIR
        index_wiki(wiki, wiki_page_id, data_dir, image_urls=image_urls)
        logger.info("Pipeline: embeddings indexed for wiki %s", wiki_page_id)
    except Exception:
        logger.exception("Pipeline: embedding failed for wiki %s", wiki_page_id)
