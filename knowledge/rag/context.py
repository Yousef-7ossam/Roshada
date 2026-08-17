"""Stage 3 — context building.

Turns gated retrieval results into the numbered block a model is shown, and the
matching citation list a reader is shown. The numbering is shared, which is what
makes a "[2]" in an answer resolvable to a real document, section and source
*by the application* rather than on the model's word.

**Passages are included whole or not at all.** Half a clinical statement is
worse than its absence, because nothing downstream can tell it was cut — a
sentence ending "...unless the patient is pregnant" carries the opposite meaning
to the same sentence truncated before the comma.

The engine has a similar formatter (``appointments.services.rag.retrieval``).
This one exists because it builds from the *governed* results — approved source,
processed document, live version — and carries the full provenance trail that
gate produces. The engine's version is ungated and knows nothing about sources.
"""
import re
from dataclasses import dataclass, field

from . import config

#: Word tokens for duplicate detection. ``\w`` is Unicode-aware, so this splits
#: Arabic as readily as English.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


@dataclass
class BuiltContext:
    """What the model is shown, and what the reader is shown about it."""

    text: str = ""
    #: One entry per passage actually included, numbered from 1 to match the
    #: block above. Never what was *found* — only what was sent, so a citation
    #: can never point at a passage the model never saw.
    sources: list = field(default_factory=list)
    results: list = field(default_factory=list)
    #: True when relevant passages were dropped for space. Surfaced rather than
    #: hidden: an answer built from a truncated context is a weaker answer and
    #: the operator should be able to see that.
    truncated: bool = False
    dropped: int = 0
    #: Passages skipped as near-duplicates of one already included. Counted
    #: separately from ``dropped`` because they mean the opposite thing: nothing
    #: was lost, the same text was simply not sent twice.
    deduplicated: int = 0

    @property
    def found(self):
        return bool(self.results)

    @property
    def count(self):
        return len(self.results)

    def as_dict(self):
        return {"text": self.text, "sources": self.sources,
                "found": self.found, "truncated": self.truncated,
                "dropped": self.dropped, "deduplicated": self.deduplicated}


def _block(index, result):
    """One numbered source block, carrying its own provenance.

    Provenance travels *inside* the context, not only alongside it. A model
    asked to cite [2] can see what [2] actually is, which is the difference
    between a citation and a number.
    """
    provenance = result.get("provenance") or {}
    lines = [f"[{index}] {result.get('reference') or 'Source'}"]

    source_name = provenance.get("source_name")
    if source_name:
        lines.append(f"Source: {source_name}")
    title = provenance.get("document_title")
    if title:
        version = provenance.get("document_version")
        lines.append(f"Document: {title}"
                     + (f" (v{version})" if version else ""))
    if result.get("section"):
        lines.append(f"Section: {result['section']}")
    # Only when the source actually has one. The corpus is text and Markdown,
    # which have no pages, and a fabricated page number is a fabricated
    # citation.
    if result.get("page"):
        lines.append(f"Page: {result['page']}")
    if provenance.get("url"):
        lines.append(f"URL: {provenance['url']}")
    lines.append(f"Content: {(result.get('text') or '').strip()}")
    return "\n".join(lines)


def _words(text):
    """Comparable word set for one passage.

    Case-folded and punctuation-stripped so that "Hypertension is..." and
    "hypertension is..." are recognised as the same text. Deliberately
    language-agnostic: ``\\w`` covers Arabic script, and a tokeniser that only
    understood English would silently stop deduplicating half the corpus.
    """
    return set(_WORD_RE.findall((text or "").casefold()))


def _is_near_duplicate(candidate, kept, threshold):
    """Does ``candidate`` restate a passage already included?

    Compared only against passages from the **same document**: two sources
    saying the same thing is corroboration and both deserve a citation number,
    while one document's overlapping chunks are one statement counted twice.

    The measure is containment, not symmetric similarity — a short chunk fully
    inside a longer one is a duplicate even though the sets are far from equal.
    """
    if threshold >= 1.0:
        return False
    document = (candidate.get("provenance") or {}).get("document_id")
    if document is None:
        return False

    words = _words(candidate.get("text"))
    if not words:
        return False

    for existing in kept:
        if (existing.get("provenance") or {}).get("document_id") != document:
            continue
        other = _words(existing.get("text"))
        if not other:
            continue
        shared = len(words & other)
        # Against the smaller passage: that is what makes this containment.
        if shared / min(len(words), len(other)) >= threshold:
            return True
    return False


def _citation(index, result):
    provenance = result.get("provenance") or {}
    return {
        "n": index,
        "reference": result.get("reference", ""),
        # Identifiers a caller can resolve back to the record. Only what the
        # knowledge base actually holds — no invented URLs or page numbers.
        "document_id": provenance.get("document_id"),
        "source_id": provenance.get("source_id"),
        "document_type": provenance.get("document_type", ""),
        "source_name": provenance.get("source_name", ""),
        "source_url": provenance.get("source_url", ""),
        "document_title": provenance.get("document_title", ""),
        "document_version": provenance.get("document_version"),
        "section": result.get("section", ""),
        "page": result.get("page"),
        "url": provenance.get("url", ""),
        "score": result.get("score"),
        # Filled in after generation: whether the answer actually cited this.
        "cited": False,
    }


def build(results, *, max_chars=None, max_chunks=None, dedupe_overlap=None):
    """Assemble the context block from gated retrieval results.

    Results arrive best-first, so the budget is spent on the most relevant
    passages — selection by relevance rather than by truncation, which is the
    difference between dropping the least useful source and cutting the most
    useful one in half.

    Near-duplicates are skipped as they are met. Because the list is ordered by
    score, the passage that survives is always the better-matching one; there is
    no separate "pick the winner" step to get wrong.
    """
    max_chars = config.MAX_CONTEXT_CHARS if max_chars is None else max_chars
    max_chunks = config.MAX_CONTEXT_CHUNKS if max_chunks is None else max_chunks
    if dedupe_overlap is None:
        dedupe_overlap = config.DEDUPE_OVERLAP

    if not results:
        return BuiltContext()

    blocks, used, budget = [], [], max_chars
    truncated = False
    duplicates = 0
    for result in results:
        if len(used) >= max_chunks:
            truncated = True
            break
        if _is_near_duplicate(result, used, dedupe_overlap):
            # Not a loss: the same statement is already in the context, held by
            # the higher-scoring chunk. Freeing its budget lets a genuinely
            # different passage take the slot.
            duplicates += 1
            continue
        block = _block(len(used) + 1, result)
        if len(block) > budget:
            # Whole or not at all. Keep going: a later passage may still fit,
            # and dropping one long source should not discard the rest.
            truncated = True
            continue
        blocks.append(block)
        used.append(result)
        budget -= len(block) + 2

    return BuiltContext(
        text="\n\n".join(blocks),
        sources=[_citation(index, result)
                 for index, result in enumerate(used, start=1)],
        results=used,
        truncated=truncated,
        # Duplicates were not "dropped" — nothing they carried is missing.
        dropped=len(results) - len(used) - duplicates,
        deduplicated=duplicates,
    )
