"""Knowledge Base endpoints — administrators only, end to end.

**Every endpoint here requires the admin role, including search.** The brief
gives patients read access "through *future* retrieval", which is the AI
copilot that is explicitly out of scope. Shipping a patient-facing search
endpoint now would be starting that work, so the retrieval endpoint exists as
the administrator's tool for checking the corpus, and
``knowledge.retrieval.search`` is the seam a future copilot will call.

Thin like the rest of the API: parse, delegate to ``services``, translate the
domain exception into the project's error envelope.
"""
import logging

from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsPlatformAdmin
from appointments.exceptions import api_error
from appointments.models import Document
from appointments.validators import validate_knowledge_document

from . import rag, retrieval, services
from .models import KnowledgeSource
from .serializers import (
    DocumentCreateSerializer, DocumentSerializer, KnowledgeSourceSerializer,
    RagQuerySerializer, RetrievalQuerySerializer, SourceCreateSerializer,
    SourceReviewSerializer,
)

logger = logging.getLogger("appointments")

MAX_LIMIT = 100
DEFAULT_LIMIT = 25

_STATUS_FOR = [
    (services.NotFound, status.HTTP_404_NOT_FOUND),
    (services.NotAuthorized, status.HTTP_403_FORBIDDEN),
    (services.DuplicateDocument, status.HTTP_409_CONFLICT),
    (services.InvalidTransition, status.HTTP_409_CONFLICT),
    # An ingestion failure is the document's problem, not the request's: the
    # same request would have succeeded with a parseable document.
    (services.IngestionFailed, status.HTTP_422_UNPROCESSABLE_ENTITY),
    # The corpus is unreadable until re-indexed — a service state, not a bad
    # query, and emphatically not "no results".
    (retrieval.RetrievalUnavailable, status.HTTP_503_SERVICE_UNAVAILABLE),
]


class _KnowledgeView(APIView):
    """Base view: admin-only, with this module's exceptions translated."""

    permission_classes = [IsPlatformAdmin]

    def handle_exception(self, exc):
        for exception_type, code in _STATUS_FOR:
            if isinstance(exc, exception_type):
                return api_error(str(exc) or "Error", code)
        return super().handle_exception(exc)


def _paging(request):
    try:
        limit = int(request.query_params.get("limit") or DEFAULT_LIMIT)
        offset = int(request.query_params.get("offset") or 0)
    except (TypeError, ValueError):
        raise ValueError("limit and offset must be whole numbers.")
    return max(min(limit, MAX_LIMIT), 1), max(offset, 0)


