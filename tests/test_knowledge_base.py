"""The Medical Knowledge Base.

Two guarantees carry this module, and the tests are shaped around them:

* **The approval gate.** A perfectly processed document under an unapproved
  source must be absent from retrieval, and must appear the moment the source
  is approved — with no change on the document's side. Same shape as
  radiology's draft-report gate.
* **One live version.** A document identity has exactly one active version,
  guaranteed by a partial unique index in PostgreSQL rather than by the service
  that maintains it. Proven by writing directly through the ORM.

The whole pipeline is exercised for real. The offline hashing embedder is
deterministic and always configured, so ingestion, chunking, embedding,
indexing and retrieval all run with no API key and no network — which is why
section 35's realistic document is a genuine end-to-end test rather than a mock.
"""
import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts import roles
from accounts.models import UserAccount
from accounts.services import register_account
from appointments.models import Document, DocumentChunk
from knowledge import retrieval, services
from knowledge.models import KnowledgeSource

pytestmark = pytest.mark.django_db

PW = "Str0ng!Passw0rd"

#: A small, real reference document. Its wording is ordinary public health
#: information — the test asserts that retrieval *finds* it, never that any
#: clinical claim in it is correct.
HYPERTENSION = """---
title: Hypertension
language: en
---

# Hypertension

High blood pressure, or hypertension, is a long-term condition in which the
force of the blood against the artery walls stays elevated over time. It is
measured in millimetres of mercury and written as two numbers.

## Symptoms

Hypertension is often called a silent condition because most people have no
symptoms at all. When symptoms do occur they may include headaches, shortness
of breath, nosebleeds, flushing and visual changes. These signs are not
specific to high blood pressure and usually appear only once readings are
severely raised.

## Measurement

A diagnosis is not made from a single reading. Blood pressure is measured on
more than one occasion, at rest, with a cuff of the correct size.

## Follow-up

People with raised readings are usually asked to return for repeat
measurement, and may be offered home monitoring.
"""

ASTHMA = """# Asthma

Asthma is a condition affecting the airways. Common features include
wheezing, coughing and shortness of breath, which vary over time.
"""


@pytest.fixture(autouse=True)
def _no_throttling(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK,
                               "DEFAULT_THROTTLE_CLASSES": []}


@pytest.fixture(autouse=True)
def _offline_embedder(monkeypatch):
    """Deterministic, offline embeddings for every test.

    The hashing embedder needs no credential and no network, so the whole
    pipeline is exercised for real rather than mocked.
    """
    from appointments.services.rag import embeddings
    monkeypatch.setenv("RAG_EMBEDDER", "hashing")
    embeddings.reset()
    yield
    embeddings.reset()


@pytest.fixture(autouse=True)
def _isolated_media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")


@pytest.fixture
def client():
    return APIClient()


def make(role, username, **extra):
    defaults = {
        roles.PATIENT: {"age": 41},
        roles.DOCTOR: {"specialization": "Cardiology"},
        roles.RADIOLOGY: {"services": "MRI"},
        roles.LABORATORY: {"services": "CBC"},
        roles.PHARMACY: {"services": "Dispensing"},
    }[role]
    user, _account, _profile, token = register_account(
        role, username=username, password=PW,
        name=username.replace("_", " ").title(), **{**defaults, **extra})
    return user, token.key


def make_admin(username="kb_admin"):
    user = User.objects.create_user(username, password=PW)
    UserAccount.objects.create(user=user, role=roles.ADMIN)
    return user, Token.objects.create(user=user).key


def as_user(client, token):
    client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
    return client


@pytest.fixture
def admin():
    return make_admin()


@pytest.fixture
def approved_source(admin):
    user, _token = admin
    source = services.create_source(
        user, "WHO Cardiovascular", organization="World Health Organization",
        source_type=KnowledgeSource.GOVERNMENT, specialty="cardiology")
    services.review_source(user, source.id, KnowledgeSource.APPROVED,
                           "Recognised authority.")
    source.refresh_from_db()
    return source


