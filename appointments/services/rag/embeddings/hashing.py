"""Offline embedder: hashed bag-of-words.

**This is lexical, not semantic.** It matches passages that share words with the
query; it does not know that "hypertension" and "high blood pressure" are the
same thing. Anything relying on meaning needs :mod:`.api`.

It exists because the alternatives are worse for development and testing:

* a sentence-transformer would pull in torch — hundreds of megabytes, for a
  project whose image already carries TensorFlow;
* an API embedder makes the test suite depend on a key and a network round-trip,
  and bills for every run.

It is deterministic, needs no credentials, works offline, and is built on
scikit-learn's ``HashingVectorizer``, which the project already depends on, so
it costs nothing new. That makes the retrieval pipeline testable end
to end without a provider, which is exactly what this task has to demonstrate.
"""
import os
from functools import lru_cache

from .base import Embedder, unit_normalise

#: Width of the hashed space. Read per instance, not at import, so a test or a
#: deployment can change it and the new value actually takes effect.
FALLBACK_DIMENSION = 2048


def default_dimension():
    try:
        return int(os.environ.get("RAG_HASHING_DIMENSION", FALLBACK_DIMENSION))
    except ValueError:
        return FALLBACK_DIMENSION


@lru_cache(maxsize=4)
def _vectorizer(dimension):
    from sklearn.feature_extraction.text import HashingVectorizer

    return HashingVectorizer(
        n_features=dimension,
        # Signed hashing cancels colliding terms to zero, which is right for a
        # linear model and wrong here: it can drive a passage's similarity to
        # zero for a word it actually contains.
        alternate_sign=False,
        norm="l2",
        lowercase=True,
        # Two-plus word characters, Unicode-aware, so Arabic tokenises too.
        token_pattern=r"(?u)\b\w\w+\b",
        # Word bigrams as well as single words, so "blood pressure" is a feature
        # in its own right rather than two independent common words.
        ngram_range=(1, 2),
        # The single most important setting here, measured rather than assumed.
        # Without it, function words ("how", "is", "the") are features that every
        # document shares, so every query matched every passage: an unrelated
        # question scored the same 0.085 as a genuine one. With them removed, an
        # unrelated query scores exactly 0.0 and correctly returns nothing.
        #
        # Only English stopwords ship with scikit-learn. Arabic function words
        # are still features, so Arabic retrieval is noisier — one more reason
        # the API embedder is what production should use.
        stop_words="english",
    )


class HashingEmbedder(Embedder):
    """Deterministic offline embedder. Lexical matching only."""

    name = "hashing"
    #: Bumped whenever the vectorizer configuration changes, because that
    #: changes the space. Stamping it into the model id means an existing corpus
    #: is detected as stale and reindexed, rather than being queried with
    #: vectors that are silently incompatible.
    #: v3 — English stopwords removed from the feature space.
    revision = "v3"

    def __init__(self, dimension=None):
        self._dimension = int(dimension or default_dimension())

    @property
    def model(self):
        return f"hashing-{self.revision}-{self._dimension}"

    @property
    def dimension(self):
        return self._dimension

    def embed_documents(self, texts):
        texts = [t if isinstance(t, str) else "" for t in (texts or [])]
        if not texts:
            import numpy as np

            return np.zeros((0, self._dimension), dtype="float32")
        matrix = _vectorizer(self._dimension).transform(texts).toarray()
        return unit_normalise(matrix)
