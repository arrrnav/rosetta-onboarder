# `onboarder/github/fetcher.py`

## What it is

A thin, opinionated wrapper around PyGithub that exposes exactly the data the agent needs — nothing more. All methods accept a full GitHub URL and return plain Python dicts or strings, ready to be passed to Claude as tool results.

## Why it exists

The agent should be able to call `fetch_github_readme` and get back a string of README content — it shouldn't have to know about PyGithub's `Repository` object, base64 decoding, or rate limit headers. This module absorbs all of that so `tools.py` stays clean.

---

## Public API

### `GithubFetcher`

```python
fetcher = GithubFetcher(token="ghp_...")  # token is optional but strongly recommended
```

| Method | Returns | Description |
|---|---|---|
| `get_readme(repo_url)` | `str` | Full decoded README content, or `"(No README found)"` |
| `get_structure(repo_url, max_depth=2)` | `list[dict]` | File tree up to `max_depth` levels deep |
| `get_issues(repo_url, label?, limit=10)` | `list[dict]` | Open issues, optionally filtered by label |
| `get_recent_prs(repo_url, state="all", limit=5)` | `list[dict]` | Recent PRs sorted by last-updated |
| `get_contributing(repo_url)` | `str \| None` | CONTRIBUTING.md content, or `None` |
| `get_repo_metadata(repo_url)` | `dict` | Name, description, language, stars, topics |
| `get_image_urls_from_readme(repo_url)` | `list[str]` | Image URLs extracted from the README |

---

## Return shapes

### `get_structure` entry
```json
{"path": "src/api/routes.py", "type": "blob", "size": 3412}
```
`type` is `"blob"` (file) or `"tree"` (directory).

### `get_issues` entry
```json
{
  "number": 42,
  "title": "Add pagination to /users endpoint",
  "url": "https://github.com/org/repo/issues/42",
  "labels": ["good first issue", "backend"],
  "body_preview": "Currently the endpoint returns all users...",
  "created_at": "2024-11-01T10:22:00"
}
```

### `get_recent_prs` entry
```json
{
  "number": 87,
  "title": "Refactor auth middleware",
  "url": "https://github.com/org/repo/pull/87",
  "state": "closed",
  "author": "jsmith",
  "merged": true,
  "updated_at": "2024-12-15T14:05:00",
  "body_preview": "This PR extracts the JWT validation logic..."
}
```

### `get_repo_metadata` result
```json
{
  "name": "payments",
  "full_name": "acme/payments",
  "description": "Stripe integration and billing service",
  "language": "Python",
  "stars": 12,
  "topics": ["payments", "stripe", "fastapi"],
  "default_branch": "main",
  "url": "https://github.com/acme/payments"
}
```

---

## Implementation notes

### Git Trees API vs. recursive directory walking

`get_structure()` uses `repo.get_git_tree(branch, recursive=True)` — a single API request that returns every path in the repo — then filters by depth client-side. The alternative, calling `get_contents()` per directory, uses one API request per folder, which exhausts the rate limit on any non-trivial repo.

### Rate limit handling

`_check_rate_limit()` is called before every `get_repo()` call. If fewer than 10 requests remain in the current window, the method sleeps until the window resets rather than raising an exception. This is intentional: the `watch` polling loop should recover gracefully from a burst of new hires without crashing.

Unauthenticated requests are limited to 60/hour. A `GITHUB_TOKEN` raises this to 5 000/hour, which is why the token is strongly recommended even for public repos.

### `get_issues` skips PRs

GitHub's Issues API returns pull requests alongside issues (a GitHub quirk — PRs are a superset of issues in their data model). The `if issue.pull_request: continue` guard filters these out so Claude only sees actual issues.

### `get_image_urls_from_readme` and multimodal embeddings

This method is not called during Milestone 1 (wiki generation). It is reserved for Milestone 2, where the Gemini embedder fetches each image URL and embeds it in the same vector space as the wiki text — enabling the chat interface to answer questions like "what does the architecture diagram show?".

---

## Example

```python
from onboarder.github.fetcher import GithubFetcher

fetcher = GithubFetcher(token="ghp_...")

meta = fetcher.get_repo_metadata("https://github.com/acme/payments")
# {"name": "payments", "language": "Python", "stars": 12, ...}

readme = fetcher.get_readme("https://github.com/acme/payments")
# "# Payments Service\n\nHandles Stripe billing..."

tree = fetcher.get_structure("https://github.com/acme/payments", max_depth=2)
# [{"path": "src", "type": "tree"}, {"path": "src/api", "type": "tree"}, ...]

issues = fetcher.get_issues("https://github.com/acme/payments", label="good first issue")
# [{"number": 42, "title": "Add pagination...", ...}]
```
