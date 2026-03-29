"""
Shared CLI utilities — logging, environment helpers, questionary styles.

Keeps main.py and setup_wizard.py thin by centralising repeated patterns.
"""
from __future__ import annotations

import logging
import os

import questionary
import typer
from rich.console import Console
from rich.logging import RichHandler

console = Console()

# ---------------------------------------------------------------------------
# Verbose logging toggle
# ---------------------------------------------------------------------------

# Set to True to show internal debug output and HTTP access logs.
# Can also be enabled by setting LOG_LEVEL=DEBUG in your .env.
VERBOSE_LOGGING = False


def setup_logging() -> None:
    """Configure the root logger using VERBOSE_LOGGING and LOG_LEVEL env var."""
    if VERBOSE_LOGGING or os.getenv("LOG_LEVEL", "").upper() in ("DEBUG", "INFO"):
        level = os.getenv("LOG_LEVEL", "INFO").upper()
    else:
        level = "WARNING"
    logging.basicConfig(
        level=getattr(logging, level, logging.WARNING),
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def require_env(key: str) -> str:
    """Read a required environment variable, print a clear error and exit if missing."""
    value = os.environ.get(key)
    if not value:
        console.print(f"[bold red]Error:[/bold red] {key} is not set. "
                      f"Add it to your .env file.")
        raise typer.Exit(code=1)
    return value


# ---------------------------------------------------------------------------
# Questionary style (shared across setup_wizard and settings)
# ---------------------------------------------------------------------------

QUESTIONARY_STYLE = questionary.Style([
    ("qmark",       "fg:#00d7ff bold"),
    ("question",    "bold"),
    ("answer",      "fg:#00ff87 bold"),
    ("pointer",     "fg:#00d7ff bold"),
    ("highlighted", "fg:#00d7ff bold"),
    ("selected",    "fg:#00ff87"),
    ("separator",   "fg:#555555"),
    ("instruction", "fg:#555555"),
])
