"""The embedder interface and the vector-space identity that goes with it.

An embedding only means something relative to the model that produced it. A
1024-dimension hashing vector and a 1536-dimension API vector are both just
arrays of floats: nothing stops you computing a cosine between them, and the
number that comes back is meaningless. Worse, when the dimensions happen to
match, the failure is silent — retrieval returns confident, wrong passages.

So every embedding is stamped with its :class:`EmbeddingSpace`, and search only
ever compares vectors within one space.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np


class EmbeddingError(Exception):
    """Embedding failed. Raised at ingestion or query time, never swallowed."""


class EmbedderNotConfigured(EmbeddingError):
    """The selected embedder has no usable credentials."""


@dataclass(frozen=True)
class EmbeddingSpace:
    """Identity of a vector space: which embedder, which model, how wide."""
    embedder: str
    model: str
    dimension: int

    def as_dict(self):
        return {"embedder": self.embedder, "model": self.model,
                "dimension": self.dimension}

    def __str__(self):
        return f"{self.embedder}:{self.model}[{self.dimension}]"


def unit_normalise(matrix):
    """Scale rows to unit length so cosine similarity is a plain dot product.

    Applied defensively at write *and* query time: both shipped embedders
    already return normalised vectors, but a future one might not, and the
    resulting scores would be silently unbounded rather than in [-1, 1].
    """
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.ndim == 1:
        matrix = matrix.reshape(1, -1)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # A zero vector (empty or all-stopword text) stays zero rather than becoming
    # NaN, so it simply never matches anything.
    norms[norms == 0] = 1.0
    return matrix / norms


class Embedder(ABC):
    """Turns text into vectors in one, self-identifying space."""

    name = "abstract"

    @property
    @abstractmethod
    def model(self) -> str:
        """The specific model id, recorded against every chunk."""

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Vector width."""

    @property
    def space(self) -> EmbeddingSpace:
        return EmbeddingSpace(self.name, self.model, self.dimension)

    @abstractmethod
    def embed_documents(self, texts) -> np.ndarray:
        """Embed a batch. Returns ``(len(texts), dimension)``, unit-normalised."""

    def embed_query(self, text) -> np.ndarray:
        """Embed one query. Returns ``(dimension,)``, unit-normalised.

        Separate from :meth:`embed_documents` because some providers ask for a
        different prefix or task type for queries; the default treats them the
        same.
        """
        return self.embed_documents([text])[0]

    @classmethod
    def is_configured(cls) -> bool:
        return True

    def describe(self):
        return {**self.space.as_dict(), "configured": self.is_configured()}
