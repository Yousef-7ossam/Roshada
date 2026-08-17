"""The vector store.

**The application database is the vector database.** Chunks and their embeddings
live in ``DocumentChunk`` alongside every other Roshada table; similarity is a
dot product over the candidate set in numpy.

Why this rather than pgvector, Chroma or FAISS, for this application:

* **Metadata filtering is free and expressive.** Filters are ORM lookups, so
  they compose with every relationship already modelled. A separate vector
  service would mean duplicating that metadata into a second system and keeping
  the two in step.
* **One store, one backup, one access-control story.** A medical corpus in a
  second database is a second thing to secure and restore.
* **No new dependency or service.** numpy is already required.

The cost is that scoring is exhaustive over the *filtered* candidate set. At
this corpus's scale — reference material, thousands of chunks — that is a
sub-millisecond dot product. Brute force stops being the right answer somewhere
around **50k chunks in a single unfiltered space**, or when p99 latency here
becomes measurable against the LLM call it precedes.

.. note::
   This module originally carried a fourth reason — that it worked identically
   on SQLite and PostgreSQL. Since the migration to PostgreSQL-only that reason
   no longer applies, and ``pgvector`` became a genuinely available option
   rather than one ruled out by the development engine. The remaining three
   reasons still hold at this corpus size, so the implementation is unchanged;
   what changed is that adopting pgvector is now a real choice to make on
   measured latency rather than a blocked one.

   When that day comes this module is the only thing that changes: add the
   extension and an ``ivfflat``/``hnsw`` index, then swap :func:`search` for
   ``ORDER BY embedding <=> %s LIMIT k``, keeping the same pre-filter queryset
   and the same :class:`SearchHit`. The stored ``bytea`` converts to a
   ``vector`` column in one additive migration. Nothing above this layer knows
   how similarity is computed.
"""
import logging

import numpy as np
from django.db import transaction

from ...models import Document, DocumentChunk
from .embeddings import EmbeddingSpace, resolve, unit_normalise

logger = logging.getLogger("appointments")

#: float32 keeps the corpus compact (1024 dims = 4 KB/chunk) and costs nothing
#: in retrieval quality — the ranking is unchanged at this precision.
DTYPE = np.float32

DEFAULT_TOP_K = 5
#: Below this, a "match" is noise. Cosine over unit vectors is in [-1, 1]; with
#: lexical embeddings an unrelated passage typically scores well under 0.1.
DEFAULT_MIN_SCORE = 0.05


class IndexSpaceMismatch(Exception):
    """The corpus was built with a different embedder than the one now active.

    Raised instead of returning no results, because the two look identical from
    the caller's side and mean completely different things: "we have nothing on
    that" versus "everything we have is unreadable until you reindex".
    """


class SearchHit:
    """One retrieved chunk with its score and citation."""

    __slots__ = ("chunk", "score")

    def __init__(self, chunk, score):
        self.chunk = chunk
        self.score = float(score)

    @property
    def document(self):
        return self.chunk.document

    @property
    def reference(self):
        """Human-readable citation, e.g. ``Hypertension Guide › Treatment``."""
        parts = [self.document.title]
        if self.chunk.section:
            parts.append(self.chunk.section)
        return " › ".join(parts)

    def as_dict(self):
        return {
            "chunk_id": self.chunk.id,
            "document_id": self.document_id,
            "document": self.document.title,
            "source": self.document.source,
            "section": self.chunk.section,
            "ordinal": self.chunk.ordinal,
            "reference": self.reference,
            "score": round(self.score, 4),
            "text": self.chunk.text,
            "metadata": self.chunk.metadata,
        }

    @property
    def document_id(self):
        return self.chunk.document_id

    def __repr__(self):
        return f"<SearchHit {self.reference} score={self.score:.3f}>"


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def to_bytes(vector):
    return np.asarray(vector, dtype=DTYPE).tobytes()


def from_bytes(raw):
    # psycopg2 returns a ``bytea`` column as ``memoryview``; ``bytes()`` copies
    # it into something ``frombuffer`` accepts. Verified against PostgreSQL 17.
    return np.frombuffer(bytes(raw), dtype=DTYPE)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
