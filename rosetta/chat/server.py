"""
FastAPI server — Milestone 2 (chat) + Milestone 3 (webhook trigger).

Start with:
    rosetta serve
    (or directly: uvicorn rosetta.chat.server:app --host 0.0.0.0 --port 8000)

Routes:
    GET  /health               — liveness probe
    GET  /chat/{wiki_id}       — serves the chat HTML page
    POST /chat/{wiki_id}       — {"question": str} → {"answer": str}
    POST /webhook/notion       — Notion webhook receiver (page.properties_updated)

The server loads VectorStore pickles from CHAT_DATA_DIR (default: ./data).
Stores are cached in memory after the first load per wiki_id.

Webhook flow (M3):
    Notion fires page.properties_updated when a DB row is edited.
    The handler verifies the HMAC-SHA256 signature, checks if Status=Ready,
    and runs the full onboard flow as a FastAPI BackgroundTask so Notion
    gets an immediate 200 response.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

import anthropic
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..embeddings import VectorStore

logger = logging.getLogger(__name__)

app = FastAPI(title="Rosetta Onboarding Chat")

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

def _data_dir() -> Path:
    return Path(os.getenv("CHAT_DATA_DIR", "data"))


def _get_store(wiki_id: str) -> VectorStore:
    if wiki_id not in _store_cache:
        path = _data_dir() / f"{wiki_id}.pkl"
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
        model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
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
# Webhook endpoint (Milestone 3)
# ---------------------------------------------------------------------------

@app.post("/webhook/notion")
async def notion_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receive Notion webhook events and auto-trigger onboarding.

    Notion fires ``page.properties_updated`` whenever a DB row is edited.
    We verify the HMAC-SHA256 signature, check if the updated page is a
    New Hire Requests row with Status=Ready, and kick off the full onboard
    flow as a background task — returning 200 immediately so Notion doesn't
    retry.

    Set NOTION_WEBHOOK_SECRET to the signing secret from the Notion
    integration dashboard.  If the env var is unset, signature verification
    is skipped (development only).
    """
    body = await request.body()

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # Notion sends a verification_token request before the subscription is active.
    # This request has no signature — handle it first, before the HMAC check.
    verification_token = payload.get("verification_token")
    if verification_token:
        logger.info("=" * 60)
        logger.info("NOTION WEBHOOK VERIFICATION TOKEN: %s", verification_token)
        logger.info("Copy this token → Notion dashboard → Webhooks → Verify → paste it.")
        logger.info("=" * 60)
        return {"ok": True}

    # Verify HMAC-SHA256 signature for all non-verification requests
    secret = os.getenv("NOTION_WEBHOOK_SECRET", "")
    if secret:
        sig_header = request.headers.get("X-Notion-Signature", "")
        expected = "sha256=" + hmac.new(
            secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig_header, expected):
            logger.warning("Webhook: invalid signature — rejecting request")
            raise HTTPException(status_code=401, detail="Invalid signature")

    event_type = payload.get("type", "")
    page_id = payload.get("entity", {}).get("id", "")

    logger.info("Webhook: received event=%s page_id=%s", event_type, page_id)

    if event_type == "page.properties_updated" and page_id:
        background_tasks.add_task(_handle_hire_if_ready, page_id)

    return {"ok": True}


async def _handle_hire_if_ready(page_id: str) -> None:
    """
    Background task: fetch the DB row and run the full onboard flow if Status=Ready.

    Guards against re-processing by checking Status before doing anything.
    Rows that are already Processing or Done are silently skipped.
    """
    from ..agent import run_onboarding_agent
    from ..embeddings import index_wiki
    from ..github.fetcher import GithubFetcher
    from ..notion.mcp_session import NotionMCPSession

    notion_token = os.getenv("NOTION_TOKEN", "")
    parent_page_id = os.getenv("NOTION_ONBOARDING_PAGE_ID", "")
    github_token = os.getenv("GITHUB_TOKEN")
    model = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

    if not notion_token or not parent_page_id:
        logger.error(
            "Webhook: NOTION_TOKEN or NOTION_ONBOARDING_PAGE_ID not set — "
            "cannot run onboard"
        )
        return

    fetcher = GithubFetcher(token=github_token)

    try:
        async with NotionMCPSession(token=notion_token) as session:
            # Guard: only process rows where Status = "Ready"
            status = await session.fetch_page_status(page_id)
            if status != "Ready":
                logger.debug(
                    "Webhook: page %s has Status=%r — skipping", page_id, status
                )
                return

            hire = await session.fetch_hire_row(page_id)
            if not hire.name:
                logger.warning("Webhook: row %s has no name — skipping", page_id)
                return

            logger.info(
                "Webhook: Status=Ready — starting onboard for %s (%s)",
                hire.name, hire.role,
            )
            await session.update_hire_row(page_id, "Processing")

            try:
                wiki_url, wiki_page_id, wiki = await run_onboarding_agent(
                    hire=hire,
                    fetcher=fetcher,
                    notion_session=session,
                    parent_page_id=parent_page_id,
                    model=model,
                )
            except Exception:
                logger.exception("Webhook: agent failed for %s — rolling back", hire.name)
                await session.update_hire_row(page_id, "Ready")
                return

            await session.update_hire_row(page_id, "Done", wiki_url=wiki_url)
            logger.info("Webhook: wiki created for %s — %s", hire.name, wiki_url)

            # Index embeddings + append chat embed
            gemini_key = os.getenv("GEMINI_API_KEY")
            if gemini_key and wiki_page_id:
                try:
                    image_urls: list[str] = []
                    for repo_url in hire.repo_urls:
                        try:
                            image_urls.extend(fetcher.get_image_urls_from_readme(repo_url))
                        except Exception:
                            pass
                    data_dir = Path(os.getenv("CHAT_DATA_DIR", "data"))
                    index_wiki(wiki, wiki_page_id, data_dir, image_urls=image_urls)

                    chat_url = os.getenv("CHAT_SERVER_URL", "").rstrip("/")
                    if chat_url:
                        embed_url = f"{chat_url}/chat/{wiki_page_id}"
                        await session.append_embed_block(wiki_page_id, embed_url)
                        logger.info("Webhook: chat widget embedded at %s", embed_url)
                except Exception:
                    logger.exception(
                        "Webhook: embedding/chat setup failed for %s", hire.name
                    )

    except Exception:
        logger.exception("Webhook: unexpected error processing page %s", page_id)
