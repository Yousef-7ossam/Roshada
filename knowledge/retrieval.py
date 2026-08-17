"""The gated retrieval foundation.

This is the seam a future RAG layer consumes, and the only place that decides
what the Knowledge Base is allowed to return. Three conditions, applied in the
database before anything is scored:

* the document's **source is APPROVED** — pending, rejected and archived
  sources are excluded, and so is material with no source at all
* the document is **PROCESSED** — never a failed or half-processed one
* the document is the **active version** — superseded guidance cannot answer

``rag.store.search`` deliberately stays ungated: it is the engine, and an
engine that enforced one application's policy could not serve another. This
module is the policy. The same relationship a Django manager has to a service
that scopes it.

**This is not RAG.** Nothing here calls a language model, builds a prompt or
composes an answer. It returns passages and their provenance.
"""
import logging

from appointments.models import Document
from appointments.services import rag

from .models import KnowledgeSource

logger = logging.getLogger("appointments")

DEFAULT_TOP_K = 5

#: The gate, in one place. Any caller that wants "what may be shown" gets
#: exactly this; there is no second definition to drift from it.
SAFE_FILTERS = {
    "source_status": KnowledgeSource.RETRIEVABLE_STATUSES,
    "document_status": Document.RETRIEVABLE_STATUSES,
    "active_only": True,
}


class RetrievalUnavailable(Exception):
    """The corpus cannot be searched right now — not "nothing matched".

    Distinguished because the two mean opposite things to a caller: one is an
    answer, the other is a broken index that needs re-building.
    """


def search(query, *, top_k=DEFAULT_TOP_K, min_score=None, language=None,
           specialty=None, source=None, document=None, section=None,
           metadata=None):
    """Retrieve passages that are allowed to be shown.

    Returns a list of dicts carrying the passage, its score and the full
    provenance trail — chunk → document → source. A caller never has to look
    anything up to cite what it was given.
    """
    query = (query or "").strip()
    if not query:
        return []

    filters = dict(SAFE_FILTERS)
    if language:
        filters["language"] = language
    if specialty:
        filters["specialty"] = specialty
    if source is not None:
        filters["knowledge_sources"] = [source]
    if document is not None:
        filters["documents"] = [document]
    if section:
        filters["section"] = section
    if metadata:
        filters["metadata"] = metadata

    kwargs = {"top_k": top_k, **filters}
    if min_score is not None:
        kwargs["min_score"] = min_score

    try:
        hits = rag.search(query, **kwargs)
    except rag.IndexSpaceMismatch as exc:
        # Honest failure: the corpus exists but was built with a different
        # embedder, so it is unreadable until re-indexed. Reporting "no
        # results" here would be a lie with the same shape as an answer.
        raise RetrievalUnavailable(str(exc)) from None
    except rag.EmbeddingError as exc:
        raise RetrievalUnavailable(
            f"The embedding service is unavailable: {exc}") from None

    return [_as_result(hit) for hit in hits]


def _as_result(hit):
    """One passage, with everything needed to cite it."""
    chunk = hit.chunk
    document = chunk.document
    return {
        "text": chunk.text,
        "score": round(hit.score, 4),
        "section": chunk.section,
        "chunk_index": chunk.ordinal,
        # No page number: the corpus is text and Markdown, which have no pages.
        # Reporting a fabricated one would be worse than reporting none.
        "page": chunk.metadata.get("page"),
        "reference": _citation(document, chunk),
        "provenance": document.provenance,
        "metadata": chunk.metadata,
    }


def _citation(document, chunk):
    parts = []
    source = document.knowledge_source
    if source is not None:
        parts.append(source.citation)
    parts.append(f"{document.title} (v{document.version})")
    if chunk.section:
        parts.append(chunk.section)
    return " › ".join(parts)


def retrievable_documents():
    """Exactly the documents ``search`` can draw on. Used by diagnostics."""
    return (Document.objects
            .filter(is_active=True, status=Document.PROCESSED,
                    knowledge_source__verification_status__in=(
                        KnowledgeSource.RETRIEVABLE_STATUSES))
            .select_related("knowledge_source"))


def coverage():
    """What the corpus can currently answer from, for the admin index view."""
    documents = retrievable_documents()
    return {
        "documents": documents.count(),
        "sources": documents.values("knowledge_source").distinct().count(),
        # values_list rather than .only(): deferring a column that
        # select_related traverses is a FieldError, and the language strings
        # are all this needs.
        "languages": sorted({language for language
                             in documents.values_list("language", flat=True)
                             if language}),
    }
