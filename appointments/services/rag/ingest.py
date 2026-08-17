"""The ingestion pipeline.

    document -> parse -> clean -> chunk -> metadata -> embed -> store

One function, :func:`ingest_text`, runs the whole thing; :func:`ingest_file` and
:func:`ingest_directory` are conveniences over it.

Re-ingestion is idempotent by checksum: unchanged content in the active
embedding space is skipped rather than re-embedded, which matters because
embedding is the one step that costs money.
"""
import logging
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction

from ...models import Document, DocumentChunk
from . import chunking, cleaning, parsing, store
from .embeddings import resolve

logger = logging.getLogger("appointments")


@dataclass
class IngestResult:
    document: Document
    chunks: int
    skipped: bool = False
    space: str = ""

    def as_dict(self):
        return {"document": self.document.title, "source": self.document.source,
                "chunks": self.chunks, "skipped": self.skipped,
                "space": self.space}


def _needs_reindex(document, digest, space):
    """True unless this exact content is already indexed in this exact space."""
    if document.checksum != digest:
        return True
    return not DocumentChunk.objects.filter(
        document=document, embedder=space.embedder,
        embedding_model=space.model, dimension=space.dimension).exists()


def ingest_text(raw, *, source, title="", source_type="text", metadata=None,
                force=False, embedder=None, document=None):
    """Run the full pipeline for one document's content.

    ``document`` lets a caller that already owns a row — the Knowledge Base,
    which creates versions itself — hand it in instead of having one looked up
    by ``source``. That lookup cannot serve a versioned corpus: several rows
    share one ``source`` identity, so ``get_or_create(source=...)`` would be
    ambiguous the moment a second version exists.
    """
    embedder = embedder or resolve()
    space = embedder.space

    parsed = parsing.parse_text(raw, title=title, source_type=source_type)
    cleaned = cleaning.clean(parsed.text)
    if not cleaned:
        raise parsing.UnsupportedDocument(
            f"'{source}' has no text left after cleaning.")
    digest = cleaning.checksum(cleaned)

    # Frontmatter is the document's own metadata; the caller's takes precedence.
    combined = {**parsed.metadata, **(metadata or {})}

    if document is None:
        document, _ = Document.objects.get_or_create(
            source=source, is_active=True,
            defaults={"title": parsed.title, "source_type": parsed.source_type,
                      "metadata": combined})

    if not force and not _needs_reindex(document, digest, space):
        logger.info("RAG: '%s' unchanged, skipping", source)
        return IngestResult(document, document.chunks.count(), skipped=True,
                            space=str(space))

    document.title = title or parsed.title or document.title
    document.source_type = parsed.source_type
    document.metadata = combined
    document.checksum = digest
    document.save(update_fields=["title", "source_type", "metadata",
                                 "checksum", "updated_at"])

    chunks = chunking.chunk_document(cleaned, source_type=parsed.source_type,
                                     metadata=combined)
    if not chunks:
        raise parsing.UnsupportedDocument(f"'{source}' produced no chunks.")

    # The one step that costs money and time, so it happens once per changed
    # document, in batches, over the whole set.
    vectors = embedder.embed_documents([chunk.text for chunk in chunks])

    with transaction.atomic():
        store.replace_chunks(document, chunks, vectors, space)

    logger.info("RAG: indexed '%s' as %d chunks in %s", source, len(chunks), space)
    return IngestResult(document, len(chunks), space=str(space))


def ingest_file(path, *, metadata=None, force=False, embedder=None):
    path = Path(path)
    parsed = parsing.parse_file(path)
    return ingest_text(parsed.text, source=str(path), title=parsed.title,
                       source_type=parsed.source_type,
                       metadata={**parsed.metadata, **(metadata or {})},
                       force=force, embedder=embedder)


def ingest_directory(directory, *, metadata=None, force=False, embedder=None,
                     recursive=True):
    """Ingest every supported file in a directory.

    One unreadable file does not abandon the rest of the corpus — it is reported
    and skipped, so a single bad document cannot leave the index half-built.
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise parsing.UnsupportedDocument(f"Not a directory: {directory}")

    pattern = "**/*" if recursive else "*"
    results, failures = [], []
    for path in sorted(directory.glob(pattern)):
        if not path.is_file() or path.suffix.lower() not in parsing.SUPPORTED_SUFFIXES:
            continue
        try:
            results.append(ingest_file(path, metadata=metadata, force=force,
                                       embedder=embedder))
        except Exception as exc:
            logger.warning("RAG: could not ingest %s: %s", path, exc)
            failures.append((str(path), str(exc)))
    return results, failures


@transaction.atomic
def remove(source):
    """Delete a document and its chunks. Returns True if anything was removed."""
    deleted, _ = Document.objects.filter(source=source).delete()
    return bool(deleted)
