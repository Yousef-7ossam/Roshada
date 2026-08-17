"""Knowledge Base use-cases: source governance, ingestion, re-indexing.

This is the *domain* layer over ``appointments.services.rag``, which is the
engine. The engine parses, cleans, chunks, embeds and stores; it has no opinion
about whether a document is trustworthy or current. Everything that is an
opinion lives here:

* which sources may be used, and who decided
* which version of a document is live
* what a failed ingestion is allowed to say about itself
* what retrieval is allowed to return

The split is the one the rest of the platform already uses — ``radiology`` over
the scheduling engine, ``records`` over the domain modules. Nothing here
re-implements a pipeline step.
"""
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts import roles
from accounts.services import role_of
from appointments.models import Document, DocumentChunk
from appointments.services import rag

from .models import KnowledgeSource

logger = logging.getLogger("appointments")

#: Errors are shown in the admin UI and stored in the database, and provider
#: exceptions can carry an endpoint, a model name or a fragment of a key. This
#: is the ceiling on what gets kept.
MAX_ERROR_CHARS = 400


class NotAuthorized(Exception):
    """The caller may not manage the Knowledge Base."""


class NotFound(Exception):
    """No such source or document."""


class InvalidTransition(Exception):
    """The requested status change is not permitted from the current state."""


class DuplicateDocument(Exception):
    """This exact content is already in the corpus."""


class IngestionFailed(Exception):
    """The pipeline could not process this document."""


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------
def may_manage(user):
    """Only platform administrators curate the Knowledge Base.

    Every other role is refused, doctors included. The corpus is general
    medical reference material that will one day ground answers shown to
    patients; who may add to it is a governance decision, not a clinical one,
    and a doctor's clinical authority is not authority over what the platform
    tells everybody.
    """
    return user is not None and getattr(user, "is_authenticated", False) \
        and role_of(user) == roles.ADMIN


def _require_manager(user):
    if not may_manage(user):
        raise NotAuthorized(
            "Only administrators can manage the medical knowledge base.")


def safe_error(exc):
    """What a failure is allowed to record about itself.

    Truncated, single-line, and with anything key-shaped removed. The message
    is stored and displayed, so a provider exception that quotes a request
    header must not turn the document list into a credential leak.
    """
    import re

    text = str(exc) or exc.__class__.__name__
    text = " ".join(text.split())
    # Long opaque tokens (sk-..., bearer values, hex secrets) never help a
    # reader and sometimes are the secret.
    text = re.sub(r"\b(sk|pk|key|token|bearer)[-_ :]?[A-Za-z0-9_\-]{8,}",
                  "[redacted]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[A-Za-z0-9_\-]{32,}\b", "[redacted]", text)
    return text[:MAX_ERROR_CHARS]


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def sources(status=None, search=""):
    queryset = KnowledgeSource.objects.all()
    if status:
        queryset = queryset.filter(verification_status=status)
    if search:
        from django.db.models import Q
        queryset = queryset.filter(Q(name__icontains=search)
                                   | Q(organization__icontains=search))
    return queryset


def get_source(source_id):
    source = KnowledgeSource.objects.filter(pk=source_id).first()
    if source is None:
        raise NotFound()
    return source


def create_source(user, name, **fields):
    """Register a source. It starts PENDING — never trusted on arrival."""
    _require_manager(user)
    name = (name or "").strip()
    if not name:
        raise InvalidTransition("A source needs a name.")
    if KnowledgeSource.objects.filter(name__iexact=name).exists():
        raise DuplicateDocument(f"A source named {name!r} already exists.")

    allowed = {"organization", "source_type", "url", "description", "language",
               "specialty"}
    source = KnowledgeSource.objects.create(
        name=name, **{k: v for k, v in fields.items()
                      if k in allowed and v is not None})
    logger.info("KB: source %s (%s) registered by %s", source.id, source.name,
                user.username)
    return source


def review_source(user, source_id, status, notes=""):
    """Approve, reject or archive a source.

    Approving is the moment its material becomes retrievable, which is why it
    is an explicit administrative act with a reason attached rather than a
    side effect of uploading a document.
    """
    _require_manager(user)
    source = get_source(source_id)
    if not source.can_transition_to(status):
        raise InvalidTransition(
            f"A {source.get_verification_status_display().lower()} source "
            f"cannot become {status}.")

    source.verification_status = status
    source.review_notes = notes or source.review_notes
    source.reviewed_at = timezone.now()
    source.save(update_fields=["verification_status", "review_notes",
                               "reviewed_at", "updated_at"])
    logger.info("KB: source %s -> %s by %s", source.id, status, user.username)
    return source


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
def documents(source=None, status=None, search="", active_only=False):
    queryset = (Document.objects
                .select_related("knowledge_source")
                .prefetch_related("chunks"))
    if source is not None:
        queryset = queryset.filter(knowledge_source=source)
    if status:
        queryset = queryset.filter(status=status)
    if active_only:
        queryset = queryset.filter(is_active=True)
    if search:
        queryset = queryset.filter(title__icontains=search)
    return queryset


