"""
FastAPI chat server for the Gemini RAG chat widget — Milestone 2.

Start with:
    rosetta serve
    (or directly: uvicorn rosetta.chat.server:app --host 0.0.0.0 --port 8000)

Routes:
    GET  /health           — liveness probe
    GET  /chat/{wiki_id}   — serves the chat HTML page
    POST /chat/{wiki_id}   — {"question": str} → {"answer": str}

The server loads VectorStore pickles from CHAT_DATA_DIR (default: ./data).
Stores are cached in memory after the first load per wiki_id.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException
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
