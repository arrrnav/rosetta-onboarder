"""
Claude agentic loop for onboarding wiki generation.

The agent receives an OnboardingInput, researches the assigned GitHub repos by
calling tools on demand, and terminates by calling create_notion_wiki exactly
once.  The loop exits as soon as that tool is dispatched.

Typical call sequence per repo:
  fetch_github_metadata → fetch_github_readme → fetch_github_structure
  → fetch_github_issues (label="good first issue")
  → fetch_github_prs → fetch_github_contributing
  → [repeat for each repo]
  → create_notion_wiki  (once, with all 8 sections)
"""
from __future__ import annotations

import logging
import os
from typing import Any

import anthropic

from .github.fetcher import GithubFetcher
from .notion.mcp_session import NotionMCPSession
from .notion.models import OnboardingInput, WikiPage
from .tools import TOOL_DEFINITIONS, ToolDispatcher

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert engineering onboarding specialist. Your job is to create a \
comprehensive, personalised onboarding wiki for a new hire joining an engineering team.

You have access to GitHub tools. Use them to research the assigned repositories \
before writing the wiki — the quality of your wiki depends on how well you understand \
the codebases.

Research strategy (per repo):
1. Call fetch_github_metadata first — quick orientation (language, stars, description)
2. Call fetch_github_readme — understand purpose and setup
3. Call fetch_github_structure — understand codebase layout
4. Call fetch_github_issues with label="good first issue" — find starter tasks
5. Call fetch_github_prs — understand recent activity and team conventions
6. Call fetch_github_contributing — extract contribution guidelines if they exist

When you have gathered enough information across all repos, call create_notion_wiki \
ONCE with all 8 sections. Do not call it until you are ready to write a complete, \
high-quality wiki.

Wiki sections (include all 8, in this order):
1. Welcome & Overview — warm, personalised welcome; role context; what to expect in week 1
2. Your Repositories — purpose and scope of each assigned repo
3. Codebase Architecture — how the code is organised; key directories and what lives in them
4. Getting Started — local dev setup synthesised from READMEs; step-by-step instructions
5. Good First Issues — specific issues the new hire should look at first, with context
6. Team Conventions — coding standards, PR process, commit style, review culture
7. Recent Activity — what the team has been working on recently (from recent PRs)
8. Resources & Links — links to repos, issue trackers, docs, and any other useful resources

Tone: warm, direct, and specific. Avoid generic filler. Tailor every section to \
this person's role and their specific repos.\
"""


def _user_message(hire: OnboardingInput) -> str:
    repos = "\n".join(f"  - {url}" for url in hire.repo_urls)
    notes = f"\n\nAdditional notes from the team lead:\n{hire.notes}" if hire.notes.strip() else ""
    return (
        f"Please create an onboarding wiki for:\n\n"
        f"Name: {hire.name}\n"
        f"Role: {hire.role}\n"
        f"Assigned repositories:\n{repos}"
        f"{notes}\n\n"
        f"Research the repositories using the available tools, then call "
        f"create_notion_wiki to produce the final wiki."
    )


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

async def run_onboarding_agent(
    hire: OnboardingInput,
    fetcher: GithubFetcher,
    notion_session: NotionMCPSession,
    parent_page_id: str,
    model: str | None = None,
    max_iterations: int = 15,
) -> tuple[str, str, WikiPage]:
    """
    Run the Claude agentic loop for one new hire.

    Calls tools until ``create_notion_wiki`` is dispatched, then returns the
    wiki URL, wiki page ID, and the ``WikiPage`` object.

    Args:
        hire:             Parsed new hire data from the Notion DB row.
        fetcher:          Initialised GithubFetcher.
        notion_session:   Active NotionMCPSession (must be used inside ``async with``).
        parent_page_id:   Notion page ID of the "Engineering Onboarding" parent page.
        model:            Claude model string. Defaults to CLAUDE_MODEL env var or
                          ``claude-sonnet-4-6``.
        max_iterations:   Safety cap on the number of Claude API calls.

    Returns:
        (wiki_url, wiki_page_id, wiki_page) — the Notion URL, raw page ID, and the
        WikiPage object (for downstream RAG indexing).

    Raises:
        RuntimeError: if the agent exhausts max_iterations without writing the wiki.
    """
    resolved_model = model or os.getenv("CLAUDE_MODEL")#, "claude-sonnet-4-6")
    client = anthropic.AsyncAnthropic()

    dispatcher = ToolDispatcher(
        fetcher=fetcher,
        notion_session=notion_session,
        parent_page_id=parent_page_id,
        max_issues=int(os.getenv("GITHUB_MAX_ISSUES", 10)),
        max_prs=int(os.getenv("GITHUB_MAX_PRS", 5)),
        tree_depth=int(os.getenv("GITHUB_TREE_DEPTH", 2)),
    )

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": _user_message(hire)},
    ]

    wiki_url: str = ""

    for iteration in range(max_iterations):
        logger.info("[%s] agent iteration %d/%d", hire.name, iteration + 1, max_iterations)

        response = await client.messages.create(
            model=resolved_model,
            max_tokens=16000,
            system=_SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        logger.debug("stop_reason=%s blocks=%d", response.stop_reason, len(response.content))

        # Append assistant turn — preserves tool_use blocks required for the next turn
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason != "tool_use":
            logger.warning("Unexpected stop_reason %r — stopping loop", response.stop_reason)
            break

        # Execute every tool_use block in this turn
        tool_results: list[dict[str, Any]] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            logger.info("[%s] → %s(%s)", hire.name, block.name,
                        str(block.input)[:120].replace("\n", " "))
            result_text = await dispatcher.dispatch(block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_text,
            })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        # Exit as soon as the wiki is written — no need for another Claude turn
        if dispatcher.created_wiki is not None:
            # Extract the URL from the last tool result
            for tr in reversed(tool_results):
                content = tr.get("content", "")
                if "URL:" in content:
                    wiki_url = content.split("URL:", 1)[1].strip()
                    break
            logger.info("[%s] wiki created — %s", hire.name, wiki_url)
            break

    if dispatcher.created_wiki is None:
        raise RuntimeError(
            f"Agent exhausted {max_iterations} iterations for {hire.name!r} "
            f"without calling create_notion_wiki"
        )

    return wiki_url, dispatcher.created_wiki_page_id, dispatcher.created_wiki
