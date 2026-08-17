"""Knowledge Base input validation and output shapes.

No serializer here exposes a filesystem path. ``Document.file`` is deliberately
absent from every payload — the presence of a stored file is reported as a
boolean, because a storage path is infrastructure detail and, under a media
root that is not routed, a path a client could do nothing with anyway.
"""
from rest_framework import serializers

from appointments.models import Document

from .models import KnowledgeSource


class KnowledgeSourceSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_verification_status_display",
                                         read_only=True)
    source_type_label = serializers.CharField(source="get_source_type_display",
                                              read_only=True)
    is_approved = serializers.BooleanField(read_only=True)
    document_count = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeSource
        fields = ["id", "name", "organization", "source_type",
                  "source_type_label", "url", "description",
                  "verification_status", "status_label", "is_approved",
                  "review_notes", "reviewed_at", "language", "specialty",
                  "document_count", "created_at", "updated_at"]

    def get_document_count(self, obj):
        # Annotated by the view in one query; never a query per row.
        counts = self.context.get("document_counts")
        if counts is not None:
            return counts.get(obj.id, 0)
        return obj.documents.count()


class SourceCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    organization = serializers.CharField(max_length=200, required=False,
                                         allow_blank=True, default="")
    source_type = serializers.ChoiceField(
        choices=[c[0] for c in KnowledgeSource.SOURCE_TYPE_CHOICES],
        default=KnowledgeSource.OTHER)
    url = serializers.URLField(max_length=500, required=False,
                               allow_blank=True, default="")
    description = serializers.CharField(required=False, allow_blank=True,
                                        default="")
    language = serializers.CharField(max_length=20, required=False,
                                     allow_blank=True, default="en")
    specialty = serializers.CharField(max_length=120, required=False,
                                      allow_blank=True, default="")


class SourceReviewSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[c[0] for c in KnowledgeSource.STATUS_CHOICES])
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class DocumentSerializer(serializers.ModelSerializer):
    status_label = serializers.CharField(source="get_status_display",
                                         read_only=True)
    document_type_label = serializers.CharField(
        source="get_document_type_display", read_only=True)
    source_name = serializers.SerializerMethodField()
    chunk_count = serializers.SerializerMethodField()
    is_retrievable = serializers.BooleanField(read_only=True)
    has_file = serializers.SerializerMethodField()

    class Meta:
        model = Document
        # Note the absence of `file`: see the module docstring.
        fields = ["id", "title", "source", "source_type", "document_type",
                  "document_type_label", "knowledge_source", "source_name",
                  "language", "description", "url", "version", "is_active",
                  "supersedes", "publication_date", "review_date", "status",
                  "status_label", "is_retrievable", "processing_started_at",
                  "processed_at", "error_message", "checksum", "metadata",
                  "chunk_count", "has_file", "created_at", "updated_at"]

    def get_source_name(self, obj):
        return obj.knowledge_source.name if obj.knowledge_source else ""

    def get_chunk_count(self, obj):
        counts = self.context.get("chunk_counts")
        if counts is not None:
            return counts.get(obj.id, 0)
        return obj.chunks.count()

    def get_has_file(self, obj):
        return bool(obj.file)


class DocumentCreateSerializer(serializers.Serializer):
    """Register a document from pasted text or an uploaded file.

    One of ``text`` or ``file`` is required — validated together, so "neither"
    is refused here rather than surfacing as a confusing pipeline error.
    """
    source_id = serializers.IntegerField()
    identity = serializers.CharField(max_length=500)
    title = serializers.CharField(max_length=300, required=False,
                                  allow_blank=True, default="")
    text = serializers.CharField(required=False, allow_blank=True, default="")
    source_type = serializers.ChoiceField(
        choices=[c[0] for c in Document.SOURCE_TYPES], default=Document.TEXT)
    document_type = serializers.ChoiceField(
        choices=[c[0] for c in Document.DOCUMENT_TYPES],
        default=Document.REFERENCE)
    language = serializers.CharField(max_length=20, required=False,
                                     allow_blank=True, default="en")
    description = serializers.CharField(required=False, allow_blank=True,
                                        default="")
    url = serializers.URLField(max_length=500, required=False,
                               allow_blank=True, default="")
    publication_date = serializers.DateField(required=False, allow_null=True)
    review_date = serializers.DateField(required=False, allow_null=True)
    metadata = serializers.JSONField(required=False, default=dict)

    def validate(self, attrs):
        has_file = bool(self.context.get("uploaded_file"))
        if not attrs.get("text", "").strip() and not has_file:
            raise serializers.ValidationError(
                "Provide the document text, or upload a .txt/.md file.")
        return attrs


class RetrievalQuerySerializer(serializers.Serializer):
    q = serializers.CharField(max_length=1000)
    top_k = serializers.IntegerField(min_value=1, max_value=20, default=5)
    language = serializers.CharField(max_length=20, required=False,
                                     allow_blank=True)
    specialty = serializers.CharField(max_length=120, required=False,
                                      allow_blank=True)
    source = serializers.IntegerField(required=False, allow_null=True)
    document = serializers.IntegerField(required=False, allow_null=True)
    section = serializers.CharField(max_length=300, required=False,
                                    allow_blank=True)


class RagQuerySerializer(serializers.Serializer):
    """Input to the grounded-answer endpoint.

    ``debug`` opts into the retrieved passages. It is a convenience for the
    admin tooling, not an access control — the permission class is.
    """
    query = serializers.CharField(max_length=1000)
    language = serializers.CharField(max_length=20, required=False,
                                     allow_blank=True)
    specialty = serializers.CharField(max_length=120, required=False,
                                      allow_blank=True)
    topic = serializers.CharField(max_length=120, required=False,
                                  allow_blank=True)
    source = serializers.IntegerField(required=False, allow_null=True)
    document = serializers.IntegerField(required=False, allow_null=True)
    top_k = serializers.IntegerField(min_value=1, max_value=20, required=False)
    debug = serializers.BooleanField(required=False, default=False)
