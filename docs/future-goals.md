# Future Goals

A running list of enhancements that are intentionally out of scope for the current build. Add items here as they come up so nothing gets forgotten.

---

## Setup & Configuration

### Textual TUI for `rosetta setup`

**Current behaviour:** `rosetta setup` doesn't exist yet. Configuration is done by manually editing `.env`.

**Goal:** A proper interactive setup wizard built with [Textual](https://github.com/Textualize/textual) that walks the team lead through first-time configuration and writes `.env` for them. No manual file editing.

**Proposed flow:**
```
Welcome to Rosetta Setup
────────────────────────
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

Step 5 should also prompt the team lead to set the "Engineering Onboarding" parent page to **Share to web → Anyone with the link can view** in Notion — with a direct link to the page. Child wikis inherit this setting, so it only needs to be done once. The setup wizard should confirm it's been done before finishing.

**What needs to be built:**
- `rosetta/cli/setup.py` — Textual app with a multi-step form
- Connectivity checks: Notion token → list pages, GitHub token → check rate limit, Anthropic/Gemini → lightweight ping
- `rosetta setup` command wired into `main.py`

---

### Rich live output for `rosetta watch`

**Current behaviour:** `rosetta watch` doesn't exist yet.

**Goal:** Use Rich (included with Textual) to render a live dashboard while the watcher runs — a table of pending/in-progress hires, a spinner for active generation, and a log panel showing recent completions.

---

## GitHub Fetcher

### Configurable tree depth

**Current behaviour:** `GITHUB_TREE_DEPTH=2` in `.env` sets a global depth applied to every repo.

**Goal:** Expose `--tree-depth` as an option in `rosetta setup` (written to `.env`) so teams can calibrate the default for their typical repo size. Not a per-hire setting — a single sensible default set once at install time is the right model here.

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

## Chat RAG

### Index raw README content alongside wiki sections

**Current behaviour:** The chat RAG only indexes the 8 generated wiki sections. Claude already synthesised these from the READMEs, so most questions are covered.

**Goal:** Also chunk and embed the raw README text for each assigned repo. A new hire asking very specific technical questions ("what's the exact flag to skip integration tests?", "which Node version is pinned in .nvmrc?") would get better answers from the source README than from the wiki summary.

**Implementation sketch:**
- After wiki creation, call `fetcher.get_readme(repo_url)` for each repo
- Split into overlapping chunks (~400 tokens, 50-token stride)
- Embed each chunk with `gemini-embedding-2-preview` (same model, `RETRIEVAL_DOCUMENT` task type)
- Label chunks as `"README ({owner}/{repo}): ..."` so Claude can cite the source
- Save alongside wiki section chunks in the same `VectorStore` pickle

**Why deferred:** The wiki sections alone tell a clear demo story. Adding READMEs increases embedding time and Gemini API cost during `rosetta onboard`, and adds chunking complexity. Revisit once M2 is proven.

---

*Add new items below this line as they come up during development.*