def add(user, source, text=HYPERTENSION, identity="who/hypertension",
        title="Hypertension", **extra):
    return services.add_document(
        user, source.id, identity=identity, title=title, text=text,
        source_type="markdown", **extra)


# ---------------------------------------------------------------------------
# Built on the existing corpus, not beside it
# ---------------------------------------------------------------------------
class TestBuiltOnTheExistingEngine:
    def test_the_app_owns_exactly_one_model(self):
        """Roshada already had a corpus; a second one would be the duplicate
        implementation the brief forbids."""
        from django.apps import apps
        names = {m.__name__ for m in apps.get_app_config("knowledge").get_models()}
        assert names == {"KnowledgeSource"}
        for forbidden in ("KnowledgeDocument", "KnowledgeChunk", "VectorRecord",
                          "Embedding", "Vector", "Corpus"):
            assert forbidden not in names

    def test_it_reuses_the_existing_document_and_chunk_tables(self, admin,
                                                              approved_source):
        user, _token = admin
        document = add(user, approved_source)
        assert document._meta.label == "appointments.Document"
        assert document.chunks.exists()
        assert DocumentChunk.objects.filter(document=document).exists()

    def test_only_one_vector_store_is_configured(self):
        """Section 17: not pgvector *and* Chroma *and* Qdrant."""
        import pathlib
        requirements = pathlib.Path("requirements.txt").read_text(
            encoding="utf-8").lower()
        for engine in ("pgvector", "chromadb", "qdrant", "weaviate", "pinecone",
                       "faiss"):
            assert engine not in requirements, (
                f"{engine} would be a second vector store")

    def test_the_knowledge_base_never_touches_patient_records(self):
        """Section 25: general knowledge and patient records stay apart."""
        import pathlib
        for name in ("models.py", "services.py", "retrieval.py", "views.py"):
            text = pathlib.Path("knowledge", name).read_text(encoding="utf-8")
            for forbidden in ("import records", "from records",
                              "MedicalRecord", "ScreeningResult",
                              "Prescription", "RadiologyReport"):
                assert forbidden not in text, (
                    f"knowledge/{name} reaches into patient data ({forbidden})")

    def test_it_registers_no_timeline_or_dashboard_contribution(self):
        """Reference material must not appear in anybody's medical record."""
        from records import timeline
        names = {getattr(s, "__name__", "") for s in timeline.registered()}
        assert not any("knowledge" in n for n in names)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
class TestSources:
    def test_a_new_source_starts_pending(self, admin):
        user, _token = admin
        source = services.create_source(user, "Some Guideline Body")
        assert source.verification_status == KnowledgeSource.PENDING
        assert source.is_approved is False

    def test_approve_reject_and_archive(self, admin):
        user, _token = admin
        source = services.create_source(user, "Reviewable Source")
        approved = services.review_source(user, source.id,
                                          KnowledgeSource.APPROVED, "Trusted.")
        assert approved.is_approved is True
        assert approved.reviewed_at is not None
        assert approved.review_notes == "Trusted."

        archived = services.review_source(user, source.id,
                                          KnowledgeSource.ARCHIVED)
        assert archived.verification_status == KnowledgeSource.ARCHIVED
        assert archived.is_approved is False

    def test_an_illegal_transition_is_refused(self, admin):
        user, _token = admin
        source = services.create_source(user, "Odd Source")
        services.review_source(user, source.id, KnowledgeSource.REJECTED)
        with pytest.raises(services.InvalidTransition):
            services.review_source(user, source.id, KnowledgeSource.ARCHIVED)

    def test_duplicate_source_names_are_refused(self, admin):
        user, _token = admin
        services.create_source(user, "One Source")
        with pytest.raises(services.DuplicateDocument):
            services.create_source(user, "one source")

    def test_the_api_creates_and_reviews(self, client, admin):
        _user, token = admin
        as_user(client, token)
        created = client.post("/api/knowledge/sources/",
                              {"name": "NICE", "organization": "NICE UK",
                               "source_type": "guideline"}, format="json")
        assert created.status_code == 201, created.data
        assert created.data["verification_status"] == "pending"

        reviewed = client.post(
            f"/api/knowledge/sources/{created.data['id']}/review/",
            {"status": "approved", "notes": "National guideline body."},
            format="json")
        assert reviewed.status_code == 200
        assert reviewed.data["is_approved"] is True

    def test_an_unknown_status_filter_is_an_error(self, client, admin):
        _user, token = admin
        as_user(client, token)
        assert client.get(
            "/api/knowledge/sources/?status=nonsense").status_code == 400


