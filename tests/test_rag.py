"""TASK 05 — RAG infrastructure.

Every test here runs with **no LLM involved**. Retrieval is verified on its own:
if these pass, the corpus, the embeddings and the search work regardless of
whether any provider is reachable.
"""
import numpy as np
import pytest

from appointments.models import Document, DocumentChunk
from appointments.services import rag
from appointments.services.rag import (
    chunking, cleaning, embeddings, ingest, parsing, store,
)
from appointments.services.rag.embeddings.hashing import HashingEmbedder

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _offline_embedder(monkeypatch):
    """Force the offline embedder: hermetic, and never bills for a test run."""
    monkeypatch.setenv("RAG_EMBEDDER", "hashing")
    monkeypatch.delenv("RAG_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("RAG_EMBEDDING_DIMENSION", raising=False)
    embeddings.reset()
    yield
    embeddings.reset()


# --- corpus fixtures: inline strings, never files on disk -------------------
HYPERTENSION = """# Hypertension

## Diagnosis
Blood pressure is measured in millimetres of mercury. A reading at or above
140/90 on two separate occasions is the usual threshold for a hypertension
diagnosis in adults.

## Treatment
First-line management is lifestyle change: reducing dietary salt, losing excess
weight, and regular aerobic exercise. Where medication is needed the usual
starting classes are ACE inhibitors and calcium channel blockers.
"""

DIABETES = """# Diabetes

## Monitoring
HbA1c reflects average blood glucose over roughly three months. A result at or
above 48 mmol/mol indicates diabetes.

## Diet
Carbohydrate awareness matters more than avoidance. Wholegrain foods release
glucose more slowly than refined flour and white rice.
"""

NUTRITION = """Fibre and cholesterol

Soluble fibre from oats, beans and lentils binds cholesterol in the gut and
lowers circulating LDL. Most adults eat well under the recommended thirty grams
of fibre a day.
"""


def _ingest(text, source, **kwargs):
    return ingest.ingest_text(text, source=source, **kwargs)


@pytest.fixture
def corpus():
    return [
        _ingest(HYPERTENSION, "hypertension", source_type="markdown",
                metadata={"audience": "clinician", "language": "en"}),
        _ingest(DIABETES, "diabetes", source_type="markdown",
                metadata={"audience": "patient", "language": "en"}),
        _ingest(NUTRITION, "nutrition", metadata={"audience": "patient",
                                                  "language": "en"}),
    ]


# ===========================================================================
# Stage 1 — parsing
# ===========================================================================
class TestParsing:
    def test_markdown_title_comes_from_the_first_heading(self):
        parsed = parsing.parse_text(HYPERTENSION, source_type="markdown")
        assert parsed.title == "Hypertension"

    def test_frontmatter_becomes_metadata(self):
        parsed = parsing.parse_text(
            "---\ntitle: Custom\nspeciality: cardiology\n---\nBody text here.",
            source_type="markdown")
        assert parsed.metadata["speciality"] == "cardiology"
        assert parsed.title == "Custom"
        assert "---" not in parsed.text

    def test_an_unsupported_extension_is_refused(self, tmp_path):
        path = tmp_path / "scan.pdf"
        path.write_bytes(b"%PDF-1.4")
        with pytest.raises(parsing.UnsupportedDocument, match="not"):
            parsing.parse_file(path)

    def test_an_empty_document_is_refused(self):
        with pytest.raises(parsing.UnsupportedDocument, match="empty"):
            parsing.parse_text("   \n\n  ")

    def test_non_utf8_bytes_are_decoded_rather_than_crashing(self):
        parsed = parsing.parse_text("ضغط الدم مرتفع".encode("cp1256"))
        assert "الدم" in parsed.text


# ===========================================================================
# Stage 2 — cleaning
# ===========================================================================
class TestCleaning:
    def test_whitespace_is_normalised(self):
        assert cleaning.clean("a  b\r\n\n\n\nc  ") == "a b\n\nc"

    def test_arabic_diacritics_and_tatweel_are_stripped(self):
        """Decorative marks must not fork one word into several index terms."""
        assert cleaning.clean("مَرِيض") == cleaning.clean("مريض")
        assert cleaning.clean("مريـــض") == "مريض"

    def test_invisible_characters_are_removed(self):
        assert cleaning.clean("blood​pressure") == "bloodpressure"

    def test_horizontal_rules_are_dropped(self):
        assert "---" not in cleaning.clean("Heading\n\n---\n\nBody")

    def test_the_checksum_is_stable_and_content_sensitive(self):
        assert cleaning.checksum("abc") == cleaning.checksum("abc")
        assert cleaning.checksum("abc") != cleaning.checksum("abd")


# ===========================================================================
# Stage 3 — chunking
# ===========================================================================
class TestChunking:
    def test_headings_become_section_metadata(self):
        chunks = chunking.chunk_document(cleaning.clean(HYPERTENSION),
                                         source_type="markdown")
        sections = {c.section for c in chunks}
        assert "Hypertension > Treatment" in sections
        assert "Hypertension > Diagnosis" in sections

    def test_chunks_are_ordered_without_gaps(self):
        chunks = chunking.chunk_document(cleaning.clean(HYPERTENSION),
                                         source_type="markdown")
        assert [c.ordinal for c in chunks] == list(range(len(chunks)))

    def test_long_prose_is_split_with_overlap(self):
        text = " ".join(f"Sentence number {i} about blood pressure." for i in range(200))
        chunks = chunking.chunk_document(text, size=400, overlap=80)
        assert len(chunks) > 1
        assert all(len(c.text) <= 500 for c in chunks)
        # The overlap must actually carry words across the boundary.
        first_tail = set(chunks[0].text.split()[-6:])
        assert first_tail & set(chunks[1].text.split())

    def test_prose_with_no_breaks_still_splits(self):
        """Unbroken text (common in Arabic paragraphs) must not become one
        enormous chunk that dilutes every embedding."""
        chunks = chunking.chunk_document("لا" * 3000, size=300, overlap=50)
        assert len(chunks) > 1

    def test_tiny_fragments_are_merged_not_stored_alone(self):
        chunks = chunking.chunk_document("Tiny.\n\n" + "x" * 400, size=900)
        assert len(chunks) == 1

    def test_overlap_must_be_smaller_than_the_chunk(self):
        with pytest.raises(ValueError):
            chunking.chunk_document("text", size=100, overlap=100)


# ===========================================================================
# Stage 4 — embeddings
# ===========================================================================
class TestEmbeddings:
    def test_vectors_are_unit_normalised(self):
        matrix = HashingEmbedder().embed_documents(["blood pressure", "diabetes"])
        assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)

    def test_embedding_is_deterministic(self):
        first = HashingEmbedder().embed_documents(["chest pain"])
        second = HashingEmbedder().embed_documents(["chest pain"])
        assert np.allclose(first, second)

    def test_related_text_scores_above_unrelated(self):
        embedder = HashingEmbedder()
        query = embedder.embed_query("how do I lower blood pressure?")
        related = embedder.embed_query("reducing dietary salt lowers blood pressure")
        unrelated = embedder.embed_query("wholegrain rice releases glucose slowly")
        assert float(related @ query) > float(unrelated @ query)

    def test_stopwords_are_not_features(self):
        """Measured, not assumed. With function words in the space every query
        matched every passage — an unrelated question scored the same as a
        genuine one. Removing them is what makes 'no match' mean no match."""
        embedder = HashingEmbedder()
        a = embedder.embed_query("how should the patient be treated")
        b = embedder.embed_query("what are the usual options for it")
        assert float(a @ b) == pytest.approx(0.0, abs=1e-6)

    def test_unrelated_text_scores_zero_not_merely_low(self):
        embedder = HashingEmbedder()
        corpus_text = embedder.embed_query(
            "soluble fibre from oats binds cholesterol in the gut")
        unrelated = embedder.embed_query("quarterly shipping logistics tariffs")
        assert float(corpus_text @ unrelated) == pytest.approx(0.0, abs=1e-6)

    def test_lexical_matching_does_not_understand_synonyms(self):
        """The documented limit of the offline embedder, asserted so it stays
        visible: it matches shared words, not meaning. Semantic retrieval needs
        the API embedder — this is why `describe()['semantic']` is False."""
        embedder = HashingEmbedder()
        query = embedder.embed_query("hypertension")
        synonym = embedder.embed_query("high blood pressure")
        assert float(query @ synonym) == pytest.approx(0.0, abs=1e-6)

    def test_an_empty_batch_returns_the_right_shape(self):
        matrix = HashingEmbedder().embed_documents([])
        assert matrix.shape == (0, HashingEmbedder().dimension)

    def test_the_space_identifies_embedder_model_and_width(self):
        space = HashingEmbedder().space
        assert space.embedder == "hashing"
        assert space.dimension == HashingEmbedder().dimension
        assert str(space).startswith("hashing:")

    def test_explicit_selection_wins(self, monkeypatch):
        monkeypatch.setenv("RAG_EMBEDDER", "hashing")
        embeddings.reset()
        assert embeddings.selected_name() == "hashing"

    def test_aliases_resolve(self):
        assert embeddings.canonical("offline") == "hashing"
        assert embeddings.canonical("openai") == "api"
        assert embeddings.canonical("local") == "api"

    def test_an_unknown_embedder_is_rejected(self, monkeypatch):
        monkeypatch.setenv("RAG_EMBEDDER", "wizard")
        embeddings.reset()
        with pytest.raises(embeddings.EmbeddingError, match="Unknown"):
            embeddings.selected_name()

    def test_selecting_an_unconfigured_embedder_says_so(self, monkeypatch):
        monkeypatch.setenv("RAG_EMBEDDER", "api")
        monkeypatch.delenv("RAG_EMBEDDING_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "")
        embeddings.reset()
        with pytest.raises(embeddings.EmbedderNotConfigured):
            embeddings.selected_name()

    def test_auto_falls_back_to_offline_without_keys(self, monkeypatch):
        monkeypatch.setenv("RAG_EMBEDDER", "auto")
        monkeypatch.delenv("RAG_EMBEDDING_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "")
        embeddings.reset()
        assert embeddings.selected_name() == "hashing"

    def test_a_placeholder_key_does_not_count_as_configured(self, monkeypatch):
        monkeypatch.setenv("RAG_EMBEDDER", "auto")
        monkeypatch.setenv("OPENAI_API_KEY", "your-openai-api-key-here")
        embeddings.reset()
        assert embeddings.selected_name() == "hashing"


class TestAPIEmbedder:
    """The semantic embedder, with HTTP mocked — a real key is not available
    in this deployment, so the contract is verified rather than the network."""

    def _body(self, vectors):
        return {"data": [{"index": i, "embedding": v}
                         for i, v in enumerate(vectors)]}

    def test_vectors_are_requested_and_normalised(self, monkeypatch):
        from appointments.services.rag.embeddings import api

        sent = {}

        def fake_post(url, *, headers, payload, timeout, provider, model):
            sent.update(url=url, payload=payload, headers=headers)
            return self._body([[3.0, 4.0], [0.0, 5.0]])

        monkeypatch.setenv("RAG_EMBEDDING_API_KEY", "sk-real-looking-key")
        monkeypatch.setattr(api.http, "post_json", fake_post)

        matrix = api.APIEmbedder(dimension=2).embed_documents(["a", "b"])
        assert sent["url"].endswith("/embeddings")
        assert sent["payload"]["input"] == ["a", "b"]
        assert sent["headers"]["Authorization"] == "Bearer sk-real-looking-key"
        assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0)

    def test_out_of_order_results_are_realigned(self, monkeypatch):
        """The spec does not guarantee order; misaligning vectors with their
        texts would corrupt the whole index silently."""
        from appointments.services.rag.embeddings import api

        def fake_post(url, **kwargs):
            return {"data": [{"index": 1, "embedding": [0.0, 1.0]},
                             {"index": 0, "embedding": [1.0, 0.0]}]}

        monkeypatch.setenv("RAG_EMBEDDING_API_KEY", "sk-real-looking-key")
        monkeypatch.setattr(api.http, "post_json", fake_post)
        matrix = api.APIEmbedder(dimension=2).embed_documents(["first", "second"])
        assert np.allclose(matrix[0], [1.0, 0.0])

    def test_a_dimension_mismatch_fails_at_ingestion(self, monkeypatch):
        from appointments.services.rag.embeddings import api

        monkeypatch.setenv("RAG_EMBEDDING_API_KEY", "sk-real-looking-key")
        monkeypatch.setattr(api.http, "post_json",
                            lambda url, **kw: self._body([[1.0, 0.0, 0.0]]))
        with pytest.raises(api.EmbeddingError, match="reindex"):
            api.APIEmbedder(dimension=2).embed_documents(["a"])

    def test_a_short_response_is_rejected(self, monkeypatch):
        from appointments.services.rag.embeddings import api

        monkeypatch.setenv("RAG_EMBEDDING_API_KEY", "sk-real-looking-key")
        monkeypatch.setattr(api.http, "post_json",
                            lambda url, **kw: self._body([[1.0, 0.0]]))
        with pytest.raises(api.EmbeddingError, match="vectors for"):
            api.APIEmbedder(dimension=2).embed_documents(["a", "b"])

    def test_provider_errors_are_translated(self, monkeypatch):
        from appointments.services.ai.providers.base import RateLimited
        from appointments.services.rag.embeddings import api

        def boom(url, **kwargs):
            raise RateLimited("embeddings rate limited the request (429)")

        monkeypatch.setenv("RAG_EMBEDDING_API_KEY", "sk-real-looking-key")
        monkeypatch.setattr(api.http, "post_json", boom)
        with pytest.raises(api.EmbeddingError, match="429"):
            api.APIEmbedder(dimension=2).embed_documents(["a"])

    def test_no_credential_is_reported_before_any_request(self, monkeypatch):
        from appointments.services.rag.embeddings import api

        monkeypatch.delenv("RAG_EMBEDDING_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "")
        with pytest.raises(api.EmbedderNotConfigured, match="RAG_EMBEDDING_API_KEY"):
            api.APIEmbedder(dimension=2).embed_documents(["a"])


# ===========================================================================
# Stage 5-6 — ingestion into the vector store
# ===========================================================================
class TestIngestion:
    def test_a_document_and_its_chunks_are_stored(self):
        result = _ingest(HYPERTENSION, "hypertension", source_type="markdown")
        assert result.chunks > 0
        assert Document.objects.count() == 1
        assert DocumentChunk.objects.count() == result.chunks

    def test_every_chunk_is_stamped_with_its_embedding_space(self):
        _ingest(HYPERTENSION, "hypertension", source_type="markdown")
        space = HashingEmbedder().space
        for chunk in DocumentChunk.objects.all():
            assert chunk.embedder == space.embedder
            assert chunk.embedding_model == space.model
            assert chunk.dimension == space.dimension

    def test_stored_vectors_round_trip_unit_normalised(self):
        _ingest(NUTRITION, "nutrition")
        for chunk in DocumentChunk.objects.all():
            vector = store.from_bytes(chunk.embedding)
            assert vector.shape == (chunk.dimension,)
            assert pytest.approx(float(np.linalg.norm(vector)), abs=1e-4) == 1.0

    def test_reingesting_unchanged_content_is_skipped(self):
        _ingest(DIABETES, "diabetes", source_type="markdown")
        again = _ingest(DIABETES, "diabetes", source_type="markdown")
        assert again.skipped is True

    def test_force_reindexes_unchanged_content(self):
        _ingest(DIABETES, "diabetes", source_type="markdown")
        again = _ingest(DIABETES, "diabetes", source_type="markdown", force=True)
        assert again.skipped is False

    def test_editing_a_document_replaces_its_chunks(self):
        """Stale guidance that still answers queries is worse than none."""
        _ingest(DIABETES, "diabetes", source_type="markdown")
        _ingest("# Diabetes\n\nCompletely different content about insulin pumps.",
                source="diabetes", source_type="markdown")
        texts = " ".join(DocumentChunk.objects.values_list("text", flat=True))
        assert "insulin pumps" in texts
        assert "HbA1c" not in texts
        assert Document.objects.count() == 1

    def test_document_metadata_is_carried_onto_chunks(self):
        _ingest(NUTRITION, "nutrition", metadata={"audience": "patient"})
        assert all(c.metadata.get("audience") == "patient"
                   for c in DocumentChunk.objects.all())

    def test_a_directory_is_ingested_and_bad_files_do_not_abort_it(self, tmp_path):
        (tmp_path / "one.txt").write_text(NUTRITION, encoding="utf-8")
        (tmp_path / "two.txt").write_text(DIABETES, encoding="utf-8")
        (tmp_path / "empty.txt").write_text("   ", encoding="utf-8")
        (tmp_path / "ignored.pdf").write_bytes(b"%PDF")

        results, failures = ingest.ingest_directory(tmp_path)
        assert len(results) == 2
        assert len(failures) == 1          # the empty file, not the pdf
        assert Document.objects.count() == 2

    def test_removing_a_document_removes_its_chunks(self):
        _ingest(NUTRITION, "nutrition")
        assert ingest.remove("nutrition") is True
        assert DocumentChunk.objects.count() == 0

    def test_misaligned_vectors_are_refused(self):
        document = Document.objects.create(title="t", source="s")
        chunk = chunking.Chunk(text="a", ordinal=0)
        with pytest.raises(ValueError, match="misaligned"):
            store.replace_chunks(document, [chunk], np.zeros((2, 4)),
                                 HashingEmbedder().space)


# ===========================================================================
# Stage 7 — semantic search
# ===========================================================================
class TestSearch:
    def test_the_most_relevant_passage_ranks_first(self, corpus):
        hits = rag.search("when is hypertension diagnosed?")
        assert hits
        assert hits[0].document.source == "hypertension"
        assert "Diagnosis" in hits[0].chunk.section

    def test_a_different_question_retrieves_a_different_document(self, corpus):
        hits = rag.search("soluble fibre and circulating LDL")
        assert hits[0].document.source == "nutrition"

    def test_a_question_retrieves_the_right_section_of_the_right_document(self, corpus):
        hits = rag.search("what does HbA1c measure?")
        assert hits[0].document.source == "diabetes"
        assert "Monitoring" in hits[0].chunk.section

    def test_results_are_ordered_by_descending_score(self, corpus):
        hits = rag.search("blood pressure", top_k=5)
        assert [h.score for h in hits] == sorted((h.score for h in hits),
                                                 reverse=True)

    def test_top_k_is_respected(self, corpus):
        assert len(rag.search("blood pressure diabetes fibre", top_k=2)) <= 2

    def test_scores_are_bounded_cosine_similarities(self, corpus):
        for hit in rag.search("blood pressure", top_k=5):
            assert -1.0 <= hit.score <= 1.0

    def test_an_unrelated_query_returns_nothing(self, corpus):
        assert rag.search("quarterly shipping logistics tariffs") == []

    def test_an_empty_query_returns_nothing_without_touching_the_index(self):
        assert rag.search("   ") == []

    def test_searching_an_empty_corpus_is_not_an_error(self):
        assert rag.search("anything at all") == []

    def test_min_score_filters_weak_matches(self, corpus):
        assert rag.search("blood pressure", min_score=0.99) == []


# ===========================================================================
# Metadata filtering — applied before scoring
# ===========================================================================
class TestMetadataFiltering:
    def test_filtering_by_chunk_metadata(self, corpus):
        hits = rag.search("wholegrain foods and glucose",
                          metadata={"audience": "patient"})
        assert hits
        assert all(h.chunk.metadata["audience"] == "patient" for h in hits)

    def test_filtering_excludes_the_otherwise_best_match(self, corpus):
        """Proof the filter is applied before scoring, not after."""
        unfiltered = rag.search("when is hypertension diagnosed?")
        assert unfiltered[0].document.source == "hypertension"

        filtered = rag.search("when is hypertension diagnosed?",
                              metadata={"audience": "patient"})
        assert all(h.document.source != "hypertension" for h in filtered)

    def test_filtering_by_document(self, corpus):
        target = Document.objects.get(source="diabetes")
        hits = rag.search("glucose", documents=[target.id])
        assert hits
        assert all(h.document_id == target.id for h in hits)

    def test_filtering_by_source_type(self, corpus):
        hits = rag.search("blood pressure diabetes", source_type="markdown")
        assert all(h.document.source_type == "markdown" for h in hits)

    def test_filtering_by_section(self, corpus):
        hits = rag.search("blood pressure millimetres", section="Diagnosis")
        assert hits
        assert all("Diagnosis" in h.chunk.section for h in hits)

    def test_filtering_by_document_metadata(self, corpus):
        hits = rag.search("glucose", document_metadata={"audience": "patient"})
        assert all(h.document.metadata["audience"] == "patient" for h in hits)

    def test_a_filter_matching_nothing_returns_nothing(self, corpus):
        assert rag.search("blood pressure", metadata={"audience": "martian"}) == []


# ===========================================================================
# Embedding-space safety
# ===========================================================================
class TestEmbeddingSpaceSafety:
    def test_a_corpus_in_another_space_demands_a_reindex(self, corpus, monkeypatch):
        """The silent RAG failure: querying an index built by a different
        embedder. It must be loud, and distinguishable from 'no results'."""
        monkeypatch.setenv("RAG_HASHING_DIMENSION", "512")
        embeddings.reset()
        with pytest.raises(store.IndexSpaceMismatch, match="[Rr]e-ingest"):
            rag.search("blood pressure")

    def test_the_error_names_each_space_once(self, corpus, monkeypatch):
        """Found live: Meta.ordering columns leaked into the DISTINCT, so the
        message repeated the same space once per stored chunk."""
        monkeypatch.setenv("RAG_HASHING_DIMENSION", "512")
        embeddings.reset()
        with pytest.raises(store.IndexSpaceMismatch) as caught:
            rag.search("blood pressure")
        assert str(caught.value).count("hashing-v3-2048") == 1

    def test_the_error_names_both_spaces(self, corpus, monkeypatch):
        monkeypatch.setenv("RAG_HASHING_DIMENSION", "512")
        embeddings.reset()
        with pytest.raises(store.IndexSpaceMismatch) as caught:
            rag.search("blood pressure")
        assert "2048" in str(caught.value) and "512" in str(caught.value)

    def test_search_only_sees_its_own_space(self, corpus, monkeypatch):
        """Two spaces can coexist; each query sees exactly one."""
        monkeypatch.setenv("RAG_HASHING_DIMENSION", "512")
        embeddings.reset()
        _ingest(HYPERTENSION, "hypertension", source_type="markdown", force=True)

        hits = rag.search("when is hypertension diagnosed?")
        assert hits
        assert all(h.chunk.dimension == 512 for h in hits)

        monkeypatch.setenv("RAG_HASHING_DIMENSION", "2048")
        embeddings.reset()
        assert all(h.chunk.dimension == 2048
                   for h in rag.search("when is hypertension diagnosed?"))

    def test_an_empty_corpus_never_reports_a_mismatch(self, monkeypatch):
        monkeypatch.setenv("RAG_HASHING_DIMENSION", "512")
        embeddings.reset()
        assert rag.search("anything") == []


# ===========================================================================
# Stage 8 — context retrieval and source references
# ===========================================================================
class TestContextRetrieval:
    def test_context_is_numbered_and_carries_matching_sources(self, corpus):
        result = rag.retrieve_context("when is hypertension diagnosed?", top_k=2)
        assert result.found
        assert result.text.startswith("[1]")
        assert [s["n"] for s in result.sources] == list(
            range(1, len(result.sources) + 1))

    def test_every_source_is_traceable_back_to_a_real_passage(self, corpus):
        result = rag.retrieve_context("when is hypertension diagnosed?", top_k=3)
        for source in result.sources:
            chunk = DocumentChunk.objects.get(pk=source["chunk_id"])
            assert chunk.document.title == source["document"]
            assert chunk.section == source["section"]

    def test_the_reference_reads_as_a_citation(self, corpus):
        result = rag.retrieve_context("when is hypertension diagnosed?", top_k=1)
        assert result.sources[0]["reference"] == "Hypertension › Hypertension > Diagnosis"

    def test_no_match_yields_empty_context_not_an_invented_one(self, corpus):
        result = rag.retrieve_context("quarterly shipping logistics tariffs")
        assert result.found is False
        assert result.text == ""
        assert result.sources == []

    def test_context_respects_its_character_budget(self, corpus):
        result = rag.retrieve_context("blood pressure", top_k=5, max_chars=200)
        assert len(result.text) <= 200

    def test_sources_describe_only_what_was_actually_sent(self, corpus):
        """A citation must never point at a passage the model never saw."""
        result = rag.retrieve_context("blood pressure", top_k=5, max_chars=200)
        assert len(result.sources) == len(result.hits)
        for source in result.sources:
            assert f"[{source['n']}]" in result.text

    def test_passages_are_included_whole_or_not_at_all(self, corpus):
        result = rag.retrieve_context("blood pressure", top_k=5, max_chars=250)
        for hit in result.hits:
            assert hit.chunk.text.strip() in result.text

    def test_truncation_is_reported(self, corpus):
        result = rag.retrieve_context("blood pressure", top_k=5, max_chars=200)
        assert result.truncated is True

    def test_filters_reach_through_to_retrieval(self, corpus):
        result = rag.retrieve_context("wholegrain foods and glucose",
                                      metadata={"audience": "patient"})
        assert result.found
        assert all("Hypertension" not in s["document"] for s in result.sources)


# ===========================================================================
# The pipeline does not touch the LLM
# ===========================================================================
class TestIndependenceFromTheLLM:
    def test_retrieval_works_with_every_llm_provider_removed(self, corpus,
                                                             monkeypatch):
        for name in ("GROQ_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.setenv(name, "")
        monkeypatch.setenv("AI_PROVIDER", "auto")

        from appointments.services.ai import llm
        assert llm.active_provider() is None, "precondition: no provider"

        result = rag.retrieve_context("when is hypertension diagnosed?")
        assert result.found

    def test_the_assistant_pipeline_is_not_wired_to_rag_yet(self):
        """Grounding the assistant is Task 06. This asserts the seam is still
        open, so that task starts from a known state."""
        import inspect

        from appointments.services.ai import pipeline
        source = inspect.getsource(pipeline)
        assert "services.rag" not in source
        assert "import rag" not in source
        assert "retrieve_context" not in source


# ===========================================================================
# Diagnostics
# ===========================================================================
class TestStats:
    def test_stats_report_documents_chunks_and_spaces(self, corpus):
        summary = rag.stats()
        assert summary["documents"] == 3
        assert summary["chunks"] > 0
        assert summary["spaces"][0]["embedder"] == "hashing"

    def test_describe_flags_offline_embeddings_as_non_semantic(self):
        info = rag.embedder_info()
        assert info["embedder"] == "hashing"
        assert info["semantic"] is False


# ===========================================================================
# The management command
# ===========================================================================
class TestManagementCommand:
    def _run(self, *args, **kwargs):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("rag_ingest", *args, stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_ingesting_a_file(self, tmp_path):
        path = tmp_path / "nutrition.txt"
        path.write_text(NUTRITION, encoding="utf-8")
        output = self._run(str(path))
        assert "1 document(s) indexed" in output
        assert DocumentChunk.objects.exists()

    def test_searching_from_the_command_line(self, corpus):
        output = self._run(search="when is hypertension diagnosed?")
        assert "Diagnosis" in output
        assert "[1]" in output

    def test_stats(self, corpus):
        assert "documents: 3" in self._run(stats=True)

    def test_offline_embeddings_are_flagged_to_the_operator(self, corpus):
        assert "lexical" in self._run(stats=True)

    def test_a_missing_path_is_an_error(self):
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match="No such path"):
            self._run("/nonexistent/corpus")

    def test_removing_a_document(self, corpus):
        assert "Removed" in self._run(remove="nutrition")
        assert not Document.objects.filter(source="nutrition").exists()
