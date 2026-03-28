"""
Asyncio background scheduler for weekly wiki refreshes — Milestone 5.

Starts as a background task alongside the Slack bot in `rosetta serve`.
Wakes up hourly, checks whether it is Friday after 17:00 in REFRESH_TIMEZONE,
and whether it has already run today.  If both conditions are met, queries all
Done hires and runs either a light refresh (odd ISO week) or a full refresh
(even ISO week).

State is persisted to data/scheduler_state.json so restarts do not double-fire.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_SECONDS = 3600  # wake up every hour
_STATE_FILE = "scheduler_state.json"


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_state(data_dir: Path) -> dict:
    """Load scheduler state from data_dir/scheduler_state.json."""
    path = data_dir / _STATE_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_state(data_dir: Path, state: dict) -> None:
    """Persist scheduler state to data_dir/scheduler_state.json."""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / _STATE_FILE).write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Core refresh dispatcher
# ---------------------------------------------------------------------------

async def _do_refresh(
    is_light: bool,
    notion_token: str,
    data_source_id: str,
    parent_page_id: str,
    data_dir: Path,
    model: str | None,
) -> None:
    """
    Query all Done hires and run the appropriate refresh for each.

    Opens a single NotionMCPSession for the entire batch. Errors on individual
    hires are caught and logged — one failure does not block the rest.
    """
    from .github.fetcher import GithubFetcher
    from .notion.mcp_session import NotionMCPSession
    from .refresh import full_refresh, light_refresh

    fetcher = GithubFetcher(token=os.getenv("GITHUB_TOKEN"))
    refresh_type = "light" if is_light else "full"

    async with NotionMCPSession(token=notion_token) as session:
        done_hires = await session.query_done_hires(data_source_id)
        logger.info(
            "[scheduler] %s refresh — %d Done hires found", refresh_type, len(done_hires)
        )

        for hire, wiki_url, wiki_page_id in done_hires:
            logger.info("[scheduler] %s refresh for %s", refresh_type, hire.name)
            try:
                if is_light:
                    await light_refresh(
                        hire=hire,
                        wiki_page_id=wiki_page_id,
                        wiki_url=wiki_url,
                        fetcher=fetcher,
                        notion_session=session,
                        data_dir=data_dir,
                    )
                else:
                    await full_refresh(
                        hire=hire,
                        db_row_id=hire.db_row_id,
                        old_wiki_page_id=wiki_page_id,
                        fetcher=fetcher,
                        notion_session=session,
                        parent_page_id=parent_page_id,
                        data_dir=data_dir,
                        model=model,
                    )
            except Exception:
                logger.exception(
                    "[scheduler] %s refresh failed for %s — skipping", refresh_type, hire.name
                )


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------

async def start_scheduler(
    notion_token: str,
    data_source_id: str,
    parent_page_id: str,
    data_dir: Path,
    model: str | None = None,
) -> None:
    """
    Long-running asyncio coroutine. Intended to be run as asyncio.create_task().

    Wakes every hour. On each wake:
    - Checks REFRESH_ENABLED; skips if false.
    - Checks if today is Friday and current local hour >= 17.
    - Checks if already ran today (via scheduler_state.json).
    - Determines ISO week parity: odd → light refresh, even → full refresh.
    - Runs _do_refresh(), saves today's date to state.

    Handles CancelledError cleanly on uvicorn shutdown.
    """
    logger.info("[scheduler] Started.")

    while True:
        try:
            # Sleep first — prevents accidental immediate fire on server start
            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)

            if os.getenv("REFRESH_ENABLED", "false").lower() != "true":
                continue

            try:
                from zoneinfo import ZoneInfo
            except ImportError:
                from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

            tz = ZoneInfo(os.getenv("REFRESH_TIMEZONE", "UTC"))
            now = datetime.now(tz)

            if now.weekday() != 4:  # 4 = Friday
                continue
            if now.hour < 17:
                continue

            today_str = date.today().isoformat()
            state = _load_state(data_dir)
            if state.get("last_run_date") == today_str:
                logger.debug("[scheduler] Already ran today (%s) — skipping", today_str)
                continue

            iso_week = now.isocalendar().week
            is_light = (iso_week % 2 == 1)  # odd week → light, even → full
            logger.info(
                "[scheduler] Triggering %s refresh (ISO week %d, %s)",
                "light" if is_light else "full",
                iso_week,
                today_str,
            )

            await _do_refresh(
                is_light=is_light,
                notion_token=notion_token,
                data_source_id=data_source_id,
                parent_page_id=parent_page_id,
                data_dir=data_dir,
                model=model,
            )

            _save_state(data_dir, {"last_run_date": today_str})
            logger.info("[scheduler] %s refresh complete.", "Light" if is_light else "Full")

        except asyncio.CancelledError:
            logger.info("[scheduler] Task cancelled — shutting down.")
            raise
        except Exception:
            logger.exception("[scheduler] Unexpected error — will retry next hour.")