def _counts(model, field, ids):
    """One grouped COUNT instead of a query per row."""
    from django.db.models import Count
    rows = (model.objects.filter(**{f"{field}__in": ids})
            .values(field).annotate(total=Count("id")))
    return {row[field]: row["total"] for row in rows}


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
class Vocabulary(_KnowledgeView):
    """Every choice the Knowledge Base offers, so no client hardcodes one."""

    def get(self, request):
        from appointments.services.rag import parsing
        return Response({
            "source_types": [{"value": v, "label": label} for v, label
                             in KnowledgeSource.SOURCE_TYPE_CHOICES],
            "source_statuses": [{"value": v, "label": label} for v, label
                                in KnowledgeSource.STATUS_CHOICES],
            "document_types": [{"value": v, "label": label} for v, label
                               in Document.DOCUMENT_TYPES],
            "document_statuses": [{"value": v, "label": label} for v, label
                                  in Document.STATUS_CHOICES],
            "content_types": [{"value": v, "label": label} for v, label
                              in Document.SOURCE_TYPES],
            # What the pipeline can genuinely parse. Reported rather than
            # implied, because "why was my PDF rejected" is otherwise a
            # mystery: Roshada has no PDF extraction.
            "supported_uploads": sorted(parsing.SUPPORTED_SUFFIXES),
        }, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
class Sources(_KnowledgeView):
    def get(self, request):
        state = request.query_params.get("status")
        if state and state not in dict(KnowledgeSource.STATUS_CHOICES):
            return api_error(f"Unknown source status {state!r}.",
                             status.HTTP_400_BAD_REQUEST)
        queryset = list(services.sources(
            status=state, search=request.query_params.get("q", "")))
        counts = _counts(Document, "knowledge_source",
                         [s.id for s in queryset])
        return Response(
            KnowledgeSourceSerializer(
                queryset, many=True,
                context={"document_counts": counts}).data,
            status=status.HTTP_200_OK)

    def post(self, request):
        serializer = SourceCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        source = services.create_source(request.user,
                                        **serializer.validated_data)
        return Response(KnowledgeSourceSerializer(source).data,
                        status=status.HTTP_201_CREATED)


class SourceDetail(_KnowledgeView):
    def get(self, request, source_id):
        source = services.get_source(source_id)
        return Response(KnowledgeSourceSerializer(source).data,
                        status=status.HTTP_200_OK)


class SourceReview(_KnowledgeView):
    """Approve, reject or archive — the gate on everything downstream."""

    def post(self, request, source_id):
        serializer = SourceReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        source = services.review_source(
            request.user, source_id, serializer.validated_data["status"],
            serializer.validated_data.get("notes", ""))
        return Response(KnowledgeSourceSerializer(source).data,
                        status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
class Documents(_KnowledgeView):
    """GET the corpus · POST a document (text body or .txt/.md upload)."""

    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request):
        try:
            limit, offset = _paging(request)
        except ValueError as exc:
            return api_error(str(exc), status.HTTP_400_BAD_REQUEST)

        state = request.query_params.get("status")
        if state and state not in dict(Document.STATUS_CHOICES):
            return api_error(f"Unknown document status {state!r}.",
                             status.HTTP_400_BAD_REQUEST)

        queryset = services.documents(
            status=state, search=request.query_params.get("q", ""),
            active_only=request.query_params.get("active") == "true")
        total = queryset.count()
        page = list(queryset[offset:offset + limit])

        from appointments.models import DocumentChunk
        counts = _counts(DocumentChunk, "document", [d.id for d in page])
        return Response({
            "results": DocumentSerializer(
                page, many=True, context={"chunk_counts": counts}).data,
            "count": len(page), "total": total, "limit": limit,
            "offset": offset, "has_more": offset + len(page) < total,
        }, status=status.HTTP_200_OK)

    def post(self, request):
        uploaded = request.FILES.get("file")
        text = request.data.get("text", "")
        if uploaded is not None:
            # Authoritative: the bytes must decode as text, whatever the
            # filename or Content-Type claims.
            text = validate_knowledge_document(uploaded)

        serializer = DocumentCreateSerializer(
            data=request.data, context={"uploaded_file": uploaded})
        serializer.is_valid(raise_exception=True)
        payload = dict(serializer.validated_data)
        payload.pop("text", None)
        source_id = payload.pop("source_id")

        document = services.add_document(
            request.user, source_id, text=text, uploaded_file=uploaded,
            **payload)
        return Response(DocumentSerializer(document).data,
                        status=status.HTTP_201_CREATED)


class DocumentDetail(_KnowledgeView):
    def get(self, request, document_id):
        document = services.get_document(document_id)
        return Response(DocumentSerializer(document).data,
                        status=status.HTTP_200_OK)


class DocumentVersions(_KnowledgeView):
    """Every version of one document identity, newest first."""

    def get(self, request, document_id):
        document = services.get_document(document_id)
        versions = services.versions_of(document.source)
        return Response(DocumentSerializer(versions, many=True).data,
                        status=status.HTTP_200_OK)


class DocumentReindex(_KnowledgeView):
    def post(self, request, document_id):
        document = services.reindex_document(request.user, document_id)
        return Response(DocumentSerializer(document).data,
                        status=status.HTTP_200_OK)


class DocumentArchive(_KnowledgeView):
    def post(self, request, document_id):
        restore = bool(request.data.get("restore"))
        document = (services.restore_document(request.user, document_id)
                    if restore else
                    services.archive_document(request.user, document_id))
        return Response(DocumentSerializer(document).data,
                        status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Retrieval foundation and diagnostics
# ---------------------------------------------------------------------------
class Retrieve(_KnowledgeView):
    """Search the corpus. Approved sources and live processed documents only.

    Administrator-facing: this is the tool for checking what the corpus would
    answer with. It is not a patient endpoint and it is not RAG — no language
    model is called, and no answer is composed.
    """

    def get(self, request):
        serializer = RetrievalQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        results = retrieval.search(
            data["q"], top_k=data.get("top_k", 5),
            language=data.get("language") or None,
            specialty=data.get("specialty") or None,
            source=data.get("source") or None,
            document=data.get("document") or None,
            section=data.get("section") or None)
        return Response({
            "query": data["q"],
            "results": results,
            "count": len(results),
            "coverage": retrieval.coverage(),
        }, status=status.HTTP_200_OK)


class IndexStatus(_KnowledgeView):
    """What is indexed, what is retrievable, and in which embedding space."""

    def get(self, request):
        return Response(services.index_status(), status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------
class RagQuery(_KnowledgeView):
    """Answer a question from the approved corpus, with citations.

    **Administrator-only, like everything else in this module.** This is the
    infrastructure a future patient copilot will call, not the copilot itself:
    there is no conversation, no memory, no patient context and no tool use.
    Exposing it to patients now would be shipping that copilot without its
    safety work.

    Throttled under the existing ``ai`` scope — each request is a billable
    model call, the same as the assistant's.
    """

    throttle_scope = "ai"

    def post(self, request):
        serializer = RagQuerySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = rag.answer(
            data["query"],
            language=data.get("language") or None,
            specialty=data.get("specialty") or None,
            topic=data.get("topic") or None,
            source=data.get("source") or None,
            document=data.get("document") or None,
            top_k=data.get("top_k") or None)

        # The retrieved passages are a debugging aid and are returned only when
        # asked for. This endpoint is admin-only, so the flag is a convenience
        # rather than the access control — that is the permission class.
        return Response(
            result.as_dict(include_retrieved=bool(data.get("debug"))),
            status=status.HTTP_200_OK)


class RagStatus(_KnowledgeView):
    """The active RAG configuration: retrieval, LLM, embeddings, prompt."""

    def get(self, request):
        return Response(rag.describe(), status=status.HTTP_200_OK)
