"""
GitHub data fetcher using PyGithub.

All public methods accept a full GitHub URL (https://github.com/owner/repo)
and return plain Python dicts/strings suitable for passing to Claude as tool results.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any

from github import Github, GithubException, RateLimitExceededException
from github.Repository import Repository

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https://github\.com/([^/]+)/([^/\s#?]+)")


class GithubFetcher:
    def __init__(self, token: str | None = None):
        self._gh = Github(token) if token else Github()

    def _parse_url(self, repo_url: str) -> str:
        """Extract 'owner/repo' from a GitHub URL."""
        m = _URL_RE.search(repo_url.rstrip("/"))
        if not m:
            raise ValueError(f"Cannot parse GitHub repo URL: {repo_url!r}")
        return f"{m.group(1)}/{m.group(2)}"

    def _get_repo(self, repo_url: str) -> Repository:
        full_name = self._parse_url(repo_url)
        self._check_rate_limit()
        return self._gh.get_repo(full_name)

    def _check_rate_limit(self) -> None:
        """
        Sleep if fewer than 10 GitHub API requests remain in the current window.

        10 is a conservative buffer — enough for one full tool-call sequence
        (metadata + readme + structure + issues + PRs + contributing = 6 calls)
        with headroom.  We sleep rather than raise so that a long-running
        ``watch`` loop recovers automatically without losing the current job.
        """
        import datetime
        core = self._gh.get_rate_limit().resources.core
        if core.remaining < 10:
            reset_in = (core.reset.replace(tzinfo=None) - datetime.datetime.utcnow()).total_seconds()
            wait = max(reset_in + 2, 0)
            logger.warning("GitHub rate limit nearly exhausted (%d remaining). Waiting %.0fs.", core.remaining, wait)
            time.sleep(wait)

    def get_readme(self, repo_url: str) -> str:
        """Return the decoded README content, or a placeholder if none exists."""
        try:
            repo = self._get_repo(repo_url)
            readme = repo.get_readme()
            return base64.b64decode(readme.content).decode("utf-8", errors="replace")
        except GithubException as e:
            if e.status == 404:
                return "(No README found)"
            raise

    def get_structure(self, repo_url: str, max_depth: int = 2) -> list[dict[str, Any]]:
        """
        Return a flat list of files/dirs up to ``max_depth`` levels deep.

        Uses GitHub's Git Trees API with ``recursive=True`` — a single
        authenticated request that returns the entire tree — then filters
        client-side by depth.  This is significantly cheaper than walking
        directories one ``get_contents()`` call at a time, which would use
        one API request per folder and exhaust the rate limit on large repos.

        ``max_depth=2`` captures top-level files and one level of
        subdirectories, which is enough for Claude to understand repo layout
        without overwhelming the context window.
        """
        try:
            repo = self._get_repo(repo_url)
            tree = repo.get_git_tree(repo.default_branch, recursive=True)
            entries = []
            for item in tree.tree:
                depth = item.path.count("/")
                if depth < max_depth:
                    entries.append({
                        "path": item.path,
                        "type": item.type,  # "blob" or "tree"
                        "size": item.size,
                    })
            return entries
        except GithubException as e:
            logger.error("get_structure failed for %s: %s", repo_url, e)
            return []

    def get_issues(self, repo_url: str, label: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Return open issues, optionally filtered by label."""
        try:
            repo = self._get_repo(repo_url)
            kwargs: dict[str, Any] = {"state": "open"}
            if label:
                kwargs["labels"] = [label]
            issues = []
            for issue in repo.get_issues(**kwargs):
                if issue.pull_request:
                    continue  # skip PRs that appear in issues endpoint
                issues.append({
                    "number": issue.number,
                    "title": issue.title,
                    "url": issue.html_url,
                    "labels": [lb.name for lb in issue.labels],
                    "body_preview": (issue.body or "")[:300],
                    "created_at": issue.created_at.isoformat(),
                })
                if len(issues) >= limit:
                    break
            return issues
        except GithubException as e:
            logger.error("get_issues failed for %s: %s", repo_url, e)
            return []

    def get_recent_prs(self, repo_url: str, state: str = "all", limit: int = 5) -> list[dict[str, Any]]:
        """Return recent pull requests."""
        try:
            repo = self._get_repo(repo_url)
            prs = []
            for pr in repo.get_pulls(state=state, sort="updated", direction="desc"):
                prs.append({
                    "number": pr.number,
                    "title": pr.title,
                    "url": pr.html_url,
                    "state": pr.state,
                    "author": pr.user.login if pr.user else "unknown",
                    "merged": pr.merged,
                    "updated_at": pr.updated_at.isoformat(),
                    "body_preview": (pr.body or "")[:300],
                })
                if len(prs) >= limit:
                    break
            return prs
        except GithubException as e:
            logger.error("get_recent_prs failed for %s: %s", repo_url, e)
            return []

    def get_contributing(self, repo_url: str) -> str | None:
        """Return CONTRIBUTING.md content, or None if it doesn't exist."""
        try:
            repo = self._get_repo(repo_url)
            for path in ("CONTRIBUTING.md", "contributing.md", ".github/CONTRIBUTING.md"):
                try:
                    f = repo.get_contents(path)
                    return base64.b64decode(f.content).decode("utf-8", errors="replace")
                except GithubException as e:
                    if e.status == 404:
                        continue
                    raise
            return None
        except GithubException as e:
            logger.error("get_contributing failed for %s: %s", repo_url, e)
            return None

    def get_repo_metadata(self, repo_url: str) -> dict[str, Any]:
        """Return basic repo metadata (description, stars, language, topics)."""
        try:
            repo = self._get_repo(repo_url)
            return {
                "name": repo.name,
                "full_name": repo.full_name,
                "description": repo.description or "",
                "language": repo.language or "unknown",
                "stars": repo.stargazers_count,
                "topics": repo.get_topics(),
                "default_branch": repo.default_branch,
                "url": repo.html_url,
            }
        except GithubException as e:
            logger.error("get_repo_metadata failed for %s: %s", repo_url, e)
            return {"url": repo_url, "error": str(e)}

    def get_image_urls_from_readme(self, repo_url: str) -> list[str]:
        """
        Extract image URLs embedded in the README.

        Called during Milestone 2 wiki indexing to collect architecture
        diagrams, screenshots, and badges for Gemini multimodal embedding.
        Both Markdown ``![]()`` syntax and HTML ``<img src="">`` tags are
        matched so repos that use either convention are handled.

        Results are deduplicated (preserving order) because the same badge
        URL often appears multiple times at the top of a README.
        """
        readme = self.get_readme(repo_url)
        # Match markdown images: ![alt](url)
        md_images = re.findall(r"!\[.*?\]\((https?://[^\s)]+)\)", readme)
        # Match HTML img tags: <img src="url">
        html_images = re.findall(r'<img[^>]+src=["\']?(https?://[^\s"\']+)', readme)
        return list(dict.fromkeys(md_images + html_images))  # deduplicate, preserve order
