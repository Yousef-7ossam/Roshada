"""Stage 1 — query processing.

What this does: validates, normalises whitespace, and works out which retrieval
filters the question implies.

**What it deliberately does not do: rewrite the question.** A medical question
carries meaning in its exact wording — "is it safe to stop taking" and "is it
safe to take" differ by one word — so no paraphrase, no expansion, no
"helpful" reformulation, and no translation. The processed query is the user's
query with its whitespace tidied.

It also does not interpret the question clinically. Detecting that someone is
describing symptoms and turning that into a diagnosis-shaped search is exactly
the behaviour the brief rules out; the only thing inferred here is which
*language* the text is in, and that only to set a default filter the caller can
override.
"""
import re
import unicodedata
from dataclasses import dataclass, field

from . import config


class InvalidQuery(Exception):
    """The question cannot be processed as given."""


#: Arabic script, including the supplement and extended blocks. Used only to
#: pick a default language filter, never to translate.
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ]")
_WHITESPACE_RE = re.compile(r"\s+")

ENGLISH = "en"
ARABIC = "ar"


@dataclass
class ProcessedQuery:
    """A validated question plus the filters it will be retrieved with."""

    text: str
    #: The language the question was written in, as detected. Carried so the
    #: answer can be produced in the same language and so retrieval can prefer
    #: material the reader can actually read.
    language: str = ENGLISH
    #: Whether the language was detected or supplied by the caller. A detected
    #: language is a guess and is applied as a *preference*; a supplied one is
    #: an instruction.
    language_was_detected: bool = True
    filters: dict = field(default_factory=dict)

    def as_dict(self):
        return {"query": self.text, "language": self.language,
                "filters": dict(self.filters)}


def detect_language(text):
    """``ar`` when the text contains Arabic script, else ``en``.

    Deliberately crude. A full language identifier would be a dependency and a
    model to maintain, and the only decision resting on this is which language's
    material to prefer — which the caller can override and which never changes
    what the question means.
    """
    return ARABIC if _ARABIC_RE.search(text or "") else ENGLISH


def normalise(text):
    """Tidy whitespace and Unicode form. Nothing else.

    NFKC folds the compatibility forms that make the same word embed as two
    different vectors — the same normalisation the corpus itself was cleaned
    with, so a question and the passages it should match are treated alike.
    Casing, punctuation and word order are left exactly as written: medical
    terminology and negation both live there.
    """
    text = unicodedata.normalize("NFKC", text or "")
    return _WHITESPACE_RE.sub(" ", text).strip()


def process(query, *, language=None, specialty=None, topic=None, source=None,
            document=None, section=None):
    """Validate and prepare one question for retrieval.

    Filters given by the caller are passed through as they are. The only
    inferred filter is language, and only when the caller did not name one.
    """
    text = normalise(query)
    if not text:
        raise InvalidQuery("Ask a question.")
    if len(text) < config.MIN_QUERY_CHARS:
        raise InvalidQuery(
            f"That question is too short to search for "
            f"(minimum {config.MIN_QUERY_CHARS} characters).")
    if len(text) > config.MAX_QUERY_CHARS:
        raise InvalidQuery(
            f"That question is too long "
            f"(maximum {config.MAX_QUERY_CHARS} characters).")

    detected = detect_language(text)
    chosen = (language or "").strip() or detected

    filters = {}
    if language:
        # An explicit language is an instruction: restrict retrieval to it.
        filters["language"] = language.strip()
    if specialty:
        filters["specialty"] = specialty.strip()
    if source is not None:
        filters["source"] = source
    if document is not None:
        filters["document"] = document
    if section:
        filters["section"] = section.strip()
    if topic:
        # ``topic`` is a corpus-level facet carried in chunk metadata rather
        # than a column, so it filters through the metadata path.
        filters["metadata"] = {"topic": topic.strip()}

    return ProcessedQuery(text=text, language=chosen,
                          language_was_detected=not bool(language),
                          filters=filters)
