"""RAG infrastructure: grounded answering over the governed corpus.

The suite is organised around the four things that would actually hurt if they
were wrong:

* **Nothing is answered from nothing.** With no relevant context the model is
  never called — enforced by a fake that raises if it is.
* **Citations are real.** A model citing a source it was never given is caught
  and reported, not quietly cleaned up.
* **The gate still holds.** Unapproved, archived and superseded material stays
  out of the context, so it cannot reach an answer.
* **No patient data.** The pipeline reads general reference material only.

Generation is exercised with a fake LLM — no key exists in this environment and
none is needed. The fakes are shaped like the real adapter response, so the
pipeline under test is the real one end to end apart from the provider call.
"""
import pytest
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts import roles
from accounts.models import UserAccount
from accounts.services import register_account
from appointments.services.ai.providers.base import ChatResponse
from knowledge import rag, services
from knowledge.models import KnowledgeSource
from knowledge.rag import config, context as context_builder, evaluation
from knowledge.rag import query as query_module
from knowledge.rag import service as rag_service

pytestmark = pytest.mark.django_db

PW = "Str0ng!Passw0rd"

HYPERTENSION = """# Hypertension

High blood pressure, or hypertension, is a long-term condition in which the
force of the blood against the artery walls stays elevated over time.

## Symptoms

Hypertension is often called a silent condition because most people have no
symptoms at all. When symptoms do occur they may include headaches, shortness
of breath, nosebleeds, flushing and visual changes.

## Measurement

A diagnosis is not made from a single reading. Blood pressure is measured on
more than one occasion, at rest, with a cuff of the correct size.
"""

ARABIC_DOC = """# ارتفاع ضغط الدم

ارتفاع ضغط الدم حالة مزمنة ترتفع فيها قوة الدم على جدران الشرايين.

## الأعراض

في معظم الحالات لا توجد أعراض واضحة على الإطلاق.
"""


@pytest.fixture(autouse=True)
def _no_throttling(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK,
                               "DEFAULT_THROTTLE_CLASSES": []}


