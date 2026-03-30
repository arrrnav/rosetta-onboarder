"""
Centralised constants and configuration defaults.

Import from here instead of hardcoding values across modules.
"""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Model & data defaults
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DATA_DIR = Path("data")

# ---------------------------------------------------------------------------
# Status display styles (Rich markup colour names)
# ---------------------------------------------------------------------------

STATUS_STYLES: dict[str, str] = {
    "Done": "green",
    "Ready": "yellow",
    "Processing": "blue",
    "Pending": "dim",
}

# ---------------------------------------------------------------------------
# Settings schema — drives `rosetta settings` command
# ---------------------------------------------------------------------------

SETTINGS_SCHEMA: list[dict] = [
    {
        "key":     "CLAUDE_MODEL",
        "label":   "Claude model",
        "type":    "select",
        "choices": [
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-6",
        ],
        "default": DEFAULT_MODEL,
    },
    {
        "key":     "GITHUB_MAX_ISSUES",
        "label":   "Max GitHub issues fetched per repo",
        "type":    "int",
        "default": "10",
    },
    {
        "key":     "GITHUB_MAX_PRS",
        "label":   "Max GitHub PRs fetched per repo",
        "type":    "int",
        "default": "5",
    },
    {
        "key":     "GITHUB_TREE_DEPTH",
        "label":   "GitHub repo tree depth",
        "type":    "int",
        "default": "2",
    },
    {
        "key":     "REFRESH_ENABLED",
        "label":   "Scheduled Friday refresh",
        "type":    "bool",
        "default": "false",
    },
    {
        "key":     "REFRESH_TIMEZONE",
        "label":   "Refresh timezone",
        "type":    "text",
        "default": "UTC",
    },
]