def get_document(document_id):
    document = (Document.objects.select_related("knowledge_source")
                .filter(pk=document_id).first())
    if document is None:
        raise NotFound()
    return document


def content_digest(text, source_type="text"):
    """The checksum ingestion will store for this content.

    Must match the engine's own calculation exactly — parse first (which strips
    frontmatter), then clean, then hash. Duplicate detection compares against
    what ingestion stored, so a digest computed any other way compares two
    different things and never matches.
    """
    from appointments.services.rag import cleaning, parsing
    parsed = parsing.parse_text(text, source_type=source_type)
    return cleaning.checksum(cleaning.clean(parsed.text))


def _stored_name(document):
    suffix = ".md" if document.source_type == Document.MARKDOWN else ".txt"
    safe = "".join(c if c.isalnum() or c in "-_" else "-"
                   for c in document.source)[:80].strip("-") or "document"
    return f"{safe}-v{document.version}{suffix}"


def _next_version(identity):
    from django.db.models import Max
    current = (Document.objects.filter(source=identity)
               .aggregate(top=Max("version"))["top"])
    return (current or 0) + 1


def add_document(user, source_id, *, identity, title, text, source_type="text",
                 document_type=Document.REFERENCE, language="en",
                 description="", url="", metadata=None, publication_date=None,
                 review_date=None, uploaded_file=None, process=True):
    """Register a document against a source, and index it.

    Versioning is decided here, not by the engine. If the identity already
    exists, this becomes the next version: a new row, marked active, with the
    previous one deactivated and linked through ``supersedes``. History is kept
    — a guideline that has been superseded is still a record of what the
    platform said last year.

    Duplicate content is refused before anything is embedded: the same bytes
    arriving under a new title would otherwise be indexed twice and returned
    twice, which reads to a user as corroboration.
    """
    _require_manager(user)
    source = get_source(source_id)

    identity = (identity or "").strip()
    if not identity:
        raise InvalidTransition("A document needs a source identity.")
    if not (text or "").strip():
        raise InvalidTransition("That document has no text.")

    # Computed exactly as the engine computes it — parse *then* clean. Doing
    # only half of that produced a different digest from the one ingestion
    # stores, so duplicate detection silently never matched.
    digest = content_digest(text, source_type)

    clash = Document.objects.filter(checksum=digest).exclude(
        source=identity).first()
    if clash is not None:
        raise DuplicateDocument(
            f"Identical content is already indexed as '{clash.title}' "
            f"(v{clash.version}).")

    previous = Document.objects.filter(source=identity,
                                       is_active=True).first()
    unchanged = previous is not None and previous.checksum == digest
    if unchanged:
        raise DuplicateDocument(
            f"'{previous.title}' v{previous.version} already holds this exact "
            f"content. Edit it, or use re-index to rebuild its vectors.")

    with transaction.atomic():
        if previous is not None:
            # Stand the old version down *before* the new one claims active:
            # the partial unique index allows exactly one live version.
            previous.is_active = False
            previous.save(update_fields=["is_active", "updated_at"])

        document = Document.objects.create(
            knowledge_source=source, source=identity, title=title or identity,
            source_type=source_type, document_type=document_type,
            language=language or source.language, description=description,
            url=url, metadata=metadata or {},
            publication_date=publication_date, review_date=review_date,
            version=_next_version(identity), is_active=True,
            supersedes=previous, status=Document.UPLOADED)
        # The text is always persisted, whether it arrived as a file or as a
        # paste. Re-indexing rebuilds from stored text, so a document with no
        # stored copy could be indexed once and never again — and the chunks
        # are a lossy record, not a source of truth.
        if uploaded_file is not None:
            document.file = uploaded_file
        else:
            from django.core.files.base import ContentFile
            document.file.save(_stored_name(document),
                               ContentFile(text.encode("utf-8")), save=False)
        document.save(update_fields=["file"])

    logger.info("KB: document %s '%s' v%s registered under source %s",
                document.id, document.title, document.version, source.id)

    if process:
        process_document(user, document.id, text=text)
        document.refresh_from_db()
    return document


