"""
FastAPI server — chat, polling, and webhook.

Start with:
    rosetta serve
    (or directly: uvicorn rosetta.chat.server:app --host 0.0.0.0 --port 8000)

Routes:
    GET  /health               — liveness probe
    GET  /chat/{wiki_id}       — serves the chat HTML page
    POST /chat/{wiki_id}       — {"question": str} → {"answer": str}
    POST /webhook/notion       — Notion webhook receiver (page.properties_updated)

The server loads VectorStore pickles from ./data.
Stores are cached in memory after the first load per wiki_id.

Polling loop (default):
    Every 60 seconds, the server queries the New Hire Requests database for rows
    with Status=Ready and triggers the full onboard flow for each one.

Webhook flow (optional — instant trigger):
    Notion fires page.properties_updated when a DB row is edited.
    The handler verifies the HMAC-SHA256 signature, checks if Status=Ready,
    and runs the full onboard flow as a FastAPI BackgroundTask so Notion
    gets an immediate 200 response.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..config import DATA_DIR, DEFAULT_MODEL
from ..embeddings import VectorStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — background tasks
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start background tasks (Slack bot, poller, scheduler) alongside uvicorn."""
    tasks: list[asyncio.Task] = []

    app_token = os.getenv("SLACK_APP_TOKEN", "")
    if app_token:
        from ..slack_bot import start_bot
        tasks.append(asyncio.create_task(start_bot(DATA_DIR)))
        logger.info("Slack bot task created.")

    notion_token = os.getenv("NOTION_TOKEN", "")
    database_id = os.getenv("NOTION_DATABASE_ID", "")
    if notion_token and database_id:
        tasks.append(asyncio.create_task(_poll_pending_hires(notion_token, database_id)))
        logger.info("Pending-hires poller started (60s interval).")

    if os.getenv("REFRESH_ENABLED", "false").lower() == "true":
        parent_page_id = os.getenv("NOTION_ONBOARDING_PAGE_ID", "")
        model = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)
        if notion_token and database_id and parent_page_id:
            from ..scheduler import start_scheduler
            tasks.append(asyncio.create_task(
                start_scheduler(notion_token, database_id, parent_page_id, DATA_DIR, model)
            ))
            logger.info("Scheduler task created (REFRESH_ENABLED=true).")
        else:
            logger.warning(
                "REFRESH_ENABLED=true but NOTION_TOKEN / NOTION_DATABASE_ID / "
                "NOTION_ONBOARDING_PAGE_ID not fully set — scheduler not started."
            )

    yield

    for task in tasks:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------

async def _poll_pending_hires(notion_token: str, database_id: str) -> None:
    """Query the DB every 60s for Status=Ready rows and trigger the pipeline."""
    from ..notion.mcp_session import NotionMCPSession

    while True:
        await asyncio.sleep(60)
        try:
            async with NotionMCPSession(token=notion_token) as session:
                hires = await session.query_pending_hires(database_id)
            for hire in hires:
                logger.info("Poller: found Ready row — triggering onboard for %s", hire.name)
                asyncio.create_task(_run_pipeline_safe(hire.db_row_id))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if isinstance(exc, ExceptionGroup):
                for sub in exc.exceptions:
                    logger.error("Poller: error querying pending hires — %s: %s",
                                 type(sub).__name__, sub)
            else:
                logger.exception("Poller: error querying pending hires")


async def _run_pipeline_safe(page_id: str) -> None:
    """Run the shared pipeline, catching errors so one failure doesn't crash the server."""
    from ..pipeline import run_onboard_pipeline

    notion_token = os.getenv("NOTION_TOKEN", "")
    parent_page_id = os.getenv("NOTION_ONBOARDING_PAGE_ID", "")
    github_token = os.getenv("GITHUB_TOKEN")
    model = os.getenv("CLAUDE_MODEL", DEFAULT_MODEL)

    if not notion_token or not parent_page_id:
        logger.error("NOTION_TOKEN or NOTION_ONBOARDING_PAGE_ID not set — cannot run onboard")
        return

    try:
        await run_onboard_pipeline(
            page_id=page_id,
            notion_token=notion_token,
            parent_page_id=parent_page_id,
            github_token=github_token,
            model=model,
            on_status="Ready",
        )
    except Exception:
        logger.exception("Pipeline failed for page %s", page_id)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Rosetta Onboarding Chat", lifespan=_lifespan)