@pytest.fixture(autouse=True)
def _offline_embedder(monkeypatch):
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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
def stub_llm(monkeypatch, text="Most people have no symptoms [1]."):
    """A fake provider, shaped like the real adapter response."""
    captured = {}

    def fake_complete(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return ChatResponse(text=text, provider="fake", model="fake-1")

    monkeypatch.setattr(rag_service.llm, "complete", fake_complete)
    return captured


def forbid_llm(monkeypatch):
    """Fail loudly if the pipeline calls a model when it must not."""
    def must_not_be_called(*args, **kwargs):
        raise AssertionError("the LLM must not be called without context")

    monkeypatch.setattr(rag_service.llm, "complete", must_not_be_called)


def make(role, username, **extra):
    defaults = {
        roles.PATIENT: {"age": 40},
        roles.DOCTOR: {"specialization": "Cardiology"},
        roles.RADIOLOGY: {"services": "MRI"},
        roles.LABORATORY: {"services": "CBC"},
        roles.PHARMACY: {"services": "Dispensing"},
    }[role]
    user, _account, _profile, token = register_account(
        role, username=username, password=PW,
        name=username.replace("_", " ").title(), **{**defaults, **extra})
    return user, token.key


def make_admin(username="rag_admin"):
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
def corpus(admin):
    """An approved source with one indexed, retrievable document."""
    user, _token = admin
    source = services.create_source(
        user, "WHO Cardiovascular", organization="World Health Organization",
        source_type=KnowledgeSource.GOVERNMENT, specialty="cardiology")
    services.review_source(user, source.id, KnowledgeSource.APPROVED, "Trusted.")
    document = services.add_document(
        user, source.id, identity="who/hypertension", title="Hypertension",
        text=HYPERTENSION, source_type="markdown")
    return {"user": user, "source": source, "document": document}


# ---------------------------------------------------------------------------
# Built on what already exists
# ---------------------------------------------------------------------------
class TestBuiltOnExistingInfrastructure:
    def test_no_second_retriever_embedder_or_vector_store(self):
        """Section 45: reuse, do not create a V2 of anything."""
        import pathlib
        package = pathlib.Path("knowledge/rag")
        text = "\n".join(p.read_text(encoding="utf-8")
                         for p in package.glob("*.py"))
        for forbidden in ("class Retriever", "class EmbeddingService",
                          "class VectorStore", "RetrieverV2",
                          "EmbeddingServiceV2", "VectorStoreV2"):
            assert forbidden not in text, f"{forbidden} duplicates the engine"

    def test_it_retrieves_through_the_governed_knowledge_base(self):
        import pathlib
        text = pathlib.Path("knowledge/rag/service.py").read_text(
            encoding="utf-8")
        # The gated retriever, never the ungated engine search.
        assert "knowledge_retrieval.search" in text
        assert "rag.store.search" not in text

    def test_it_reuses_the_existing_llm_service(self):
        from appointments.services.ai import llm
        assert rag_service.llm is llm

    def test_it_reuses_the_versioned_prompt_library(self):
        from appointments.services.ai import prompts
        template = prompts.get("rag_answer")
        assert template.status == "active"
        assert set(template.variables) == {"sources", "question"}

    def test_no_second_vector_database_was_added(self):
        import pathlib
        requirements = pathlib.Path("requirements.txt").read_text(
            encoding="utf-8").lower()
        for engine in ("chromadb", "qdrant", "weaviate", "pinecone", "faiss",
                       "langchain", "llama-index"):
            assert engine not in requirements


# ---------------------------------------------------------------------------
# Query processing
# ---------------------------------------------------------------------------
class TestQueryProcessing:
    def test_whitespace_is_normalised(self):
        processed = query_module.process("  what   is\n hypertension?  ")
        assert processed.text == "what is hypertension?"

    def test_the_question_is_never_rewritten(self):
        """Medical meaning lives in the exact words, including negation."""
        original = "Is it safe to STOP taking my medication if I feel fine?"
        assert query_module.process(original).text == original

    def test_an_empty_query_is_refused(self):
        with pytest.raises(query_module.InvalidQuery):
            query_module.process("   ")

    def test_a_too_short_query_is_refused(self):
        with pytest.raises(query_module.InvalidQuery):
            query_module.process("a")

    def test_a_too_long_query_is_refused(self):
        with pytest.raises(query_module.InvalidQuery):
            query_module.process("x" * (config.MAX_QUERY_CHARS + 1))

    def test_arabic_is_detected_and_not_translated(self):
        processed = query_module.process("ما هي أعراض ارتفاع ضغط الدم؟")
        assert processed.language == query_module.ARABIC
        # The text is unchanged — no translation, no transliteration.
        assert "ارتفاع" in processed.text

    def test_a_detected_language_does_not_become_a_hard_filter(self):
        """Detection is a guess; it must not silently exclude material."""
        processed = query_module.process("ما هي الأعراض؟")
        assert processed.language == query_module.ARABIC
        assert "language" not in processed.filters

    def test_an_explicit_language_is_a_filter(self):
        processed = query_module.process("what is hypertension", language="en")
        assert processed.filters["language"] == "en"
        assert processed.language_was_detected is False

    def test_filters_are_passed_through(self):
        processed = query_module.process(
            "hypertension", specialty="cardiology", topic="bp", source=3,
            document=7, section="Symptoms")
        assert processed.filters["specialty"] == "cardiology"
        assert processed.filters["source"] == 3
        assert processed.filters["document"] == 7
        assert processed.filters["section"] == "Symptoms"
        assert processed.filters["metadata"] == {"topic": "bp"}


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------
class TestContextBuilder:
    def _result(self, n, text="Some passage.", score=0.5):
        return {
            "text": text, "score": score, "section": f"Section {n}",
            "reference": f"Source {n} › Doc {n}", "page": None,
            "provenance": {"source_name": f"Source {n}",
                           "document_title": f"Doc {n}",
                           "document_version": 1, "url": ""},
            "metadata": {},
        }

    def test_context_carries_provenance(self):
        built = context_builder.build([self._result(1)])
        assert "[1]" in built.text
        assert "Source: Source 1" in built.text
        assert "Document: Doc 1 (v1)" in built.text
        assert "Section: Section 1" in built.text
        assert "Content: Some passage." in built.text

    def test_source_numbering_matches_the_block(self):
        built = context_builder.build([self._result(1), self._result(2)])
        assert [s["n"] for s in built.sources] == [1, 2]
        assert "[2]" in built.text

    def test_the_chunk_limit_is_enforced(self):
        built = context_builder.build([self._result(n) for n in range(10)],
                                      max_chunks=3)
        assert built.count == 3
        assert built.truncated is True
        assert built.dropped == 7

    def test_the_character_budget_is_enforced(self):
        long_text = "x" * 500
        built = context_builder.build(
            [self._result(n, text=long_text) for n in range(5)], max_chars=700)
        assert built.count == 1
        assert built.truncated is True

    def test_a_passage_is_included_whole_or_not_at_all(self):
        """Half a clinical statement is worse than none."""
        built = context_builder.build(
            [self._result(1, text="y" * 5000)], max_chars=500)
        assert built.count == 0
        assert built.truncated is True
        assert built.text == ""

    def test_sources_describe_what_was_sent_not_what_was_found(self):
        built = context_builder.build([self._result(n) for n in range(6)],
                                      max_chunks=2)
        assert len(built.sources) == 2

    def test_no_page_is_invented(self):
        built = context_builder.build([self._result(1)])
        assert "Page:" not in built.text
        assert built.sources[0]["page"] is None

    def test_empty_results_build_nothing(self):
        built = context_builder.build([])
        assert built.found is False and built.text == ""


# ---------------------------------------------------------------------------
# Citation verification
# ---------------------------------------------------------------------------
class TestCitations:
    def test_citation_numbers_are_parsed(self):
        assert rag.cited_numbers("A [1] and B [2, 3] and C [4][5].") == \
            {1, 2, 3, 4, 5}

    def test_valid_citations_are_marked(self):
        sources = [{"n": 1}, {"n": 2}]
        fabricated, marked = rag.verify_citations("Only [1] here.", sources)
        assert fabricated == []
        assert marked[0]["cited"] is True and marked[1]["cited"] is False

    def test_a_fabricated_citation_is_reported(self):
        sources = [{"n": 1}]
        fabricated, _marked = rag.verify_citations("As shown in [9].", sources)
        assert fabricated == [9]


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------
class TestRagPipeline:
    def test_a_grounded_answer_is_produced_from_retrieved_context(
            self, corpus, monkeypatch):
        captured = stub_llm(monkeypatch)
        result = rag.answer("What are common symptoms of hypertension?")

        assert result.degraded is False
        assert "Most people have no symptoms" in result.answer
        assert result.sources, "an answer must carry its sources"
        assert result.provider == "fake"
        assert result.prompt_version.startswith("rag_answer@")

        # The prompt actually contained the retrieved passages.
        assert "Hypertension" in captured["prompt"]
        assert "Source: WHO Cardiovascular" in captured["prompt"]
        # …and the question, unchanged.
        assert "What are common symptoms of hypertension?" in captured["prompt"]

    def test_generation_is_low_temperature_by_default(self, corpus,
                                                      monkeypatch):
        captured = stub_llm(monkeypatch)
        rag.answer("hypertension symptoms")
        assert captured["kwargs"]["temperature"] == config.TEMPERATURE
        assert config.TEMPERATURE <= 0.3

    def test_the_information_notice_is_always_appended(self, corpus,
                                                       monkeypatch):
        stub_llm(monkeypatch)
        result = rag.answer("hypertension symptoms")
        assert rag.INFORMATION_NOTICE in result.answer
        assert "not a diagnosis" in result.answer

    def test_sources_are_marked_cited_or_not(self, corpus, monkeypatch):
        stub_llm(monkeypatch, text="Symptoms are often absent [1].")
        result = rag.answer("hypertension symptoms")
        assert result.sources[0]["cited"] is True

    def test_a_fabricated_citation_degrades_the_answer(self, corpus,
                                                       monkeypatch):
        """Section 18: never generate fake citations — and never hide one."""
        stub_llm(monkeypatch, text="Per the guideline [9], symptoms vary.")
        result = rag.answer("hypertension symptoms")
        assert result.fabricated_citations == [9]
        assert result.degraded is True
        assert result.reason == "fabricated_citation"
        # Surfaced, not scrubbed: the model's words are intact.
        assert "[9]" in result.answer
        assert any("were not among the retrieved" in w for w in result.warnings)

    def test_retrieval_signals_are_relevance_not_certainty(self, corpus,
                                                           monkeypatch):
        stub_llm(monkeypatch)
        result = rag.answer("hypertension symptoms")
        signals = result.retrieval
        assert signals["matches"] >= 1
        assert 0.0 <= signals["top_score"] <= 1.0
        assert signals["match_strength"] in ("none", "weak", "strong")
        assert "not medical certainty" in signals["note"]
        # No field claims medical confidence anywhere in the payload.
        assert "confidence" not in result.as_dict()

    def test_the_answer_payload_hides_raw_chunks_by_default(self, corpus,
                                                            monkeypatch):
        stub_llm(monkeypatch)
        result = rag.answer("hypertension symptoms")
        assert "retrieved_chunks" not in result.as_dict()
        assert "retrieved_chunks" in result.as_dict(include_retrieved=True)

    def test_validation_flags_a_dose_on_rag_output_too(self, corpus,
                                                       monkeypatch):
        """A grounded answer can still state a dose, and the existing guard
        catches it here exactly as it does for the assistant.

        Flagged, not rejected: the platform's rule is to surface the answer
        with the caveat rather than rewrite it, so a reader can see what was
        said. The query has to be one the corpus answers, or the pipeline
        stops at no-context and validation is never reached — which would make
        this test pass for the wrong reason.
        """
        stub_llm(monkeypatch, text="I diagnose you with hypertension. "
                                   "Take 50 mg twice daily.")
        result = rag.answer("hypertension symptoms")
        assert result.warnings, "a dose statement must be flagged"
        assert any("dose" in w.lower() for w in result.warnings)
        assert any("not a clinical decision" in w for w in result.warnings)
        # Surfaced with the caveat, and still carrying the notice.
        assert rag.INFORMATION_NOTICE in result.answer

    def test_an_unusable_reply_is_rejected_outright(self, corpus, monkeypatch):
        """The one case validation refuses rather than flags."""
        stub_llm(monkeypatch, text="   ")
        result = rag.answer("hypertension symptoms")
        assert result.degraded is True
        assert result.reason == "rejected"
        assert "safety checks" in result.answer


# ---------------------------------------------------------------------------
# No context — the pipeline must not invent
# ---------------------------------------------------------------------------
class TestNoContextBehaviour:
    def test_the_model_is_never_called_without_context(self, admin,
                                                       monkeypatch):
        """Section 17 and 38, in one test that cannot pass by accident."""
        forbid_llm(monkeypatch)
        result = rag.answer("What is the treatment for a rare tropical fever?")
        assert result.degraded is True
        assert result.reason == "no_context"
        assert result.answer == rag.NO_CONTEXT_REPLY
        assert result.sources == []

    def test_the_refusal_names_the_limit_honestly(self, admin, monkeypatch):
        forbid_llm(monkeypatch)
        result = rag.answer("something the corpus has never heard of")
        assert "approved medical knowledge sources" in result.answer
        assert "clinician" in result.answer

    def test_an_unapproved_source_yields_no_context(self, admin, monkeypatch):
        """The gate reaches all the way into the pipeline."""
        user, _token = admin
        source = services.create_source(user, "Unreviewed")
        services.add_document(user, source.id, identity="pending/ht",
                              title="Hypertension", text=HYPERTENSION,
                              source_type="markdown")
        forbid_llm(monkeypatch)
        result = rag.answer("What are common symptoms of hypertension?")
        assert result.reason == "no_context"

    def test_an_archived_document_yields_no_context(self, corpus, monkeypatch):
        services.archive_document(corpus["user"], corpus["document"].id)
        forbid_llm(monkeypatch)
        assert rag.answer("hypertension symptoms").reason == "no_context"

    def test_a_superseded_version_is_not_used(self, corpus, monkeypatch):
        services.add_document(
            corpus["user"], corpus["source"].id, identity="who/hypertension",
            title="Hypertension", source_type="markdown",
            text=HYPERTENSION + "\n\n## Follow-up\n\nRepeat measurement.\n")
        stub_llm(monkeypatch)
        result = rag.answer("hypertension symptoms")
        versions = {s["document_version"] for s in result.sources}
        assert versions == {2}

    def test_an_invalid_query_never_reaches_retrieval(self, monkeypatch):
        forbid_llm(monkeypatch)
        result = rag.answer("  ")
        assert result.degraded is True and result.reason == "invalid_query"


# ---------------------------------------------------------------------------
# Infrastructure failures
# ---------------------------------------------------------------------------
class TestFailureHandling:
    def test_an_unconfigured_provider_degrades_safely(self, corpus,
                                                      monkeypatch):
        from appointments.services.ai import llm

        def unavailable(*args, **kwargs):
            raise llm.LLMUnavailable("nothing configured")

        monkeypatch.setattr(rag_service.llm, "complete", unavailable)
        result = rag.answer("hypertension symptoms")
        assert result.degraded is True and result.reason == "unavailable"
        assert "not available" in result.answer
        # No medical content was invented to fill the gap.
        assert "hypertension" not in result.answer.lower()

    def test_a_provider_failure_degrades_safely(self, corpus, monkeypatch):
        from appointments.services.ai import llm

        def failed(*args, **kwargs):
            raise llm.LLMFailed("fake")

        monkeypatch.setattr(rag_service.llm, "complete", failed)
        result = rag.answer("hypertension symptoms")
        assert result.degraded is True and result.reason == "failed"

    def test_a_broken_index_is_reported_not_answered(self, corpus,
                                                     monkeypatch):
        from appointments.services.rag import embeddings

        monkeypatch.setenv("RAG_HASHING_DIMENSION", "512")
        embeddings.reset()
        forbid_llm(monkeypatch)
        result = rag.answer("hypertension symptoms")
        assert result.degraded is True and result.reason == "unavailable"

    def test_a_broken_prompt_library_degrades(self, corpus, monkeypatch):
        from appointments.services.ai import prompts

        def boom(*args, **kwargs):
            raise prompts.PromptError("library unusable")

        monkeypatch.setattr(rag_service.prompts, "render", boom)
        result = rag.answer("hypertension symptoms")
        assert result.degraded is True and result.reason == "unavailable"


# ---------------------------------------------------------------------------
# No patient data
# ---------------------------------------------------------------------------
class TestNoPatientData:
    def test_the_pipeline_reads_no_patient_record(self):
        """Section 22: general medical knowledge only."""
        import pathlib
        for name in ("service.py", "query.py", "context.py", "evaluation.py"):
            text = pathlib.Path("knowledge/rag", name).read_text(
                encoding="utf-8")
            for forbidden in ("MedicalRecord", "ScreeningResult",
                              "Prescription", "RadiologyReport", "Appointment",
                              "from records", "import records"):
                assert forbidden not in text, (
                    f"knowledge/rag/{name} reaches into patient data")

    def test_the_prompt_contains_only_corpus_text(self, corpus, monkeypatch):
        patient, _token = make(roles.PATIENT, "rag_patient")
        captured = stub_llm(monkeypatch)
        rag.answer("hypertension symptoms")
        assert patient.username not in captured["prompt"]
        assert "rag_patient" not in captured["prompt"]

    def test_the_service_takes_no_user_argument(self):
        """Nothing about the caller can reach the prompt, by signature."""
        import inspect
        signature = inspect.signature(rag.answer)
        assert "user" not in signature.parameters
        assert "patient" not in signature.parameters


# ---------------------------------------------------------------------------
# Medical safety
# ---------------------------------------------------------------------------
class TestMedicalSafety:
    def test_the_prompt_forbids_inventing_sources(self):
        from appointments.services.ai import prompts
        body = prompts.get("rag_answer").body.lower()
        assert "never invent a source" in body
        assert "do not add knowledge from training" in body

    def test_the_prompt_separates_information_from_diagnosis(self):
        from appointments.services.ai import prompts
        body = prompts.get("rag_answer").body.lower()
        assert "not a diagnosis" in body
        assert "only a clinician can decide" in body

    def test_the_prompt_preserves_uncertainty(self):
        from appointments.services.ai import prompts
        body = prompts.get("rag_answer").body.lower()
        assert "keep the uncertainty" in body

    def test_the_prompt_includes_the_shared_safety_fragment(self):
        from appointments.services.ai import prompts
        assert "safety" in prompts.get("rag_answer").includes

    def test_a_diagnosis_request_still_gets_information_not_a_diagnosis(
            self, corpus, monkeypatch):
        stub_llm(monkeypatch,
                 text="Only a clinician can diagnose this. The sources "
                      "describe symptoms of hypertension [1].")
        result = rag.answer("Do I have hypertension?")
        assert result.degraded is False
        assert rag.INFORMATION_NOTICE in result.answer
        assert "not a diagnosis" in result.answer

    def test_the_answer_never_claims_medical_certainty(self, corpus,
                                                       monkeypatch):
        stub_llm(monkeypatch)
        payload = rag.answer("hypertension symptoms").as_dict()
        text = str(payload).lower()
        for claim in ("% certain", "medically certain", "confidence:"):
            assert claim not in text


# ---------------------------------------------------------------------------
# Evaluation foundation
# ---------------------------------------------------------------------------
class TestEvaluation:
    def test_a_trace_measures_retrieval_and_grounding(self, corpus,
                                                      monkeypatch):
        stub_llm(monkeypatch, text="Symptoms are often absent [1].")
        result = rag.answer("hypertension symptoms")
        processed = query_module.process("hypertension symptoms")
        trace = evaluation.build(processed, result)

        assert trace.matches >= 1
        assert trace.sources_sent >= 1
        assert trace.sources_cited == 1
        assert trace.fabricated_citations == 0
        assert trace.provider == "fake"
        assert trace.latency_ms >= 0
        assert 0.0 <= trace.citation_coverage <= 1.0

    def test_the_question_text_is_never_stored_in_a_trace(self, corpus,
                                                          monkeypatch):
        stub_llm(monkeypatch)
        question = "a very distinctive question about hypertension"
        result = rag.answer(question)
        trace = evaluation.build(query_module.process(question), result)
        body = str(trace.as_dict())
        assert question not in body
        assert "distinctive" not in body
        # A stable fingerprint instead, which cannot be read back.
        assert trace.query_id == evaluation.query_fingerprint(question)

    def test_a_degraded_answer_is_traced_with_its_reason(self, admin,
                                                         monkeypatch):
        forbid_llm(monkeypatch)
        result = rag.answer("nothing the corpus knows about")
        trace = evaluation.build(query_module.process("x y z"), result)
        assert trace.degraded is True and trace.reason == "no_context"


# ---------------------------------------------------------------------------
# API and security
# ---------------------------------------------------------------------------
class TestApiAndSecurity:
    def test_an_administrator_can_query(self, client, admin, corpus,
                                        monkeypatch):
        _user, token = admin
        stub_llm(monkeypatch)
        as_user(client, token)
        res = client.post("/api/knowledge/rag/query/",
                          {"query": "What are symptoms of hypertension?"},
                          format="json")
        assert res.status_code == 200, res.data
        assert res.data["answer"]
        assert res.data["sources"]
        assert res.data["degraded"] is False

    def test_raw_chunks_are_opt_in(self, client, admin, corpus, monkeypatch):
        _user, token = admin
        stub_llm(monkeypatch)
        as_user(client, token)
        plain = client.post("/api/knowledge/rag/query/",
                            {"query": "hypertension symptoms"}, format="json")
        assert "retrieved_chunks" not in plain.data

        debug = client.post("/api/knowledge/rag/query/",
                            {"query": "hypertension symptoms", "debug": True},
                            format="json")
        assert "retrieved_chunks" in debug.data

    def test_the_no_context_path_through_the_api(self, client, admin,
                                                 monkeypatch):
        _user, token = admin
        forbid_llm(monkeypatch)
        as_user(client, token)
        res = client.post("/api/knowledge/rag/query/",
                          {"query": "an entirely unknown topic"},
                          format="json")
        assert res.status_code == 200
        assert res.data["degraded"] is True
        assert res.data["reason"] == "no_context"

    def test_an_empty_query_is_a_400(self, client, admin):
        _user, token = admin
        as_user(client, token)
        assert client.post("/api/knowledge/rag/query/", {"query": ""},
                           format="json").status_code == 400

    @pytest.mark.parametrize("role,username", [
        (roles.PATIENT, "rag_patient_x"), (roles.DOCTOR, "rag_doctor_x"),
        (roles.LABORATORY, "rag_lab_x"), (roles.RADIOLOGY, "rag_centre_x"),
        (roles.PHARMACY, "rag_pharmacy_x"),
    ])
    def test_no_clinical_role_can_reach_the_rag_api(self, client, role,
                                                    username):
        """Section 25: this is infrastructure, not the patient copilot."""
        _actor, token = make(role, username)
        as_user(client, token)
        assert client.post("/api/knowledge/rag/query/",
                           {"query": "what is hypertension"},
                           format="json").status_code == 403
        assert client.get("/api/knowledge/rag/status/").status_code == 403

    def test_an_anonymous_caller_gets_nothing(self, client):
        client.credentials()
        assert client.post("/api/knowledge/rag/query/", {"query": "x"},
                           format="json").status_code == 401
        assert client.get("/api/knowledge/rag/status/").status_code == 401

    def test_the_status_endpoint_exposes_no_credential(self, client, admin):
        _user, token = admin
        as_user(client, token)
        res = client.get("/api/knowledge/rag/status/")
        assert res.status_code == 200
        body = str(res.data).lower()
        for secret in ("api_key", "sk-", "authorization", "bearer", "password"):
            assert secret not in body
        assert res.data["config"]["top_k"] == config.TOP_K
        assert res.data["prompt"]["status"] == "active"

    def test_the_endpoint_is_throttled_under_the_ai_scope(self):
        from knowledge.views import RagQuery
        assert RagQuery.throttle_scope == "ai"


# ---------------------------------------------------------------------------
# Nothing else broke
# ---------------------------------------------------------------------------
class TestExistingModulesStillWork:
    def test_knowledge_search_still_works(self, corpus):
        from knowledge import retrieval
        assert retrieval.search("hypertension symptoms")

    def test_the_assistant_pipeline_is_untouched(self, monkeypatch):
        from appointments.services.ai import pipeline
        patient, _token = make(roles.PATIENT, "rag_assistant_patient")

        def fake_complete(prompt, **kwargs):
            return ChatResponse(text="A safe answer.", provider="fake",
                                model="fake-1")

        monkeypatch.setattr(pipeline.llm, "complete", fake_complete)
        reply = pipeline.ask(patient, "What is a healthy blood pressure?")
        assert reply.reply
        assert reply.degraded is False

    def test_the_medical_record_still_answers(self, client):
        _patient, token = make(roles.PATIENT, "rag_record_patient")
        as_user(client, token)
        assert client.get("/api/records/me/").status_code == 200

    def test_the_knowledge_admin_api_still_answers(self, client, admin, corpus):
        _user, token = admin
        as_user(client, token)
        assert client.get("/api/knowledge/index/").status_code == 200
        assert client.get("/api/knowledge/documents/").status_code == 200