def process_document(user, document_id, text=None, force=True):
    """Run the ingestion pipeline for one document and record what happened.

    Synchronous on purpose. Roshada has no Celery, no Redis and no task queue,
    and the brief is explicit that one should not be introduced for this — so
    processing is a service call, with a management command for bulk work. The
    corpus is reference material measured in hundreds of documents, not a feed.
    """
    _require_manager(user)
    document = get_document(document_id)

    if text is None:
        text = _text_of(document)

    document.status = Document.PROCESSING
    document.processing_started_at = timezone.now()
    document.error_message = ""
    document.save(update_fields=["status", "processing_started_at",
                                 "error_message", "updated_at"])
    logger.info("KB: ingestion started for document %s", document.id)

    try:
        result = rag.ingest_text(
            text, source=document.source, title=document.title,
            source_type=document.source_type,
            metadata=_chunk_metadata(document), force=force,
            # The row is handed in: several versions share one identity, so
            # the engine must not try to look one up by source.
            document=document)
    except Exception as exc:                                # noqa: BLE001
        document.status = Document.FAILED
        document.error_message = safe_error(exc)
        document.save(update_fields=["status", "error_message", "updated_at"])
        logger.warning("KB: ingestion failed for document %s: %s",
                       document.id, document.error_message)
        raise IngestionFailed(document.error_message) from None

    document.refresh_from_db()
    document.status = Document.PROCESSED
    document.processed_at = timezone.now()
    document.error_message = ""
    document.save(update_fields=["status", "processed_at", "error_message",
                                 "updated_at"])
    logger.info("KB: indexed document %s as %d chunks (%s)", document.id,
                result.chunks, result.space)
    return document


def _text_of(document):
    """The document's text, from its uploaded file if there is one."""
    if not document.file:
        raise IngestionFailed(
            "This document has no stored text to process. Re-upload it.")
    try:
        document.file.open("rb")
        raw = document.file.read()
    finally:
        try:
            document.file.close()
        except Exception:
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise IngestionFailed("The stored file is not UTF-8 text.")


def _chunk_metadata(document):
    """What every chunk of this document carries.

    Provenance is copied onto the chunk deliberately. A chunk can be retrieved
    long after the fact and must be citable on its own — and the *values that
    were true at indexing time* are what the answer was based on. Live status
    is read from the relation at query time; this is the record of what was
    indexed.
    """
    source = document.knowledge_source
    return {
        "source_id": source.id if source else None,
        "source_name": source.name if source else "",
        "source_type": source.source_type if source else "",
        "specialty": source.specialty if source else "",
        "document_id": document.id,
        "document_title": document.title,
        "document_version": document.version,
        "document_type": document.document_type,
        "language": document.language,
        "url": document.url,
        "publication_date": (document.publication_date.isoformat()
                             if document.publication_date else None),
        "verification_status": (source.verification_status if source
                                else "unverified"),
        **(document.metadata or {}),
    }


def reindex_document(user, document_id):
    """Rebuild a document's vectors from its stored text.

    The one supported way to change what is indexed. Nothing else in the
    application writes to ``DocumentChunk``: the engine replaces a document's
    chunks wholesale, so a re-index cannot leave half the old passages
    retrievable alongside the new ones.
    """
    _require_manager(user)
    return process_document(user, document_id, force=True)


def archive_document(user, document_id):
    """Withdraw a document from retrieval without destroying it."""
    _require_manager(user)
    document = get_document(document_id)
    if document.status == Document.ARCHIVED:
        raise InvalidTransition("That document is already archived.")

    with transaction.atomic():
        document.status = Document.ARCHIVED
        document.is_active = False
        document.save(update_fields=["status", "is_active", "updated_at"])
    logger.info("KB: document %s archived by %s", document.id, user.username)
    return document


def restore_document(user, document_id):
    """Bring an archived document back as the live version of its identity."""
    _require_manager(user)
    document = get_document(document_id)
    if document.status != Document.ARCHIVED:
        raise InvalidTransition("Only an archived document can be restored.")

    try:
        with transaction.atomic():
            Document.objects.filter(source=document.source, is_active=True)\
                .exclude(pk=document.pk).update(is_active=False)
            document.is_active = True
            document.status = (Document.PROCESSED if document.chunks.exists()
                               else Document.UPLOADED)
            document.save(update_fields=["is_active", "status", "updated_at"])
    except IntegrityError:
        raise InvalidTransition(
            "Another version of this document is already active.")
    return document


def versions_of(identity):
    """Every version of one document identity, newest first."""
    return (Document.objects.filter(source=identity)
            .select_related("knowledge_source").order_by("-version"))


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
def index_status():
    """What is indexed, what is retrievable, and in which embedding space."""
    from django.db.models import Count

    by_status = {row["status"]: row["count"] for row in
                 Document.objects.values("status").annotate(count=Count("id"))}
    retrievable = Document.objects.filter(
        is_active=True, status=Document.PROCESSED,
        knowledge_source__verification_status=KnowledgeSource.APPROVED).count()

    try:
        embedder = rag.embedder_info()
    except Exception as exc:                                # noqa: BLE001
        embedder = {"error": safe_error(exc)}

    return {
        "documents": Document.objects.count(),
        "documents_by_status": by_status,
        "retrievable_documents": retrievable,
        "sources": KnowledgeSource.objects.count(),
        "sources_by_status": {
            row["verification_status"]: row["count"] for row in
            KnowledgeSource.objects.values("verification_status")
            .annotate(count=Count("id"))},
        "chunks": DocumentChunk.objects.count(),
        # Which vectors exist, in which space — the answer to "why does search
        # say reindex required".
        "index": rag.stats(),
        "embedding": embedder,
    }
