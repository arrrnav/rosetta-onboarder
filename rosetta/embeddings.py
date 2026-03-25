"""
Gemini RAG layer — Milestone 2.

Builds an in-memory vector store from a WikiPage using Gemini Embedding 2
(gemini-embedding-2-preview), saves it to disk, and retrieves relevant chunks
at chat time via cosine similarity.

Usage::

    # After wiki creation (in main.py):
    from rosetta.embeddings import index_wiki
    index_wiki(wiki, wiki_page_id, data_dir=Path("data"), image_urls=[...])

    # At chat time (in chat/server.py):
    from rosetta.embeddings import VectorStore
    store = VectorStore.load(data_dir / f"{wiki_id}.pkl")
    chunks = store.retrieve("how do I run the tests?")
"""
from __future__ import annotations

import logging
import os
import pickle
import urllib.request
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .notion.models import WikiPage

logger = logging.getLogger(__name__)

EMBED_MODEL = "models/gemini-embedding-2-preview"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _configure_genai():
    """Configure and return the google.generativeai module."""
    import google.generativeai as genai  # lazy import — not everyone runs the server
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to your secrets .env file."
        )
    genai.configure(api_key=api_key)
    return genai


def _embed_text(genai, text: str, task_type: str) -> list[float]:
    result = genai.embed_content(
        model=EMBED_MODEL,
        content=text,
        task_type=task_type,
    )
    return result["embedding"]


def _embed_image(genai, url: str) -> list[float] | None:
    """Fetch a remote image and embed it. Returns None on any failure."""
    try:
        from PIL import Image

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "rosetta-onboarder/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        img = Image.open(BytesIO(data)).convert("RGB")
        result = genai.embed_content(
            model=EMBED_MODEL,
            content=img,
            task_type="RETRIEVAL_DOCUMENT",
        )
        return result["embedding"]
    except Exception as exc:
        logger.warning("Skipping image embed for %s — %s", url, exc)
        return None


# ---------------------------------------------------------------------------
# VectorStore
# ---------------------------------------------------------------------------

@dataclass
class VectorStore:
    """
    Lightweight numpy-backed vector store.

    ``chunks`` are the raw text strings returned to Claude as RAG context.
    ``embeddings`` is a float32 array of shape (n, dim).
    """

    chunks: list[str]
    embeddings: np.ndarray  # shape (n, dim), float32

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        wiki: "WikiPage",
        image_urls: list[str] | None = None,
    ) -> "VectorStore":
        """
        Embed all wiki sections and (optionally) README images.

        One chunk is created per wiki section; image chunks are labelled with
        their source URL so Claude can reference them in answers.

        Args:
            wiki:       The WikiPage produced by the agent.
            image_urls: Optional list of image URLs from README files.
                        Each is fetched and embedded; failures are skipped.
        """
        genai = _configure_genai()
        chunks: list[str] = []
        vecs: list[list[float]] = []

        # Text chunks — one per wiki section
        for section in wiki.sections:
            chunk = f"## {section.heading}\n\n{section.content}"
            chunks.append(chunk)
            vecs.append(_embed_text(genai, chunk, "RETRIEVAL_DOCUMENT"))
            logger.debug("Embedded section '%s'", section.heading)

        # Image chunks — best-effort, never fatal
        for url in (image_urls or []):
            vec = _embed_image(genai, url)
            if vec is not None:
                chunks.append(f"[Image from README: {url}]")
                vecs.append(vec)
                logger.debug("Embedded image %s", url)

        logger.info(
            "VectorStore built: %d text chunks, %d image chunks",
            len(wiki.sections),
            len(chunks) - len(wiki.sections),
        )
        return cls(
            chunks=chunks,
            embeddings=np.array(vecs, dtype=np.float32),
        )

    # ------------------------------------------------------------------
    # Retrieve
    # ------------------------------------------------------------------

    def retrieve(self, query: str, top_k: int = 3) -> list[str]:
        """
        Return the top-k most relevant chunks for the query.

        Uses cosine similarity between the query embedding and all stored
        chunk embeddings.
        """
        genai = _configure_genai()
        q_vec = np.array(
            _embed_text(genai, query, "RETRIEVAL_QUERY"),
            dtype=np.float32,
        )
        norms = np.linalg.norm(self.embeddings, axis=1)
        q_norm = np.linalg.norm(q_vec)
        sims = (self.embeddings @ q_vec) / (norms * q_norm + 1e-8)
        top_k = min(top_k, len(self.chunks))
        top_idx = np.argsort(sims)[-top_k:][::-1]
        return [self.chunks[i] for i in top_idx]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Pickle the store to disk. Creates parent directories as needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            pickle.dump(self, fh)
        logger.info("VectorStore saved → %s (%d chunks)", path, len(self.chunks))

    @classmethod
    def load(cls, path: Path) -> "VectorStore":
        """Load a previously saved VectorStore from disk."""
        with open(path, "rb") as fh:
            store = pickle.load(fh)
        logger.info("VectorStore loaded ← %s (%d chunks)", path, len(store.chunks))
        return store


# ---------------------------------------------------------------------------
# Top-level helper used by main.py
# ---------------------------------------------------------------------------

def index_wiki(
    wiki: "WikiPage",
    wiki_page_id: str,
    data_dir: Path,
    image_urls: list[str] | None = None,
) -> None:
    """
    Build a VectorStore for *wiki* and persist it to *data_dir/<wiki_page_id>.pkl*.

    Called by ``rosetta onboard`` immediately after the wiki page is created.
    The chat server reads the file on the first request for that wiki_id.

    Args:
        wiki:          The WikiPage produced by the agent.
        wiki_page_id:  Notion page ID — used as the filename stem.
        data_dir:      Directory to store pickle files (created if absent).
        image_urls:    Optional README image URLs to include as multimodal chunks.
    """
    store = VectorStore.build(wiki, image_urls=image_urls)
    store.save(data_dir / f"{wiki_page_id}.pkl")