# ---------------------------------------------------------------------------
# Documents, versioning and duplicates
# ---------------------------------------------------------------------------
class TestDocuments:
    def test_uploading_runs_the_whole_pipeline(self, admin, approved_source):
        user, _token = admin
        document = add(user, approved_source)
        assert document.status == Document.PROCESSED
        assert document.processing_started_at is not None
        assert document.processed_at is not None
        assert document.error_message == ""
        assert document.checksum
        assert document.chunks.count() >= 2

    def test_chunks_preserve_section_structure(self, admin, approved_source):
        """Markdown headings become the section path, not flattened away."""
        user, _token = admin
        document = add(user, approved_source)
        sections = {c.section for c in document.chunks.all()}
        assert any("Symptoms" in s for s in sections), sections

    def test_a_second_upload_creates_version_two(self, admin, approved_source):
        user, _token = admin
        first = add(user, approved_source)
        second = add(user, approved_source,
                     text=HYPERTENSION + "\n\n## Treatment\n\nLifestyle first.")

        first.refresh_from_db()
        assert (first.version, second.version) == (1, 2)
        assert first.is_active is False and second.is_active is True
        assert second.supersedes_id == first.id
        # History is kept, not overwritten.
        assert Document.objects.filter(source="who/hypertension").count() == 2

    def test_the_database_allows_only_one_active_version(self, admin,
                                                         approved_source):
        """The guarantee is PostgreSQL's, not the service's."""
        user, _token = admin
        add(user, approved_source)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Document.objects.create(
                    knowledge_source=approved_source,
                    source="who/hypertension", title="Sneaky duplicate",
                    version=99, is_active=True)

    def test_the_database_allows_only_one_row_per_version(self, admin,
                                                          approved_source):
        user, _token = admin
        add(user, approved_source)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Document.objects.create(
                    knowledge_source=approved_source,
                    source="who/hypertension", title="Same version",
                    version=1, is_active=False)

    def test_identical_content_is_refused(self, admin, approved_source):
        user, _token = admin
        add(user, approved_source)
        with pytest.raises(services.DuplicateDocument):
            add(user, approved_source)

    def test_identical_content_under_a_new_name_is_also_refused(
            self, admin, approved_source):
        """Detection is by checksum, not filename — section 6."""
        user, _token = admin
        add(user, approved_source)
        with pytest.raises(services.DuplicateDocument):
            add(user, approved_source, identity="who/hypertension-copy",
                title="Hypertension (copy)")

    def test_versions_are_listed_newest_first(self, admin, approved_source):
        user, _token = admin
        add(user, approved_source)
        add(user, approved_source, text=HYPERTENSION + "\n\nRevised.")
        versions = services.versions_of("who/hypertension")
        assert [v.version for v in versions] == [2, 1]

    def test_a_document_with_no_text_is_refused(self, admin, approved_source):
        user, _token = admin
        with pytest.raises(services.InvalidTransition):
            add(user, approved_source, text="   ")

    def test_unparseable_content_marks_the_document_failed(
            self, admin, approved_source):
        """The document records the failure rather than vanishing."""
        user, _token = admin
        with pytest.raises(services.IngestionFailed):
            add(user, approved_source, text="---\n---\n")
        document = Document.objects.filter(source="who/hypertension").first()
        assert document is not None
        assert document.status == Document.FAILED
        assert document.error_message

    def test_archive_and_restore(self, admin, approved_source):
        user, _token = admin
        document = add(user, approved_source)
        archived = services.archive_document(user, document.id)
        assert archived.status == Document.ARCHIVED
        assert archived.is_active is False

        restored = services.restore_document(user, document.id)
        assert restored.status == Document.PROCESSED
        assert restored.is_active is True

    def test_reindexing_replaces_chunks_rather_than_appending(
            self, admin, approved_source):
        user, _token = admin
        document = add(user, approved_source)
        before = document.chunks.count()
        services.reindex_document(user, document.id)
        document.refresh_from_db()
        assert document.chunks.count() == before

    def test_an_upload_is_validated_before_it_is_stored(self, client, admin,
                                                        approved_source):
        from django.core.files.uploadedfile import SimpleUploadedFile
        _user, token = admin
        as_user(client, token)
        res = client.post("/api/knowledge/documents/", {
            "source_id": approved_source.id, "identity": "who/binary",
            "title": "Not a document",
            "file": SimpleUploadedFile("notes.pdf", b"%PDF-1.7 binary",
                                       content_type="application/pdf"),
        }, format="multipart")
        assert res.status_code == 400
        assert "txt" in res.data["error"].lower()

    def test_a_text_upload_is_accepted_and_indexed(self, client, admin,
                                                   approved_source):
        from django.core.files.uploadedfile import SimpleUploadedFile
        _user, token = admin
        as_user(client, token)
        res = client.post("/api/knowledge/documents/", {
            "source_id": approved_source.id, "identity": "who/asthma",
            "title": "Asthma", "source_type": "markdown",
            "file": SimpleUploadedFile("asthma.md", ASTHMA.encode("utf-8"),
                                       content_type="text/markdown"),
        }, format="multipart")
        assert res.status_code == 201, res.data
        assert res.data["status"] == "processed"
        assert res.data["chunk_count"] >= 1

    def test_no_filesystem_path_is_ever_returned(self, client, admin,
                                                 approved_source):
        """Section 27: internal storage paths are not the client's business."""
        from django.core.files.uploadedfile import SimpleUploadedFile
        _user, token = admin
        as_user(client, token)
        created = client.post("/api/knowledge/documents/", {
            "source_id": approved_source.id, "identity": "who/paths",
            "title": "Paths", "source_type": "markdown",
            "file": SimpleUploadedFile("paths.md", ASTHMA.encode("utf-8"),
                                       content_type="text/markdown"),
        }, format="multipart")
        assert created.status_code == 201
        body = str(created.data)
        assert "file" not in created.data
        assert "knowledge/documents" not in body
        assert "media" not in body.lower()
        assert created.data["has_file"] is True


