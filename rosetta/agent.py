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

_REFRESH_SYSTEM_PROMPT = """\
You are a technical documentation updater. Your job is to refresh an existing \
engineering onboarding wiki with current information from GitHub.

You have access to GitHub tools. Use them to fetch up-to-date content for the \
assigned repositories. Do NOT call fetch_github_contributing — that information \
is stable and does not need refreshing.

Research strategy (per repo):
1. Call fetch_github_metadata — check for description or language changes
2. Call fetch_github_readme — capture significant README updates
3. Call fetch_github_structure — note any structural changes
4. Call fetch_github_issues with label="good first issue" — current starter tasks
5. Call fetch_github_prs — latest pull request activity

When you have gathered current information across all repos, call create_notion_wiki \
ONCE with all 8 sections. Write factually and concisely. Reflect the current state \
of the repositories. Do not include a welcome message or first-week guidance.

Wiki sections (include all 8, same order):
1. Overview — updated repository purposes and current state
2. Your Repositories — current scope and any notable changes
3. Codebase Architecture — current organisation; note structural changes
4. Getting Started — current setup instructions from README
5. Good First Issues — current open issues tagged for contribution
6. Team Conventions — current PR process and coding standards
7. Recent Activity — latest PRs and what they changed
8. Resources & Links — current links to repos, trackers, and docs

Tone: factual and direct. No filler. No welcome tone.\
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


def _refresh_user_message(hire: OnboardingInput) -> str:
    repos = "\n".join(f"  - {url}" for url in hire.repo_urls)
    return (
        f"Please refresh the onboarding wiki for:\n\n"
        f"Name: {hire.name}\n"
        f"Role: {hire.role}\n"
        f"Assigned repositories:\n{repos}\n\n"
        f"Fetch current information from the repositories using the available tools "
        f"(skip fetch_github_contributing), then call create_notion_wiki to produce "
        f"the updated wiki."
    )


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

async def _run_agent_loop(
    system_prompt: str,
    initial_user_message: str,
    hire: OnboardingInput,
    fetcher: GithubFetcher,
    notion_session: NotionMCPSession,
    parent_page_id: str,
    model: str | None = None,
    max_iterations: int = 15,
) -> tuple[str, str, WikiPage]:
    """Shared agent loop — used by both onboarding and refresh agents."""
    resolved_model = model or os.getenv("CLAUDE_MODEL")
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
        {"role": "user", "content": initial_user_message},
    ]

    wiki_url: str = ""

    for iteration in range(max_iterations):
        logger.info("[%s] agent iteration %d/%d", hire.name, iteration + 1, max_iterations)

        response = await client.messages.create(
            model=resolved_model,
            max_tokens=16000,
            system=system_prompt,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        logger.debug("stop_reason=%s blocks=%d", response.stop_reason, len(response.content))
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break
        if response.stop_reason != "tool_use":
            logger.warning("Unexpected stop_reason %r — stopping loop", response.stop_reason)
            break

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

        if dispatcher.created_wiki is not None:
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

    Returns (wiki_url, wiki_page_id, wiki_page).
    Raises RuntimeError if the agent exhausts max_iterations without writing the wiki.
    """
    return await _run_agent_loop(
        system_prompt=_SYSTEM_PROMPT,
        initial_user_message=_user_message(hire),
        hire=hire,
        fetcher=fetcher,
        notion_session=notion_session,
        parent_page_id=parent_page_id,
        model=model,
        max_iterations=max_iterations,
    )


async def run_refresh_agent(
    hire: OnboardingInput,
    fetcher: GithubFetcher,
    notion_session: NotionMCPSession,
    parent_page_id: str,
    model: str | None = None,
    max_iterations: int = 15,
) -> tuple[str, str, WikiPage]:
    """
    Run the Claude agentic loop to produce a refreshed wiki for an existing hire.

    Uses the refresh system prompt (concise, no welcome tone, skips contributing).
    Returns (wiki_url, wiki_page_id, wiki_page) — same contract as run_onboarding_agent.
    """
    return await _run_agent_loop(
        system_prompt=_REFRESH_SYSTEM_PROMPT,
        initial_user_message=_refresh_user_message(hire),
        hire=hire,
        fetcher=fetcher,
        notion_session=notion_session,
        parent_page_id=parent_page_id,
        model=model,
        max_iterations=max_iterations,
    )
