"""Embedder selection, mirroring the LLM provider registry.

``RAG_EMBEDDER`` picks one explicitly (``api`` | ``hashing``). Left unset, the
API embedder is used when it has a real credential, and the offline hashing
embedder otherwise — so a developer with no keys gets a working corpus, and a
deployment with keys gets semantic retrieval, from the same code.

The resolved embedder is cached per environment signature rather than per
process: tests change ``RAG_EMBEDDER`` between cases, and a plain ``lru_cache``
would hand the second test the first test's embedder.
"""
import logging
import os

from .api import APIEmbedder
from .base import (
    Embedder, EmbedderNotConfigured, EmbeddingError, EmbeddingSpace,
    unit_normalise,
)
from .hashing import HashingEmbedder

logger = logging.getLogger("appointments")

EMBEDDERS = {
    HashingEmbedder.name: HashingEmbedder,
    APIEmbedder.name: APIEmbedder,
}
#: Tried in order when ``RAG_EMBEDDER`` is unset. Semantic first, then the
#: offline fallback — which is always configured, so resolution cannot fail.
AUTO_ORDER = (APIEmbedder.name, HashingEmbedder.name)

ALIASES = {
    "openai": APIEmbedder.name,
    "local": APIEmbedder.name,      # a self-hosted OpenAI-compatible server
    "offline": HashingEmbedder.name,
    "test": HashingEmbedder.name,
}

_cache = {}


def _signature():
    """Everything that can change which embedder or space is selected."""
    return tuple(os.environ.get(name, "") for name in (
        "RAG_EMBEDDER", "RAG_EMBEDDING_MODEL", "RAG_EMBEDDING_DIMENSION",
        "RAG_EMBEDDING_API_KEY", "OPENAI_API_KEY", "RAG_HASHING_DIMENSION",
    ))


def canonical(name):
    name = (name or "").strip().lower()
    return ALIASES.get(name, name)


def selected_name():
    """Which embedder will be used, and why it is available."""
    requested = canonical(os.environ.get("RAG_EMBEDDER", "auto"))

    if requested in EMBEDDERS:
        if not EMBEDDERS[requested].is_configured():
            raise EmbedderNotConfigured(
                f"RAG_EMBEDDER={requested} is selected but has no usable "
                f"credential. Unset it to fall back to offline embeddings.")
        return requested

    if requested not in ("", "auto"):
        raise EmbeddingError(
            f"Unknown RAG_EMBEDDER '{requested}'. "
            f"Known: {', '.join(sorted(EMBEDDERS))}, auto.")

    for name in AUTO_ORDER:
        if EMBEDDERS[name].is_configured():
            return name
    return HashingEmbedder.name


def resolve() -> Embedder:
    """The active embedder (cached per environment signature)."""
    signature = _signature()
    if signature not in _cache:
        name = selected_name()
        embedder = EMBEDDERS[name]()
        logger.info("RAG embedder: %s", embedder.space)
        _cache.clear()          # only ever one live configuration
        _cache[signature] = embedder
    return _cache[signature]


def reset():
    """Drop the cached embedder. For tests and after changing configuration."""
    _cache.clear()


def describe():
    """Current embedding configuration, for diagnostics."""
    embedder = resolve()
    return {
        **embedder.space.as_dict(),
        "semantic": embedder.name != HashingEmbedder.name,
        "available": sorted(name for name, cls in EMBEDDERS.items()
                            if cls.is_configured()),
    }


__all__ = [
    "Embedder", "EmbeddingSpace", "EmbeddingError", "EmbedderNotConfigured",
    "HashingEmbedder", "APIEmbedder", "EMBEDDERS", "ALIASES",
    "canonical", "selected_name", "resolve", "reset", "describe",
    "unit_normalise",
]