# ---------------------------------------------------------------------------
# The approval gate — retrieval safety
# ---------------------------------------------------------------------------
class TestRetrievalSafety:
    def test_a_pending_source_is_invisible_until_approved(self, admin):
        """The gate, made structural: nothing on the document changes."""
        user, _token = admin
        source = services.create_source(user, "Unreviewed Source")
        document = add(user, source, identity="pending/hypertension")
        assert document.status == Document.PROCESSED

        assert retrieval.search("symptoms of hypertension") == []

        services.review_source(user, source.id, KnowledgeSource.APPROVED)

        after = retrieval.search("symptoms of hypertension")
        assert after, "approving the source should make it retrievable"
        assert after[0]["provenance"]["source_name"] == "Unreviewed Source"

    def test_a_rejected_source_is_never_retrieved(self, admin):
        user, _token = admin
        source = services.create_source(user, "Rejected Source")
        add(user, source, identity="rejected/hypertension")
        services.review_source(user, source.id, KnowledgeSource.REJECTED,
                               "Not a medical authority.")
        assert retrieval.search("hypertension") == []

    def test_an_archived_source_is_never_retrieved(self, admin,
                                                   approved_source):
        user, _token = admin
        add(user, approved_source)
        assert retrieval.search("hypertension")
        services.review_source(user, approved_source.id,
                               KnowledgeSource.ARCHIVED)
        assert retrieval.search("hypertension") == []

    def test_an_archived_document_is_never_retrieved(self, admin,
                                                     approved_source):
        user, _token = admin
        document = add(user, approved_source)
        assert retrieval.search("hypertension")
        services.archive_document(user, document.id)
        assert retrieval.search("hypertension") == []

    def test_a_superseded_version_is_never_retrieved(self, admin,
                                                     approved_source):
        """Only the live version answers — superseded guidance cannot."""
        user, _token = admin
        first = add(user, approved_source)
        add(user, approved_source,
            text=HYPERTENSION.replace("silent condition",
                                      "SUPERSEDED-MARKER condition"))
        hits = retrieval.search("hypertension symptoms", top_k=10)
        assert hits
        assert all(h["provenance"]["document_version"] == 2 for h in hits)
        assert all(h["provenance"]["document_id"] != first.id for h in hits)

    def test_a_failed_document_is_never_retrieved(self, admin,
                                                  approved_source):
        user, _token = admin
        document = add(user, approved_source)
        document.status = Document.FAILED
        document.save(update_fields=["status"])
        assert retrieval.search("hypertension") == []

    def test_a_document_with_no_source_is_never_retrieved(self, admin):
        """Unattributed material has no verification status to trust."""
        from appointments.services import rag
        rag.ingest_text(HYPERTENSION, source="orphan/hypertension",
                        title="Orphan", source_type="markdown")
        assert Document.objects.filter(source="orphan/hypertension").exists()
        assert retrieval.search("hypertension") == []


