"""Semantic embedder over the OpenAI-compatible ``/embeddings`` endpoint.

Works with OpenAI itself, TokenRouter, and any local server that speaks the same
route (llama.cpp, LM Studio, Ollama's compatibility layer) — the base URL is
configuration, so a self-hosted embedding model needs no code.

Reuses the provider HTTP layer, so retries, timeout handling and the error
taxonomy behave identically to chat completions rather than being
reimplemented with subtly different semantics.
"""
import os

from ...ai.providers import http, registry_utils
from ...ai.providers.base import ProviderError
from .base import Embedder, EmbedderNotConfigured, EmbeddingError, unit_normalise

DEFAULT_MODEL = "text-embedding-3-small"
#: Dimension of the configured model. Declared rather than probed: probing costs
#: a network round-trip on every startup, and a wrong value must fail loudly at
#: ingestion rather than silently produce a corpus that cannot be queried.
MODEL_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}
#: Providers reject very large batches; also bounds memory during ingestion.
BATCH_SIZE = int(os.environ.get("RAG_EMBEDDING_BATCH", "64"))


def _base_url():
    return os.environ.get(
        "RAG_EMBEDDING_BASE_URL",
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    ).rstrip("/")


def _api_key():
    return (registry_utils.env_key("RAG_EMBEDDING_API_KEY")
            or registry_utils.env_key("OPENAI_API_KEY"))


class APIEmbedder(Embedder):
    """Semantic embeddings from an OpenAI-compatible endpoint."""

    name = "api"

    def __init__(self, model=None, dimension=None):
        self._model = model or os.environ.get("RAG_EMBEDDING_MODEL", DEFAULT_MODEL)
        declared = os.environ.get("RAG_EMBEDDING_DIMENSION")
        self._dimension = int(dimension or declared
                              or MODEL_DIMENSIONS.get(self._model, 1536))

    @property
    def model(self):
        return self._model

    @property
    def dimension(self):
        return self._dimension

    @classmethod
    def is_configured(cls):
        return bool(_api_key())

    def embed_documents(self, texts):
        import numpy as np

        texts = [t if isinstance(t, str) and t.strip() else " "
                 for t in (texts or [])]
        if not texts:
            return np.zeros((0, self._dimension), dtype="float32")

        key = _api_key()
        if not key:
            raise EmbedderNotConfigured(
                "No embedding credential. Set RAG_EMBEDDING_API_KEY (or "
                "OPENAI_API_KEY), or set RAG_EMBEDDER=hashing for offline use.")

        vectors = []
        for start in range(0, len(texts), BATCH_SIZE):
            vectors.extend(self._embed_batch(texts[start:start + BATCH_SIZE], key))

        matrix = np.asarray(vectors, dtype="float32")
        if matrix.shape[1] != self._dimension:
            # Declared width and actual width disagree: every vector written
            # from here would be unqueryable. Fail at ingestion, loudly.
            raise EmbeddingError(
                f"Model '{self._model}' returned {matrix.shape[1]}-dimension "
                f"vectors but is configured as {self._dimension}. Set "
                f"RAG_EMBEDDING_DIMENSION={matrix.shape[1]} and reindex.")
        return unit_normalise(matrix)

    def _embed_batch(self, batch, key):
        try:
            body = http.post_json(
                f"{_base_url()}/embeddings",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                payload={"model": self._model, "input": batch},
                timeout=http.timeout_for(env_var="RAG_EMBEDDING_TIMEOUT"),
                provider="embeddings", model=self._model,
            )
        except ProviderError as exc:
            # Re-raised in this package's vocabulary so callers handle one
            # family of errors, with the provider detail preserved for the log.
            raise EmbeddingError(str(exc)) from exc

        data = body.get("data")
        if not isinstance(data, list) or len(data) != len(batch):
            raise EmbeddingError(
                f"Embedding endpoint returned {len(data or [])} vectors for "
                f"{len(batch)} inputs.")
        # Order is not guaranteed by the spec; sort by the returned index.
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        return [item["embedding"] for item in ordered]
