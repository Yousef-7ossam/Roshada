"""RAG: grounded answering over the governed medical knowledge base.

    question -> query processing -> gated retrieval -> context -> LLM
             -> validation -> citation verification -> grounded answer

**Two packages are called "rag" and they are different layers.**
``appointments.services.rag`` is the *engine*: parsing, chunking, embeddings and
the vector store. This package is the *pipeline* that answers a question using
that engine's results, after the Knowledge Base's approval gate has decided
which of them may be used. The same relation ``radiology`` has to the scheduling
engine, at the same boundary.

What this package is not:

* not an agent — it calls no tools and takes no actions,
* not a copilot — it has no conversation, no memory and no patient context,
* not personalised — it reads general medical reference material only, and
  never a patient's record.

:func:`answer` is the whole public surface.
"""
from .config import describe as describe_config
from .context import BuiltContext, build as build_context
from .evaluation import RAGTrace, query_fingerprint
from .query import InvalidQuery, ProcessedQuery, process as process_query
from .service import (
    INFORMATION_NOTICE, NO_CONTEXT_REPLY, RAGAnswer, answer, cited_numbers,
    describe, verify_citations,
)

__all__ = [
    # the pipeline
    "answer", "RAGAnswer",
    # stages, exposed so each is testable on its own
    "process_query", "ProcessedQuery", "InvalidQuery",
    "build_context", "BuiltContext",
    "cited_numbers", "verify_citations",
    # evaluation and diagnostics
    "RAGTrace", "query_fingerprint", "describe", "describe_config",
    # copy the API and the UI both need
    "NO_CONTEXT_REPLY", "INFORMATION_NOTICE",
]
