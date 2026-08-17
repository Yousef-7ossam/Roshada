"""Context retrieval — the last stage before the LLM, and the seam it stops at.

:func:`retrieve` returns ranked hits. :func:`retrieve_context` formats them into
a numbered block ready to paste into a prompt, plus the matching source
references.

This is the **engine**, and it is deliberately ungated: it knows nothing about
approved sources or document status, so it can serve any policy laid over it.
:mod:`knowledge.retrieval` is that policy, and :mod:`knowledge.rag` turns what
it returns into a grounded answer.

The numbering of the sources block and of ``sources`` is the same, so a model
citing "[2]" can be resolved back to a real document and section by the caller —
citation that survives past the model's output rather than being asserted by it.
"""
import os
from dataclasses import dataclass, field

from . import store
from .store import IndexSpaceMismatch  # re-exported: callers handle it here

#: Character budget for the assembled context. A retrieval layer that returns
#: everything it found simply moves the context-window problem downstream.
MAX_CONTEXT_CHARS = int(os.environ.get("RAG_CONTEXT_CHARS", "4000"))
DEFAULT_TOP_K = store.DEFAULT_TOP_K


@dataclass
class RetrievedContext:
    """Retrieved passages, formatted for a prompt and for attribution."""
    text: str = ""
    sources: list = field(default_factory=list)
    hits: list = field(default_factory=list)
    truncated: bool = False

    @property
    def found(self):
        return bool(self.hits)

    def as_dict(self):
        return {"text": self.text, "sources": self.sources,
                "found": self.found, "truncated": self.truncated}


def retrieve(query, *, top_k=DEFAULT_TOP_K, min_score=store.DEFAULT_MIN_SCORE,
             **filters):
    """Ranked chunks for a query. Thin pass-through, kept so callers depend on
    this module rather than reaching into the store."""
    return store.search(query, top_k=top_k, min_score=min_score, **filters)


def format_sources(hits):
    """Numbered citations, aligned with the numbering in the context block."""
    return [
        {"n": index,
         "reference": hit.reference,
         "document": hit.document.title,
         "source": hit.document.source,
         "section": hit.chunk.section,
         "chunk_id": hit.chunk.id,
         "score": round(hit.score, 4)}
        for index, hit in enumerate(hits, start=1)
    ]


def format_context(hits, *, max_chars=MAX_CONTEXT_CHARS):
    """Render hits as a numbered block. Returns ``(text, used_hits, truncated)``.

    A passage is included whole or not at all: half a clinical statement is
    worse than its absence, because the model cannot tell it was cut.
    """
    parts, used, truncated, budget = [], [], False, max_chars
    for index, hit in enumerate(hits, start=1):
        block = f"[{index}] {hit.reference}\n{hit.chunk.text.strip()}"
        if len(block) > budget:
            truncated = True
            break
        parts.append(block)
        used.append(hit)
        budget -= len(block) + 2
    return "\n\n".join(parts), used, truncated


def retrieve_context(query, *, top_k=DEFAULT_TOP_K,
                     min_score=store.DEFAULT_MIN_SCORE,
                     max_chars=MAX_CONTEXT_CHARS, **filters):
    """Retrieve and format context for ``query``.

    Returns an empty :class:`RetrievedContext` when nothing matches — the caller
    decides what to do about it. It does **not** invent a fallback: a prompt
    told to answer only from sources, handed no sources, must be able to see
    that it has none.
    """
    hits = retrieve(query, top_k=top_k, min_score=min_score, **filters)
    if not hits:
        return RetrievedContext()

    text, used, truncated = format_context(hits, max_chars=max_chars)
    # Sources describe what was actually sent, not what was found, so a citation
    # can never point at a passage the model was never shown.
    return RetrievedContext(text=text, sources=format_sources(used),
                            hits=used, truncated=truncated)


__all__ = ["RetrievedContext", "IndexSpaceMismatch", "retrieve",
           "retrieve_context", "format_context", "format_sources",
           "MAX_CONTEXT_CHARS", "DEFAULT_TOP_K"]
