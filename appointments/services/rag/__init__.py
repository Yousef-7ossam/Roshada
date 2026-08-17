"""Retrieval-augmented generation infrastructure.

    documents -> parsing -> cleaning -> chunking -> metadata
              -> embeddings -> vector store -> semantic search
              -> context retrieval -> (LLM)

Retrieval is complete and testable on its own, with no language model in the
loop. Grounding the assistant in a corpus is :mod:`knowledge.rag`, which
consumes this engine through the governed gate in :mod:`knowledge.retrieval`.

**The vector database is the application database.** ``DocumentChunk`` holds the
embeddings, metadata filtering is an ORM query, and similarity is a numpy dot
product over the filtered candidates. :mod:`.store` documents that choice, its
limits, and the single place to change if it stops holding.
"""
from .embeddings import EmbeddingError, EmbeddingSpace, describe as embedder_info
from .embeddings import resolve as resolve_embedder
from .ingest import (
    IngestResult, ingest_directory, ingest_file, ingest_text, remove,
)
from .parsing import UnsupportedDocument
from .retrieval import (
    RetrievedContext, format_context, format_sources, retrieve, retrieve_context,
)
from .store import IndexSpaceMismatch, SearchHit, search, stats

__all__ = [
    # ingestion
    "ingest_text", "ingest_file", "ingest_directory", "remove", "IngestResult",
    # retrieval
    "search", "retrieve", "retrieve_context", "format_context", "format_sources",
    "RetrievedContext", "SearchHit",
    # configuration / diagnostics
    "resolve_embedder", "embedder_info", "EmbeddingSpace", "stats",
    # errors
    "EmbeddingError", "IndexSpaceMismatch", "UnsupportedDocument",
]