# ---------------------------------------------------------------------------
# Section 35 — the realistic end-to-end test
# ---------------------------------------------------------------------------
class TestRealisticDocument:
    def test_upload_index_and_retrieve_with_full_provenance(
            self, client, admin, approved_source):
        user, token = admin
        document = add(user, approved_source)

        results = retrieval.search("What are common symptoms of hypertension?",
                                   top_k=3)
        assert results, "the indexed document should answer its own topic"

        best = results[0]
        # A score is returned, and it is a real number in cosine range.
        assert isinstance(best["score"], float)
        assert -1.0 <= best["score"] <= 1.0

        # The passage is the document's own text — nothing was generated.
        assert best["text"] in document.chunks.values_list("text", flat=True)

        # Full provenance: chunk -> document -> source.
        provenance = best["provenance"]
        assert provenance["source_name"] == "WHO Cardiovascular"
        assert provenance["source_organization"] == "World Health Organization"
        assert provenance["document_title"] == "Hypertension"
        assert provenance["document_version"] == 1
        assert provenance["verification_status"] == "approved"
        assert best["reference"].startswith("WHO Cardiovascular")

        # Section metadata survived chunking. There is no page number, and the
        # payload says so honestly rather than inventing one — Markdown has no
        # pages.
        assert any(r["section"] for r in results)
        assert best["page"] is None

        # And the same thing through the API.
        as_user(client, token)
        res = client.get("/api/knowledge/search/",
                         {"q": "symptoms of hypertension", "top_k": 3})
        assert res.status_code == 200, res.data
        assert res.data["count"] >= 1
        assert res.data["results"][0]["provenance"]["source_name"] == \
            "WHO Cardiovascular"

    def test_filtering_by_specialty_and_language(self, admin,
                                                 approved_source):
        user, _token = admin
        add(user, approved_source)
        assert retrieval.search("hypertension", specialty="cardiology")
        assert retrieval.search("hypertension", specialty="dermatology") == []
        assert retrieval.search("hypertension", language="en")
        assert retrieval.search("hypertension", language="fr") == []

    def test_filtering_by_source_and_document(self, admin, approved_source):
        user, _token = admin
        document = add(user, approved_source)
        assert retrieval.search("hypertension", source=approved_source.id)
        assert retrieval.search("hypertension", document=document.id)
        assert retrieval.search("hypertension", document=document.id + 999) == []

    def test_an_empty_query_returns_nothing_rather_than_everything(self):
        assert retrieval.search("   ") == []


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
class TestSecurity:
    @pytest.mark.parametrize("role,username", [
        (roles.PATIENT, "kb_patient"), (roles.DOCTOR, "kb_doctor"),
        (roles.LABORATORY, "kb_lab"), (roles.RADIOLOGY, "kb_centre"),
        (roles.PHARMACY, "kb_pharmacy"),
    ])
    def test_no_clinical_role_can_touch_the_knowledge_base(
            self, client, admin, approved_source, role, username):
        user, _token = admin
        document = add(user, approved_source)
        _actor, token = make(role, username)
        as_user(client, token)

        for path in ("/api/knowledge/sources/", "/api/knowledge/documents/",
                     "/api/knowledge/index/", "/api/knowledge/vocabulary/",
                     "/api/knowledge/search/?q=hypertension"):
            assert client.get(path).status_code == 403, path

        assert client.post("/api/knowledge/sources/", {"name": "Mine"},
                           format="json").status_code == 403
        assert client.post(
            f"/api/knowledge/sources/{approved_source.id}/review/",
            {"status": "approved"}, format="json").status_code == 403
        assert client.post(
            f"/api/knowledge/documents/{document.id}/reindex/", {},
            format="json").status_code == 403
        assert client.post(
            f"/api/knowledge/documents/{document.id}/archive/", {},
            format="json").status_code == 403

    def test_the_service_layer_refuses_too_not_just_the_view(
            self, approved_source):
        """A second view that forgot the permission class still gets nothing."""
        patient, _token = make(roles.PATIENT, "kb_direct_patient")
        assert services.may_manage(patient) is False
        with pytest.raises(services.NotAuthorized):
            services.create_source(patient, "Patient's own source")
        with pytest.raises(services.NotAuthorized):
            services.review_source(patient, approved_source.id, "approved")

    def test_a_doctor_cannot_modify_the_knowledge_base(self, approved_source):
        doctor, _token = make(roles.DOCTOR, "kb_direct_doctor")
        with pytest.raises(services.NotAuthorized):
            services.add_document(doctor, approved_source.id,
                                  identity="doc/own", title="Mine",
                                  text=ASTHMA)

    def test_an_anonymous_caller_gets_nothing(self, client):
        client.credentials()
        for path in ("/api/knowledge/sources/", "/api/knowledge/documents/",
                     "/api/knowledge/search/?q=x", "/api/knowledge/index/"):
            assert client.get(path).status_code == 401, path

    def test_a_failure_message_carries_no_credential(self):
        """Section 20/29: provider errors can quote headers."""
        message = services.safe_error(
            RuntimeError("401 from https://api.example.com "
                         "Authorization: Bearer sk-abcdef0123456789abcdef"))
        assert "sk-abcdef" not in message
        assert "[redacted]" in message
        assert len(message) <= services.MAX_ERROR_CHARS


