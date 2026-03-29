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

_genai_client = None


def _configure_genai():
    """Create (or return cached) google.genai Client."""
    global _genai_client
    if _genai_client is not None:
        return _genai_client
    from google import genai  # lazy import — not everyone runs the server
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to your secrets .env file."
        )
    _genai_client = genai.Client(api_key=api_key)
    return _genai_client


def _embed_text(client, text: str, task_type: str) -> list[float]:
    from google.genai import types
    result = client.models.embed_content(
        model=EMBED_MODEL,
        contents=text,
        config=types.EmbedContentConfig(task_type=task_type),
    )
    return result.embeddings[0].values


def _embed_image(client, url: str) -> list[float] | None:
    """Fetch a remote image and embed it. Returns None on any failure."""
    try:
        from google.genai import types

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "rosetta-onboarder/1.0"},
        )
        _SUPPORTED = {"image/jpeg", "image/png", "image/gif", "image/webp"}
        with urllib.request.urlopen(req, timeout=10) as resp:
            content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            data = resp.read()
        # Prefer Content-Type header; fall back to URL extension
        if content_type in _SUPPORTED:
            mime_type = content_type
        elif url.lower().endswith(".png"):
            mime_type = "image/png"
        elif url.lower().endswith(".gif"):
            mime_type = "image/gif"
        elif url.lower().endswith(".webp"):
            mime_type = "image/webp"
        elif url.lower().endswith((".jpg", ".jpeg")):
            mime_type = "image/jpeg"
        else:
            logger.debug("Skipping image embed for %s — unsupported type %r", url, content_type or "unknown")
            return None
        result = client.models.embed_content(
            model=EMBED_MODEL,
            contents=types.Part.from_bytes(data=data, mime_type=mime_type),
            config=types.EmbedContentConfig(task_type="RETRIEVAL_DOCUMENT"),
        )
        return result.embeddings[0].values
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
        client = _configure_genai()
        chunks: list[str] = []
        vecs: list[list[float]] = []

        # Text chunks — one per wiki section
        for section in wiki.sections:
            chunk = f"## {section.heading}\n\n{section.content}"
            chunks.append(chunk)
            vecs.append(_embed_text(client, chunk, "RETRIEVAL_DOCUMENT"))
            logger.debug("Embedded section '%s'", section.heading)

        # Image chunks — best-effort, never fatal
        for url in (image_urls or []):
            vec = _embed_image(client, url)
            if vec is not None:
                chunks.append(f"[Image from README: {url}]")
                vecs.append(vec)
                logger.debug("Embedded image %s", url)

        logger.info(
            "VectorStore built: %d text chunks, %d image chunks",
            len(wiki.sections),
            len(chunks) - len(wiki.sections),
        )
        if not chunks:
            logger.warning("No chunks to embed — creating empty VectorStore")
            return cls(chunks=[], embeddings=np.empty((0, 0), dtype=np.float32))
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
        if len(self.chunks) == 0:
            return []
        client = _configure_genai()
        q_vec = np.array(
            _embed_text(client, query, "RETRIEVAL_QUERY"),
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

def append_chunks_to_store(
    wiki_page_id: str,
    new_chunks: list[str],
    data_dir: Path,
) -> None:
    """
    Load an existing VectorStore, embed new_chunks, append them, and save back.

    Used by the light refresh to add updated issues/PR chunks without
    re-embedding the entire wiki from scratch.

    Raises:
        FileNotFoundError: if no pkl exists for wiki_page_id (wiki never indexed).
    """
    pkl_path = data_dir / f"{wiki_page_id}.pkl"
    if not pkl_path.exists():
        raise FileNotFoundError(
            f"No VectorStore found for wiki {wiki_page_id!r} at {pkl_path}. "
            "The wiki may not have been indexed yet."
        )
    if not new_chunks:
        logger.info("append_chunks_to_store: no chunks to append for %s", wiki_page_id)
        return

    store = VectorStore.load(pkl_path)
    client = _configure_genai()
    new_vecs: list[list[float]] = []
    for chunk in new_chunks:
        new_vecs.append(_embed_text(client, chunk, "RETRIEVAL_DOCUMENT"))

    new_arr = np.array(new_vecs, dtype=np.float32)
    store.chunks.extend(new_chunks)
    if store.embeddings.size == 0:
        # Existing store was empty (e.g. wiki had zero sections)
        store.embeddings = new_arr
    else:
        store.embeddings = np.vstack([store.embeddings, new_arr])
    store.save(pkl_path)
    logger.info(
        "Appended %d chunks to VectorStore for wiki %s (%d total)",
        len(new_chunks), wiki_page_id, len(store.chunks),
    )


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
