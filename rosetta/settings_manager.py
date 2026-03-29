"""
Settings manager — prompt for and persist agent/refresh settings.

Extracted from main.py to keep the CLI command thin.
"""
from __future__ import annotations

from pathlib import Path

import questionary
from dotenv import dotenv_values, find_dotenv, set_key

from .cli_helpers import QUESTIONARY_STYLE, console
from .config import SETTINGS_SCHEMA


def prompt_and_save() -> None:
    """Interactive settings editor: prompt for each setting, save changes to .env."""
    env_path = Path(find_dotenv(usecwd=True) or ".env")
    current = {k: v for k, v in dotenv_values(env_path).items() if v}

    console.print("\n[bold]Agent & refresh settings[/bold]  [dim](Enter to keep current value)[/dim]\n")

    updated: dict[str, str] = {}

    for s in SETTINGS_SCHEMA:
        key      = s["key"]
        label    = s["label"]
        kind     = s["type"]
        fallback = s["default"]
        cur      = current.get(key, fallback)

        if kind == "select":
            choices = s["choices"]
            ordered = [cur] + [c for c in choices if c != cur]
            val = questionary.select(label, choices=ordered, style=QUESTIONARY_STYLE).ask()
            if val is None:
                _cancel()

        elif kind == "bool":
            val_bool = questionary.confirm(
                label,
                default=(cur.lower() == "true"),
                style=QUESTIONARY_STYLE,
            ).ask()
            if val_bool is None:
                _cancel()
            val = "true" if val_bool else "false"

        else:  # text or int
            val = questionary.text(label, default=cur, style=QUESTIONARY_STYLE).ask()
            if val is None:
                _cancel()
            val = val.strip() or cur
            if kind == "int":
                try:
                    int(val)
                except ValueError:
                    console.print(f"  [red]✗[/red]  {val!r} is not a valid integer — keeping {cur}")
                    val = cur

        if val != cur:
            updated[key] = val

    if not updated:
        console.print("\n[dim]No changes.[/dim]\n")
        return

    # Show diff
    console.print()
    for key, val in updated.items():
        label = next(s["label"] for s in SETTINGS_SCHEMA if s["key"] == key)
        old   = current.get(key, next(s["default"] for s in SETTINGS_SCHEMA if s["key"] == key))
        console.print(f"  [green]✔[/green]  {label}: [dim]{old}[/dim] → [bold]{val}[/bold]")

    console.print()
    confirm = questionary.confirm("Save to .env?", default=True, style=QUESTIONARY_STYLE).ask()
    if not confirm:
        console.print("[dim]Cancelled — .env was not changed.[/dim]\n")
        return

    env_path.touch(exist_ok=True)
    for key, val in updated.items():
        set_key(str(env_path), key, val)

    console.print("[bold green]Saved.[/bold green]\n")


def _cancel() -> None:
    console.print("\n[dim]Cancelled — .env was not changed.[/dim]\n")
    import typer
    raise typer.Exit()
