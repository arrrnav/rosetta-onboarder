"""
Wiki refresh logic — Milestone 5.

light_refresh — appends updated issues + PRs to the existing wiki page,
                appends new chunks to the VectorStore, sends Slack DM.

full_refresh  — runs the refresh agent to create a new wiki page, moves
                the old wiki to the graveyard, updates the DB row and
                slack_wiki_map.json, rebuilds the VectorStore, sends Slack DM.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .github.fetcher import GithubFetcher
from .notion.mcp_session import NotionMCPSession
from .notion.models import OnboardingInput

logger = logging.getLogger(__name__)


async def light_refresh(
    hire: OnboardingInput,
    wiki_page_id: str,
    wiki_url: str,
    fetcher: GithubFetcher,
    notion_session: NotionMCPSession,
    data_dir: Path,
) -> None:
    """
    Append updated issues and PRs to the existing wiki page.

    Steps:
    1. For each repo, fetch current open issues and recent PRs.
    2. Append [Refreshed] sections to the wiki page.
    3. Append new text chunks to the VectorStore pickle.
    4. Send Slack DM via notify_light_refresh().
    """
    new_chunks: list[str] = []

    for repo_url in hire.repo_urls:
        repo_name = repo_url.rstrip("/").split("/")[-1]

        # Fetch and format issues
        try:
            issues = fetcher.get_issues(repo_url, limit=10)
            if issues:
                issues_text = "\n".join(
                    f"#{i['number']}: {i['title']} — {i['url']}" for i in issues
                )
                await notion_session.append_updated_section(
                    wiki_page_id,
                    f"Open Issues — {repo_name}",
                    issues_text,
                )
                new_chunks.append(
                    f"## [Refreshed] Open Issues — {repo_name}\n\n{issues_text}"
                )
                logger.info("Light refresh: appended %d issues for %s", len(issues), repo_name)
        except Exception:
            logger.exception("Light refresh: failed to fetch issues for %s", repo_url)

        # Fetch and format PRs
        try:
            prs = fetcher.get_recent_prs(repo_url, state="all", limit=5)
            if prs:
                prs_text = "\n".join(
                    f"#{p['number']}: {p['title']} [{p['state']}] — {p['url']}" for p in prs
                )
                await notion_session.append_updated_section(
                    wiki_page_id,
                    f"Recent PRs — {repo_name}",
                    prs_text,
                )
                new_chunks.append(
                    f"## [Refreshed] Recent PRs — {repo_name}\n\n{prs_text}"
                )
                logger.info("Light refresh: appended %d PRs for %s", len(prs), repo_name)
        except Exception:
            logger.exception("Light refresh: failed to fetch PRs for %s", repo_url)

    # Append new chunks to VectorStore
    if new_chunks:
        try:
            from .embeddings import append_chunks_to_store
            append_chunks_to_store(wiki_page_id, new_chunks, data_dir)
        except FileNotFoundError:
            logger.warning(
                "Light refresh: no VectorStore found for wiki %s — chat context not updated",
                wiki_page_id,
            )
        except Exception:
            logger.exception("Light refresh: embedding append failed for wiki %s", wiki_page_id)

    # Notify
    try:
        from .notify import notify_light_refresh
        notify_light_refresh(hire, wiki_url)
    except Exception:
        logger.exception("Light refresh: Slack notification failed for %s", hire.name)

    logger.info("Light refresh complete for %s", hire.name)


async def full_refresh(
    hire: OnboardingInput,
    db_row_id: str,
    old_wiki_page_id: str,
    fetcher: GithubFetcher,
    notion_session: NotionMCPSession,
    parent_page_id: str,
    data_dir: Path,
    model: str | None = None,
) -> None:
    """
    Regenerate the wiki from scratch, archive the old page, and rebuild the VectorStore.

    Steps:
    1. Run run_refresh_agent() to create a new wiki page.
    2. Move old_wiki_page_id to NOTION_GRAVEYARD_PAGE_ID (if set).
    3. Update the DB row with the new wiki_url.
    4. Rebuild the VectorStore for the new wiki_page_id.
    5. Send Slack DM via notify_full_refresh().
    """
    from .agent import run_refresh_agent

    # 1. Generate new wiki
    logger.info("Full refresh: running agent for %s", hire.name)
    wiki_url, new_wiki_page_id, wiki = await run_refresh_agent(
        hire=hire,
        fetcher=fetcher,
        notion_session=notion_session,
        parent_page_id=parent_page_id,
        model=model,
    )

    # 2. Move old wiki to graveyard
    graveyard_id = os.getenv("NOTION_GRAVEYARD_PAGE_ID", "")
    if graveyard_id and old_wiki_page_id:
        try:
            await notion_session.move_page(old_wiki_page_id, graveyard_id)
            logger.info("Full refresh: archived old wiki %s to graveyard", old_wiki_page_id)
        except Exception:
            logger.exception(
                "Full refresh: could not move old wiki %s to graveyard — continuing",
                old_wiki_page_id,
            )
    else:
        if not graveyard_id:
            logger.warning(
                "Full refresh: NOTION_GRAVEYARD_PAGE_ID not set — old wiki left in place"
            )

    # 3. Update DB row with new wiki URL
    try:
        await notion_session.update_hire_row(db_row_id, "Done", wiki_url=wiki_url)
        logger.info("Full refresh: updated DB row %s with new wiki URL", db_row_id)
    except Exception:
        logger.exception("Full refresh: failed to update DB row %s", db_row_id)

    # 4. Rebuild VectorStore
    try:
        from .embeddings import index_wiki
        image_urls: list[str] = []
        for repo_url in hire.repo_urls:
            try:
                image_urls.extend(fetcher.get_image_urls_from_readme(repo_url))
            except Exception:
                pass
        index_wiki(wiki, new_wiki_page_id, data_dir, image_urls=image_urls)
        logger.info("Full refresh: VectorStore rebuilt for wiki %s", new_wiki_page_id)
    except Exception:
        logger.exception(
            "Full refresh: embedding failed for wiki %s — chat context not updated",
            new_wiki_page_id,
        )

    # 5. Notify
    try:
        from .notify import notify_full_refresh
        notify_full_refresh(hire, wiki_url, new_wiki_page_id=new_wiki_page_id)
    except Exception:
        logger.exception("Full refresh: Slack notification failed for %s", hire.name)

    logger.info("Full refresh complete for %s — new wiki: %s", hire.name, wiki_url)