# ---------------------------------------------------------------------------
# API shape, diagnostics and performance
# ---------------------------------------------------------------------------
class TestApiAndPerformance:
    def test_the_vocabulary_reports_what_can_actually_be_parsed(
            self, client, admin):
        _user, token = admin
        as_user(client, token)
        res = client.get("/api/knowledge/vocabulary/")
        assert res.status_code == 200
        # Honest about the absence of PDF extraction.
        assert set(res.data["supported_uploads"]) == {".txt", ".text", ".md",
                                                      ".markdown"}

    def test_index_status_reports_real_figures(self, client, admin,
                                               approved_source):
        user, token = admin
        add(user, approved_source)
        as_user(client, token)
        res = client.get("/api/knowledge/index/")
        assert res.status_code == 200
        assert res.data["documents"] == 1
        assert res.data["retrievable_documents"] == 1
        assert res.data["chunks"] >= 2
        assert res.data["embedding"]["embedder"] == "hashing"
        assert res.data["sources_by_status"]["approved"] == 1

    def test_index_status_distinguishes_indexed_from_retrievable(
            self, client, admin):
        user, token = admin
        source = services.create_source(user, "Pending Source")
        add(user, source, identity="pending/doc")
        as_user(client, token)
        res = client.get("/api/knowledge/index/")
        assert res.data["documents"] == 1
        # Indexed, but not allowed to answer.
        assert res.data["retrievable_documents"] == 0
        assert res.data["chunks"] >= 1

    def test_document_listing_paginates(self, client, admin, approved_source):
        user, token = admin
        for index in range(6):
            add(user, approved_source, text=f"# Doc {index}\n\n" + ASTHMA,
                identity=f"who/doc-{index}", title=f"Doc {index}")
        as_user(client, token)
        page = client.get("/api/knowledge/documents/?limit=3").data
        assert page["count"] == 3 and page["has_more"] is True
        second = client.get("/api/knowledge/documents/?limit=3&offset=3").data
        assert not ({d["id"] for d in page["results"]}
                    & {d["id"] for d in second["results"]})

    def test_listing_does_not_scale_with_document_count(
            self, client, admin, approved_source, django_assert_max_num_queries):
        user, token = admin
        for index in range(6):
            add(user, approved_source, text=f"# Doc {index}\n\n" + ASTHMA,
                identity=f"who/perf-{index}", title=f"Doc {index}")
        as_user(client, token)
        with django_assert_max_num_queries(8):
            res = client.get("/api/knowledge/documents/?limit=25")
        assert len(res.data["results"]) == 6
        assert all(d["chunk_count"] >= 1 for d in res.data["results"])

    def test_source_listing_does_not_scale_with_source_count(
            self, client, admin, django_assert_max_num_queries):
        user, token = admin
        for index in range(6):
            services.create_source(user, f"Source {index}")
        as_user(client, token)
        with django_assert_max_num_queries(8):
            res = client.get("/api/knowledge/sources/")
        assert len(res.data) == 6

    def test_a_space_mismatch_is_reported_as_unavailable_not_empty(
            self, admin, approved_source, monkeypatch):
        """"Everything we have is unreadable" is not "we have nothing"."""
        from appointments.services.rag import embeddings
        user, _token = admin
        add(user, approved_source)

        monkeypatch.setenv("RAG_HASHING_DIMENSION", "512")
        embeddings.reset()
        with pytest.raises(retrieval.RetrievalUnavailable):
            retrieval.search("hypertension")


