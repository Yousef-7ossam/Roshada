"""Stage 3 — chunking: split a document into retrievable passages.

Chunk boundaries decide what retrieval can return. Two failure modes matter:

* **Too large** — the passage is mostly irrelevant to the query, the useful
  sentence is diluted in the embedding, and the model is handed padding.
* **Split mid-thought** — the sentence carrying the answer is severed from the
  condition it applies to. In a medical corpus that is the difference between
  "do not take this" and "do not take this *if pregnant*".

So splitting is hierarchical: paragraphs first, sentences only if a paragraph is
oversized, and characters only as a last resort for prose with no breaks at all
(unbroken Arabic paragraphs hit this). Consecutive small paragraphs are packed
together rather than each becoming a starved chunk.

Every chunk carries the heading path it sits under. That is both a retrieval
signal and the citation the reader sees.
"""
import os
import re
from dataclasses import dataclass, field

from .parsing import heading_of

#: Target chunk size in characters. Characters rather than tokens deliberately:
#: a token count is embedder-specific, and the ratio differs by ~2x between
#: English and Arabic, so a token budget would silently halve Arabic chunks.
DEFAULT_CHUNK_SIZE = int(os.environ.get("RAG_CHUNK_SIZE", "900"))
#: Overlap carried between neighbouring chunks so a fact spanning a boundary
#: survives in at least one of them.
DEFAULT_OVERLAP = int(os.environ.get("RAG_CHUNK_OVERLAP", "150"))
#: Below this a chunk is a fragment — a stray heading or a one-line caption.
#: It is merged into its neighbour rather than stored and retrieved on its own.
MIN_CHUNK_CHARS = 60

#: Sentence-ish boundaries, including the Arabic full stop and question mark.
_SENTENCE_RE = re.compile(r"(?<=[.!?۔؟])\s+|\n")


@dataclass
class Chunk:
    text: str
    ordinal: int = 0
    section: str = ""
    metadata: dict = field(default_factory=dict)


def _section_path(stack):
    return " > ".join(part for _, part in stack)


def _split_sections(text, *, is_markdown):
    """Yield ``(section_path, body)`` for each heading-delimited region."""
    if not is_markdown:
        return [("", text)]

    lines = text.splitlines()
    sections, stack, buffer = [], [], []

    def flush():
        body = "\n".join(buffer).strip()
        if body:
            sections.append((_section_path(stack), body))
        buffer.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        previous = lines[index - 1] if index else None
        found = heading_of(line, previous)

        if found:
            title, level = found
            # A setext heading's title is the previous line, already buffered.
            if not line.lstrip().startswith("#") and buffer:
                buffer.pop()
            flush()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
        else:
            buffer.append(line)
        index += 1

    flush()
    return sections or [("", text)]


def _pack(pieces, size, overlap):
    """Greedily pack pieces up to ``size``, carrying ``overlap`` characters over."""
    chunks, current = [], ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        candidate = f"{current}\n\n{piece}".strip() if current else piece
        if len(candidate) <= size or not current:
            current = candidate
            continue
        chunks.append(current)
        tail = current[-overlap:] if overlap else ""
        # Resume at a word boundary so the overlap does not begin mid-word.
        if tail and " " in tail:
            tail = tail[tail.index(" ") + 1:]
        current = f"{tail}\n\n{piece}".strip() if tail else piece
    if current:
        chunks.append(current)
    return chunks


def _hard_split(text, size, overlap):
    """Last resort for prose with no paragraph or sentence breaks at all."""
    step = max(size - overlap, 1)
    return [text[start:start + size] for start in range(0, len(text), step)
            if text[start:start + size].strip()]


def _split_body(body, size, overlap):
    paragraphs = [p for p in re.split(r"\n\s*\n", body) if p.strip()]

    pieces = []
    for paragraph in paragraphs:
        if len(paragraph) <= size:
            pieces.append(paragraph)
            continue
        sentences = [s for s in _SENTENCE_RE.split(paragraph) if s.strip()]
        if len(sentences) > 1:
            pieces.extend(sentences)
        else:
            pieces.extend(_hard_split(paragraph, size, overlap))

    packed = _pack(pieces, size, overlap)
    return [c for c in packed if c.strip()]


def _merge_fragments(chunks):
    """Fold a too-small chunk into its neighbour instead of storing it alone."""
    merged = []
    for chunk in chunks:
        if merged and len(chunk) < MIN_CHUNK_CHARS:
            merged[-1] = f"{merged[-1]}\n\n{chunk}"
        else:
            merged.append(chunk)
    # A single leading fragment has no previous neighbour; fold it forward.
    if len(merged) > 1 and len(merged[0]) < MIN_CHUNK_CHARS:
        merged[1] = f"{merged[0]}\n\n{merged[1]}"
        merged.pop(0)
    return merged


def chunk_document(text, *, source_type="text", size=None, overlap=None,
                   metadata=None):
    """Split cleaned text into ordered :class:`Chunk` objects."""
    size = size or DEFAULT_CHUNK_SIZE
    overlap = overlap if overlap is not None else DEFAULT_OVERLAP
    if overlap >= size:
        raise ValueError("chunk overlap must be smaller than chunk size")

    chunks, ordinal = [], 0
    for section, body in _split_sections(text, is_markdown=source_type == "markdown"):
        for piece in _merge_fragments(_split_body(body, size, overlap)):
            chunks.append(Chunk(
                text=piece.strip(), ordinal=ordinal, section=section,
                metadata=dict(metadata or {})))
            ordinal += 1
    return chunks