@transaction.atomic
def replace_chunks(document, chunks, vectors, space: EmbeddingSpace):
    """Write a document's chunks for one embedding space, replacing any prior set.

    Replace rather than append: re-ingesting an edited document would otherwise
    leave the removed passages retrievable forever, and stale medical guidance
    that still answers queries is worse than none.
    """
    if len(chunks) != len(vectors):
        raise ValueError(
            f"{len(chunks)} chunks but {len(vectors)} vectors — refusing to "
            f"write a misaligned index.")

    vectors = unit_normalise(vectors) if len(vectors) else vectors

    DocumentChunk.objects.filter(
        document=document, embedder=space.embedder,
        embedding_model=space.model).delete()

    rows = [
        DocumentChunk(
            document=document, ordinal=chunk.ordinal, text=chunk.text,
            section=chunk.section, metadata=chunk.metadata or {},
            embedder=space.embedder, embedding_model=space.model,
            dimension=space.dimension, embedding=to_bytes(vectors[index]),
        )
        for index, chunk in enumerate(chunks)
    ]
    DocumentChunk.objects.bulk_create(rows, batch_size=200)
    return rows


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def candidates(space: EmbeddingSpace, *, documents=None, source_type=None,
               section=None, metadata=None, document_metadata=None,
               document_status=None, source_status=None, active_only=False,
               language=None, specialty=None, knowledge_sources=None):
    """The queryset searched, after metadata filtering.

    Filtering happens **before** scoring, in the database. That is the real
    reason to keep the corpus in the ORM: "only patient-facing Arabic material
    from these two documents" shrinks the candidate set in SQL, and numpy then
    scores what little is left. Scoring everything and filtering afterwards
    would both be slower and quietly return fewer than ``k`` results.

    The governance filters — ``document_status``, ``source_status``,
    ``active_only`` — exist so the Knowledge Base can express "approved sources,
    processed documents, live versions only" as part of the same pre-filter
    rather than as a pass over the results. This function stays ungated by
    default, the same way a Django manager does: the *service* that wraps it
    decides policy, and ``knowledge.retrieval`` is the one that does.
    """
    queryset = DocumentChunk.objects.filter(
        embedder=space.embedder, embedding_model=space.model,
        dimension=space.dimension,
    ).select_related("document", "document__knowledge_source")

    if documents:
        ids = [getattr(d, "pk", d) for d in documents]
        queryset = queryset.filter(document_id__in=ids)
    if knowledge_sources:
        ids = [getattr(s, "pk", s) for s in knowledge_sources]
        queryset = queryset.filter(document__knowledge_source_id__in=ids)
    if source_type:
        queryset = queryset.filter(document__source_type=source_type)
    if section:
        queryset = queryset.filter(section__icontains=section)
    if document_status:
        statuses = ([document_status] if isinstance(document_status, str)
                    else list(document_status))
        queryset = queryset.filter(document__status__in=statuses)
    if source_status:
        statuses = ([source_status] if isinstance(source_status, str)
                    else list(source_status))
        # A document with no source at all is excluded too: unattributed
        # material has no verification status to trust.
        queryset = queryset.filter(
            document__knowledge_source__verification_status__in=statuses)
    if active_only:
        queryset = queryset.filter(document__is_active=True)
    if language:
        queryset = queryset.filter(document__language=language)
    if specialty:
        queryset = queryset.filter(
            document__knowledge_source__specialty__iexact=specialty)
    for key, value in (metadata or {}).items():
        queryset = queryset.filter(**{f"metadata__{key}": value})
    for key, value in (document_metadata or {}).items():
        queryset = queryset.filter(**{f"document__metadata__{key}": value})
    return queryset


def _assert_space_is_usable(space):
    """Distinguish "nothing indexed" from "indexed in another space"."""
    if not DocumentChunk.objects.exists():
        return
    if DocumentChunk.objects.filter(
            embedder=space.embedder, embedding_model=space.model,
            dimension=space.dimension).exists():
        return

    # order_by() clears Meta.ordering: without it Django adds the ordering
    # columns to the SELECT, so DISTINCT dedupes on those too and the message
    # lists the same space once per chunk.
    present = sorted({
        f"{e}:{m}[{d}]" for e, m, d in DocumentChunk.objects
        .order_by()
        .values_list("embedder", "embedding_model", "dimension").distinct()})
    raise IndexSpaceMismatch(
        f"The corpus is indexed as {', '.join(present)} but the active "
        f"embedder is {space}. Re-ingest the corpus with the current embedder, "
        f"or set RAG_EMBEDDER back to what built it.")


def search(query, *, top_k=DEFAULT_TOP_K, min_score=DEFAULT_MIN_SCORE,
           embedder=None, **filters):
    """Semantic search. Returns :class:`SearchHit` objects, best first.

    Independent of the LLM: nothing here calls a chat provider, so retrieval is
    testable — and debuggable — on its own.
    """
    query = (query or "").strip()
    if not query:
        return []

    embedder = embedder or resolve()
    space = embedder.space
    _assert_space_is_usable(space)

    queryset = candidates(space, **filters)
    # Two columns only: the vectors can be tens of thousands of rows and there
    # is no reason to carry every chunk's text through scoring.
    rows = list(queryset.values_list("id", "embedding"))
    if not rows:
        return []

    ids = [row[0] for row in rows]
    matrix = np.vstack([from_bytes(row[1]) for row in rows])
    if matrix.shape[1] != space.dimension:
        raise IndexSpaceMismatch(
            f"Stored vectors are {matrix.shape[1]}-dimension but the active "
            f"embedder produces {space.dimension}. Re-ingest the corpus.")

    # Both sides unit-normalised, so the dot product *is* cosine similarity.
    query_vector = unit_normalise(embedder.embed_query(query))[0]
    scores = matrix @ query_vector

    top_k = max(int(top_k), 1)
    # argpartition finds the top k without sorting the whole array — the part
    # that matters if this ever runs over a large unfiltered corpus.
    if len(scores) > top_k:
        top = np.argpartition(-scores, top_k - 1)[:top_k]
    else:
        top = np.arange(len(scores))
    top = top[np.argsort(-scores[top])]

    chosen = [(ids[i], float(scores[i])) for i in top if scores[i] >= min_score]
    if not chosen:
        return []

    chunks = DocumentChunk.objects.select_related("document").in_bulk(
        [chunk_id for chunk_id, _ in chosen])
    return [SearchHit(chunks[chunk_id], score) for chunk_id, score in chosen
            if chunk_id in chunks]


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
def stats():
    """What is indexed, and in which spaces."""
    spaces = (DocumentChunk.objects
              .order_by()          # drop Meta.ordering so DISTINCT actually dedupes
              .values("embedder", "embedding_model", "dimension")
              .distinct().order_by("embedder", "embedding_model"))
    return {
        "documents": Document.objects.count(),
        "chunks": DocumentChunk.objects.count(),
        "spaces": [
            {**space,
             "chunks": DocumentChunk.objects.filter(
                 embedder=space["embedder"],
                 embedding_model=space["embedding_model"]).count()}
            for space in spaces
        ],
    }
