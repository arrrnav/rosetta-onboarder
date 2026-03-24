# Future Goals

A running list of enhancements that are intentionally out of scope for the current build. Add items here as they come up so nothing gets forgotten.

---

## Setup & Configuration

### Textual TUI for `onboarder setup`

**Current behaviour:** `onboarder setup` doesn't exist yet. Configuration is done by manually editing `.env`.

**Goal:** A proper interactive setup wizard built with [Textual](https://github.com/Textualize/textual) that walks the team lead through first-time configuration and writes `.env` for them. No manual file editing.

**Proposed flow:**
```
Welcome to Notion Onboarder Setup
──────────────────────────────────
[1/5] Paste your Notion integration token:  _____________
[2/5] Paste your GitHub personal access token:  _____________
[3/5] Paste your Anthropic API key:  _____________
[4/5] Paste your Gemini API key:  _____________
[5/5] Select your Engineering Onboarding page from the list below:
      > Engineering Onboarding          (fetched live from Notion)
        Old Onboarding Page
        ...
```

After confirming, writes all values to `.env` and verifies connectivity (pings each API). The team lead never touches a config file.

**What needs to be built:**
- `onboarder/cli/setup.py` — Textual app with a multi-step form
- Connectivity checks: Notion token → list pages, GitHub token → check rate limit, Anthropic/Gemini → lightweight ping
- `onboarder setup` command wired into `main.py`

---

### Rich live output for `onboarder watch`

**Current behaviour:** `onboarder watch` doesn't exist yet.

**Goal:** Use Rich (included with Textual) to render a live dashboard while the watcher runs — a table of pending/in-progress hires, a spinner for active generation, and a log panel showing recent completions.

---

## GitHub Fetcher

### Configurable tree depth

**Current behaviour:** `GITHUB_TREE_DEPTH=2` in `.env` sets a global depth applied to every repo.

**Goal:** Expose `--tree-depth` as an option in `onboarder setup` (written to `.env`) so teams can calibrate the default for their typical repo size. Not a per-hire setting — a single sensible default set once at install time is the right model here.

---

### Expanded CONTRIBUTING.md path search

**Current behaviour:** `fetcher.get_contributing()` checks three hardcoded paths:
```
CONTRIBUTING.md
contributing.md
.github/CONTRIBUTING.md
```

**Goal:** Replace the hardcoded tuple with a `_CONTRIBUTING_PATHS` constant covering the full range of community conventions:
```
CONTRIBUTING.md / contributing.md
CONTRIBUTING.rst / contributing.rst
.github/CONTRIBUTING.md / .github/CONTRIBUTING.rst
CONTRIBUTORS.md / CONTRIBUTORS.rst
docs/contributing.md / docs/CONTRIBUTING.md
docs/contributing.rst
CONTRIBUTE.md
```

This is a small, self-contained change with no API or config implications.

---

*Add new items below this line as they come up during development.*