# In-memory cache: wiki_page_id → VectorStore
_store_cache: dict[str, VectorStore] = {}

# Loaded once on first request
_chat_template: str | None = None

_SYSTEM_PROMPT = """\
You are a helpful onboarding assistant for a new software engineer. \
Answer questions about their assigned repositories, codebase setup, \
team conventions, and onboarding wiki using the provided context.

Be concise, friendly, and practical. Use bullet points for steps. \
If the answer isn't in the context, say so honestly and suggest \
where the engineer might find more information (e.g. README, docs, \
or asking a teammate).\
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_store(wiki_id: str) -> VectorStore:
    if wiki_id not in _store_cache:
        path = DATA_DIR / f"{wiki_id}.pkl"
        if not path.exists():
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No embeddings found for wiki '{wiki_id}'. "
                    "Run 'rosetta onboard <row_id>' first to generate the wiki."
                ),
            )
        _store_cache[wiki_id] = VectorStore.load(path)
    return _store_cache[wiki_id]


def _get_template() -> str:
    global _chat_template
    if _chat_template is None:
        tpl = Path(__file__).parent / "templates" / "chat.html"
        _chat_template = tpl.read_text(encoding="utf-8")
    return _chat_template


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    question: str


class ChatResponse(BaseModel):
    answer: str


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/chat/{wiki_id}", response_class=HTMLResponse)
def chat_page(wiki_id: str):
    """Serve the chat widget HTML with the wiki_id baked in."""
    html = _get_template().replace("{{WIKI_ID}}", wiki_id)
    return HTMLResponse(content=html)


@app.post("/chat/{wiki_id}", response_model=ChatResponse)
def chat(wiki_id: str, req: ChatRequest):
    """Answer a question using RAG over the wiki's VectorStore."""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    store = _get_store(wiki_id)
    chunks = store.retrieve(req.question, top_k=3)
    context = "\n\n---\n\n".join(chunks)

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=os.getenv("CLAUDE_MODEL", DEFAULT_MODEL),
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Context from the onboarding wiki:\n\n{context}\n\n"
                    f"Question: {req.question}"
                ),
            }
        ],
    )
    return ChatResponse(answer=response.content[0].text)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhook/notion")
async def notion_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive Notion webhook events and auto-trigger onboarding."""
    body = await request.body()

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Handle verification token (before HMAC check — this request has no signature)
    verification_token = payload.get("verification_token")
    if verification_token:
        return _handle_verification_token(verification_token)

    # Verify HMAC-SHA256 signature
    _verify_signature(request, body)

    event_type = payload.get("type", "")
    page_id = payload.get("entity", {}).get("id", "")

    logger.info("Webhook: received event=%s page_id=%s", event_type, page_id)

    if event_type == "page.properties_updated" and page_id:
        background_tasks.add_task(_run_pipeline_safe, page_id)

    return {"ok": True}


def _handle_verification_token(token: str) -> dict:
    """Print and auto-write the verification token to .env."""
    print(f"\n{'=' * 60}")
    print(f"  Notion webhook verification token received:")
    print(f"  {token}")
    print(f"  Paste this into the Notion dashboard to activate the webhook.")
    print(f"{'=' * 60}\n", flush=True)

    try:
        from dotenv import find_dotenv, set_key
        env_path = find_dotenv(usecwd=True) or ".env"
        set_key(env_path, "NOTION_WEBHOOK_SECRET", token)
        os.environ["NOTION_WEBHOOK_SECRET"] = token
        print("  NOTION_WEBHOOK_SECRET written to .env and active.\n", flush=True)
    except Exception as exc:
        print(f"  Could not auto-write to .env: {exc}", flush=True)
        print(f"  Add manually: NOTION_WEBHOOK_SECRET={token}\n", flush=True)

    return {"ok": True}


def _verify_signature(request: Request, body: bytes) -> None:
    """Verify the HMAC-SHA256 signature on a webhook payload."""
    secret = os.getenv("NOTION_WEBHOOK_SECRET", "")
    if not secret:
        return
    sig_header = request.headers.get("X-Notion-Signature", "")
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig_header, expected):
        logger.warning("Webhook: invalid signature — rejecting request")
        raise HTTPException(status_code=401, detail="Invalid signature")
