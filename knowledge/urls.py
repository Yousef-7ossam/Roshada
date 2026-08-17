"""Knowledge Base routes.

Included under the same ``/api/`` prefix as the rest of the platform, following
the existing convention: nouns, trailing slashes, actions as a sub-path.
"""
from django.urls import path

from .views import (
    DocumentArchive, DocumentDetail, DocumentReindex, DocumentVersions,
    Documents, IndexStatus, RagQuery, RagStatus, Retrieve, SourceDetail,
    SourceReview, Sources, Vocabulary,
)

app_name = "knowledge"

urlpatterns = [
    path("knowledge/vocabulary/", Vocabulary.as_view(), name="vocabulary"),

    # ---- Sources ----
    path("knowledge/sources/", Sources.as_view(), name="sources"),
    path("knowledge/sources/<int:source_id>/", SourceDetail.as_view(),
         name="source-detail"),
    path("knowledge/sources/<int:source_id>/review/", SourceReview.as_view(),
         name="source-review"),

    # ---- Documents ----
    path("knowledge/documents/", Documents.as_view(), name="documents"),
    path("knowledge/documents/<int:document_id>/", DocumentDetail.as_view(),
         name="document-detail"),
    path("knowledge/documents/<int:document_id>/versions/",
         DocumentVersions.as_view(), name="document-versions"),
    path("knowledge/documents/<int:document_id>/reindex/",
         DocumentReindex.as_view(), name="document-reindex"),
    path("knowledge/documents/<int:document_id>/archive/",
         DocumentArchive.as_view(), name="document-archive"),

    # ---- Retrieval foundation + diagnostics ----
    path("knowledge/search/", Retrieve.as_view(), name="search"),
    path("knowledge/index/", IndexStatus.as_view(), name="index-status"),

    # ---- RAG: grounded answering over the approved corpus ----
    path("knowledge/rag/query/", RagQuery.as_view(), name="rag-query"),
    path("knowledge/rag/status/", RagStatus.as_view(), name="rag-status"),
]