# ---------------------------------------------------------------------------
# Nothing else broke
# ---------------------------------------------------------------------------
class TestExistingModulesStillWork:
    def test_the_rag_engine_still_ingests_without_a_knowledge_source(self):
        """The engine keeps working on its own — this module is a layer over
        it, not a replacement for it."""
        from appointments.services import rag
        result = rag.ingest_text(ASTHMA, source="plain/asthma", title="Asthma",
                                 source_type="markdown")
        assert result.chunks >= 1
        assert result.document.knowledge_source is None

    def test_ungated_engine_search_is_unchanged(self, admin):
        """``rag.search`` stays policy-free; the gate lives in this module."""
        from appointments.services import rag
        user, _token = admin
        source = services.create_source(user, "Still Pending")
        add(user, source, identity="pending/engine")
        # The engine finds it…
        assert rag.search("hypertension")
        # …and the Knowledge Base still refuses to.
        assert retrieval.search("hypertension") == []

    def test_appointments_still_answer(self, client, admin):
        _user, token = admin
        as_user(client, token)
        assert client.get("/api/dashboard/summary/").status_code == 200

    def test_the_medical_record_is_untouched(self, client):
        patient, token = make(roles.PATIENT, "kb_record_patient")
        as_user(client, token)
        res = client.get("/api/records/me/")
        assert res.status_code == 200
        assert "knowledge" not in str(res.data).lower()
