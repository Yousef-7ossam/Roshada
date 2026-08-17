"""Every RAG tuning knob, in one place, read from the environment.

Nothing in this package hardcodes a value that an operator might reasonably
want to change, and nothing outside this module reads the environment for one.
That is what makes "adjust retrieval breadth" a deployment change rather than a
code change.

Credentials are deliberately absent: the LLM provider and the embedding model
already own theirs (``appointments.services.ai.providers`` and
``appointments.services.rag.embeddings``), and a second place to configure a key
is a second place to leak one.
"""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return int(default)


def _float(name, default):
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return float(default)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
#: How many passages to retrieve before context assembly.
TOP_K = _int("RAG_TOP_K", 5)
#: Below this cosine score a passage is noise. Retrieval that returns its best
#: guess regardless of quality is how a grounded system starts answering from
#: irrelevant sources.
MIN_SCORE = _float("RAG_MIN_SCORE", 0.05)
#: Above this, a single hit is a strong match rather than a weak one. Used only
#: to label *retrieval* strength — never medical certainty.
STRONG_SCORE = _float("RAG_STRONG_SCORE", 0.35)

# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------
#: Character budget for the assembled context block.
MAX_CONTEXT_CHARS = _int("RAG_CONTEXT_CHARS", 4000)
#: Hard ceiling on passages sent, independent of the character budget: ten
#: short passages are still ten things for the model to conflate.
MAX_CONTEXT_CHUNKS = _int("RAG_CONTEXT_CHUNKS", 6)
#: Word-overlap above which two passages from the *same document* are treated as
#: the same passage. Chunking overlaps deliberately (``RAG_CHUNK_OVERLAP``), so
#: adjacent chunks share text by design; sending both spends the budget twice on
#: one statement and gives the model two numbers for one fact to cite.
#: 1.0 disables deduplication.
DEDUPE_OVERLAP = _float("RAG_DEDUPE_OVERLAP", 0.8)

# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
#: Low by default. A grounded medical answer should restate its sources, not
#: improvise around them, and temperature is the dial that decides which.
TEMPERATURE = _float("RAG_TEMPERATURE", 0.1)
#: Seconds. Falls back to the LLM service's own default when unset, so this
#: does not quietly become a second source of truth for provider timeouts.
TIMEOUT = _int("RAG_LLM_TIMEOUT", 0) or None
#: Longest question accepted. Not a safety control — a bound, so a pathological
#: input cannot become a pathological embedding call.
MAX_QUERY_CHARS = _int("RAG_MAX_QUERY_CHARS", 1000)
MIN_QUERY_CHARS = _int("RAG_MIN_QUERY_CHARS", 3)


def describe():
    """The active configuration, for diagnostics. Contains no credential."""
    return {
        "top_k": TOP_K,
        "min_score": MIN_SCORE,
        "strong_score": STRONG_SCORE,
        "max_context_chars": MAX_CONTEXT_CHARS,
        "max_context_chunks": MAX_CONTEXT_CHUNKS,
        "dedupe_overlap": DEDUPE_OVERLAP,
        "temperature": TEMPERATURE,
        "timeout": TIMEOUT,
        "max_query_chars": MAX_QUERY_CHARS,
    }
