"""RAG → LLM → grounded answer, as the patient assistant actually runs it.

``test_rag_pipeline.py`` covers the RAG service in isolation. This suite covers
the step that connects it to the assistant, and the properties that only exist
once the two are joined:

* **A general medical question is retrieved for before it is answered.** The
  assistant used to hand the message straight to the provider; a test here fails
  if it ever does again.
* **Grounding is never silently bypassed.** When retrieval is unreadable or the
  provider is down, a medical question gets a refusal — not an improvised
  answer. Enforced with a fake that raises if the ungrounded path is taken.
* **A grounded answer reads no patient record.** Proved with sentinel strings
  planted in a patient's profile and schedule.
* **The layering holds in both directions.** RAG reaches the provider only
  through the LLM facade, and never names Groq.

Generation is faked, except where the point is that the *mock provider* works
end to end — that one runs the real adapter. No credential is used anywhere.
"""
import ast
import pathlib

import pytest

from accounts import roles
from accounts.models import UserAccount
from accounts.services import register_account
from appointments.services.ai import grounding, llm as llm_module, pipeline
from appointments.services.ai.providers.base import ChatResponse
from knowledge import retrieval as knowledge_retrieval, services
from knowledge.models import KnowledgeSource
from knowledge.rag import context as context_builder
from knowledge.rag import service as rag_service

from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

PW = "Str0ng!Passw0rd"

HYPERTENSION = """# Hypertension

High blood pressure, or hypertension, is a long-term condition in which the
force of the blood against the artery walls stays elevated over time.

## Symptoms

Hypertension is often called a silent condition because most people have no
symptoms at all. When symptoms do occur they may include headaches, shortness
of breath, nosebleeds and visual changes.

## Risk factors

Risk factors for hypertension include age, excess weight, a diet high in salt,
low physical activity, alcohol and a family history of high blood pressure.
"""

ARABIC_HYPERTENSION = """# ارتفاع ضغط الدم

ارتفاع ضغط الدم هو حالة مزمنة ترتفع فيها قوة الدم على جدران الشرايين لفترة
طويلة من الزمن.

## الأعراض

في معظم الحالات لا توجد أعراض واضحة، ولهذا يسمى القاتل الصامت. وقد يشعر بعض
المرضى بالصداع أو ضيق التنفس أو نزيف الأنف.

## عوامل الخطر

من عوامل الخطر التقدم في العمر وزيادة الوزن وكثرة الملح في الطعام وقلة الحركة.
"""

HBA1C = """# HbA1c

HbA1c, or glycated haemoglobin, reflects the average blood glucose concentration
over roughly the preceding two to three months.

## Use in diabetes

HbA1c is used both to help diagnose diabetes and to monitor long-term glycaemic
control in people already diagnosed with it. It is reported as a percentage or
in mmol/mol.
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
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


@pytest.fixture(autouse=True)
def _grounding_on(monkeypatch):
    """Grounding is on by default; make the tests independent of the shell.

    Tools are switched off: this suite is about the knowledge path, and a
    tool-capable provider would route the non-grounded comparison cases through
    the agent loop instead of the context-only path they are written against.
    """
    monkeypatch.setenv("AI_GROUNDING", "auto")
    monkeypatch.setenv("AI_TOOLS", "off")


def make_admin(username="rg_admin"):
    user = User.objects.create_user(username, password=PW)
    UserAccount.objects.create(user=user, role=roles.ADMIN)
    return user, Token.objects.create(user=user).key


def make_patient(username, **extra):
    user, _account, _profile, token = register_account(
        roles.PATIENT, username=username, password=PW,
        name=username.replace("_", " ").title(), age=40, **extra)
    return user, token.key


def ingest(admin_user, source, identity, title, text, language="en"):
    return services.add_document(
        admin_user, source.id, identity=identity, title=title, text=text,
        source_type="markdown", language=language)


@pytest.fixture
def corpus():
    """An approved source holding English and Arabic reference material."""
    user, _token = make_admin()
    source = services.create_source(
        user, "WHO Cardiovascular", organization="World Health Organization",
        source_type=KnowledgeSource.GOVERNMENT, specialty="cardiology")
    services.review_source(user, source.id, KnowledgeSource.APPROVED, "Trusted.")
    ingest(user, source, "who/hypertension", "Hypertension", HYPERTENSION)
    ingest(user, source, "who/hypertension-ar", "ارتفاع ضغط الدم",
           ARABIC_HYPERTENSION, language="ar")
    ingest(user, source, "who/hba1c", "HbA1c", HBA1C)
    return {"user": user, "source": source}


def stub_llm(monkeypatch, text="Most people have no symptoms at all [1]."):
    """A fake provider, shaped like the real adapter response."""
    captured = {}

    def fake_complete(prompt, **kwargs):
        captured["prompt"] = prompt
        captured["kwargs"] = kwargs
        return ChatResponse(text=text, provider="fake", model="fake-1")

    monkeypatch.setattr(llm_module, "complete", fake_complete)
    return captured


def forbid_llm(monkeypatch, why="the model must not be called here"):
    def must_not_be_called(*args, **kwargs):
        raise AssertionError(why)

    monkeypatch.setattr(llm_module, "complete", must_not_be_called)


# ---------------------------------------------------------------------------
# Section 2 / 34 — the architecture the connection is allowed to have
# ---------------------------------------------------------------------------
def imported_modules(path):
    """Every module name a file imports, however it spells the import.

    Parsed rather than grepped: the word "models" appears in prose in half
    these files, and a substring scan would be testing the comments.
    """
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" if module else alias.name
                         for alias in node.names)
    return {name for name in names if name}


class TestLayering:
    def test_rag_never_names_a_provider(self):
        """Section 34: RAG must not know that Groq exists."""
        for path in pathlib.Path("knowledge/rag").glob("*.py"):
            for name in imported_modules(path):
                assert "groq" not in name.lower(), f"{path} imports {name}"
                assert "providers" not in name.lower(), f"{path} imports {name}"

    def test_rag_reaches_the_model_only_through_the_llm_facade(self):
        from appointments.services.ai import llm
        assert rag_service.llm is llm

    def test_the_provider_never_reaches_the_database(self):
        """Section 34: Groq must not know that PostgreSQL exists."""
        imported = imported_modules(
            "appointments/services/ai/providers/groq.py")
        for name in imported:
            assert "django" not in name, f"the provider imports {name}"
            assert "models" not in name, f"the provider imports {name}"
            assert "knowledge" not in name, f"the provider imports {name}"

    def test_the_assistant_owns_the_dependency_on_rag_not_the_reverse(self):
        """The assistant may import RAG; RAG may never import the assistant."""
        for path in pathlib.Path("knowledge/rag").glob("*.py"):
            for name in imported_modules(path):
                assert "pipeline" not in name, f"{path} imports {name}"
                assert "grounding" not in name, f"{path} imports {name}"


# ---------------------------------------------------------------------------
# Section 3 — retrieval happens before generation
# ---------------------------------------------------------------------------
class TestRetrievalComesFirst:
    def test_a_medical_question_is_answered_from_retrieved_sources(
            self, corpus, monkeypatch):
        captured = stub_llm(monkeypatch)
        user, _token = make_patient("rg_first")

        result = pipeline.ask(user, "What is hypertension?")

        assert result.grounded is True
        assert result.citations, "a grounded answer must carry citations"
        # The retrieved text really was in the prompt — not just the question.
        assert "artery walls" in captured["prompt"]
        assert "What is hypertension?" in captured["prompt"]

    def test_the_question_alone_is_never_sent_for_a_medical_question(
            self, corpus, monkeypatch):
        captured = stub_llm(monkeypatch)
        user, _token = make_patient("rg_notbare")

        pipeline.ask(user, "What are the risk factors for hypertension?")

        prompt = captured["prompt"]
        # A grounded prompt is the sources plus the question, not the question.
        assert len(prompt) > 400
        assert "[1]" in prompt

    def test_a_personal_question_still_uses_the_ordinary_assistant(
            self, corpus, monkeypatch):
        captured = stub_llm(monkeypatch, text="Your next visit is on Tuesday.")
        user, _token = make_patient("rg_personal")

        result = pipeline.ask(user, "When is my appointment?")

        assert result.grounded is False
        assert result.citations == []
        assert "artery walls" not in captured["prompt"]

    def test_grounding_is_skipped_when_the_knowledge_base_is_empty(
            self, monkeypatch):
        """No corpus is a deployment state, not a retrieval failure."""
        captured = stub_llm(monkeypatch, text="Some general guidance.")
        user, _token = make_patient("rg_empty")

        result = pipeline.ask(user, "What is hypertension?")

        assert result.grounded is False
        assert result.citations == []
        assert captured["prompt"] == "What is hypertension?"

    def test_grounding_can_be_required_even_with_an_empty_corpus(
            self, monkeypatch):
        monkeypatch.setenv("AI_GROUNDING", "on")
        forbid_llm(monkeypatch, "no corpus means no model call")
        user, _token = make_patient("rg_required")

        result = pipeline.ask(user, "What is hypertension?")

        assert result.degraded is True
        assert rag_service.NO_CONTEXT_REPLY in result.reply

    def test_grounding_can_be_turned_off(self, corpus, monkeypatch):
        monkeypatch.setenv("AI_GROUNDING", "off")
        captured = stub_llm(monkeypatch, text="General guidance.")
        user, _token = make_patient("rg_off")

        result = pipeline.ask(user, "What is hypertension?")

        assert result.grounded is False
        assert captured["prompt"] == "What is hypertension?"


# ---------------------------------------------------------------------------
# Section 31, tests 1-3 and sections 20-23 — language
# ---------------------------------------------------------------------------
class TestLanguages:
    def test_english_question_retrieves_english_material(self, corpus):
        hits = knowledge_retrieval.search("What is hypertension?")
        assert hits
        assert any("artery walls" in hit["text"] for hit in hits)

    def test_arabic_question_retrieves_arabic_material(self, corpus):
        hits = knowledge_retrieval.search("ما هو ارتفاع ضغط الدم؟")
        assert hits, "Arabic retrieval returned nothing"
        assert any("الشرايين" in hit["text"] or "ضغط الدم" in hit["text"]
                   for hit in hits)

    def test_a_bilingual_question_retrieves_relevant_material(self, corpus):
        hits = knowledge_retrieval.search(
            "What is HbA1c وهل يستخدم لمتابعة السكري؟")
        assert hits
        assert any("HbA1c" in hit["text"] for hit in hits)

    def test_an_arabic_question_is_routed_and_answered_in_arabic(
            self, corpus, monkeypatch):
        captured = stub_llm(
            monkeypatch, text="في معظم الحالات لا توجد أعراض واضحة [1].")
        user, _token = make_patient("rg_ar")

        result = pipeline.ask(user, "ما هي أعراض ارتفاع ضغط الدم؟")

        assert result.grounded is True
        # The instruction that preserves the user's language is in the prompt,
        # and the model's Arabic reply survives the pipeline unaltered.
        assert "same language" in captured["prompt"]
        assert "أعراض" in result.reply

    def test_a_bilingual_question_is_grounded(self, corpus, monkeypatch):
        stub_llm(monkeypatch, text="HbA1c reflects average glucose [1].")
        user, _token = make_patient("rg_bi")

        result = pipeline.ask(user, "What is HbA1c وهل يستخدم لمتابعة السكري؟")

        assert result.grounded is True
        assert result.citations


# ---------------------------------------------------------------------------
# Section 31 test 4, sections 11 and 25 — nothing relevant
# ---------------------------------------------------------------------------
class TestNoRelevantKnowledge:
    def test_an_uncovered_medical_question_is_refused_not_improvised(
            self, corpus, monkeypatch):
        forbid_llm(monkeypatch, "no context must mean no model call")
        user, _token = make_patient("rg_nocontext")

        result = pipeline.ask(
            user, "What is the treatment for Fahr syndrome calcification?")

        assert result.degraded is True
        assert result.grounded is False
        assert rag_service.NO_CONTEXT_REPLY in result.reply
        assert "clinician" in result.reply


# ---------------------------------------------------------------------------
# Section 31 test 5, sections 26-27 — RAG unavailable
# ---------------------------------------------------------------------------
class TestRetrievalFailure:
    def test_an_unreadable_corpus_never_falls_back_to_the_model(
            self, corpus, monkeypatch):
        def unavailable(*args, **kwargs):
            raise knowledge_retrieval.RetrievalUnavailable(
                "the index was built with a different embedder")

        monkeypatch.setattr(rag_service.knowledge_retrieval, "search",
                            unavailable)
        forbid_llm(monkeypatch,
                   "a broken index must not become an ungrounded answer")
        user, _token = make_patient("rg_ragdown")

        result = pipeline.ask(user, "What are the symptoms of hypertension?")

        assert result.degraded is True
        assert rag_service.UNAVAILABLE_REPLY in result.reply

    def test_the_failure_message_carries_no_internal_detail(
            self, corpus, monkeypatch):
        def unavailable(*args, **kwargs):
            raise knowledge_retrieval.RetrievalUnavailable(
                "pgvector dimension 2048 != 1536 at chunk table")

        monkeypatch.setattr(rag_service.knowledge_retrieval, "search",
                            unavailable)
        user, _token = make_patient("rg_ragdown2")

        reply = pipeline.ask(user, "What causes hypertension?").reply
        for leak in ("pgvector", "2048", "chunk table", "Traceback"):
            assert leak not in reply


# ---------------------------------------------------------------------------
# Section 31 test 6, section 26 — the provider is down
# ---------------------------------------------------------------------------
class TestProviderFailure:
    def test_an_unconfigured_provider_is_reported_not_guessed_around(
            self, corpus, monkeypatch):
        def unavailable(*args, **kwargs):
            raise llm_module.LLMUnavailable("no provider configured")

        monkeypatch.setattr(llm_module, "complete", unavailable)
        user, _token = make_patient("rg_llmdown")

        result = pipeline.ask(user, "What is hypertension?")

        assert result.degraded is True
        assert rag_service.UNAVAILABLE_REPLY in result.reply

    def test_a_failing_provider_does_not_leak_its_error(
            self, corpus, monkeypatch):
        def failed(*args, **kwargs):
            raise llm_module.LLMFailed(
                "401 from https://api.groq.com/openai/v1 key gsk_secret")

        monkeypatch.setattr(llm_module, "complete", failed)
        user, _token = make_patient("rg_llmfail")

        result = pipeline.ask(user, "What causes hypertension?")

        assert result.degraded is True
        assert rag_service.FAILED_REPLY in result.reply
        for leak in ("gsk_", "api.groq.com", "401"):
            assert leak not in result.reply

    def test_a_provider_failure_is_not_retried_without_grounding(
            self, corpus, monkeypatch):
        """Section 27: a failed grounded call must not become an ungrounded one."""
        calls = []

        def failed(prompt, **kwargs):
            calls.append(prompt)
            raise llm_module.LLMFailed("provider down")

        monkeypatch.setattr(llm_module, "complete", failed)
        user, _token = make_patient("rg_noretry")

        pipeline.ask(user, "What are the risk factors for hypertension?")

        assert len(calls) == 1, "the question was sent to the model twice"


# ---------------------------------------------------------------------------
# Section 31 test 7, sections 6 and 16 — source attribution
# ---------------------------------------------------------------------------
class TestSourceAttribution:
    def test_citations_carry_the_metadata_the_knowledge_base_holds(
            self, corpus, monkeypatch):
        stub_llm(monkeypatch)
        user, _token = make_patient("rg_attr")

        result = pipeline.ask(user, "What is hypertension?")

        citation = result.citations[0]
        assert citation["n"] == 1
        assert citation["document_id"]
        assert citation["source_id"]
        assert citation["source_name"] == "WHO Cardiovascular"
        assert citation["document_title"]
        assert citation["reference"]

    def test_no_url_or_page_is_invented(self, corpus, monkeypatch):
        stub_llm(monkeypatch)
        user, _token = make_patient("rg_nofake")

        result = pipeline.ask(user, "What is hypertension?")

        for citation in result.citations:
            # Markdown has no pages and this source has no URL, so both must
            # come back empty rather than plausible.
            assert citation["page"] is None
            assert citation["url"] == ""

    def test_a_citation_the_model_invented_is_reported(
            self, corpus, monkeypatch):
        stub_llm(monkeypatch, text="Hypertension is symptomless [1] [9].")
        user, _token = make_patient("rg_fab")

        result = pipeline.ask(user, "What is hypertension?")

        assert result.warnings
        assert any("9" in warning for warning in result.warnings)

    def test_which_sources_the_answer_used_is_marked(
            self, corpus, monkeypatch):
        stub_llm(monkeypatch, text="Hypertension is symptomless [1].")
        user, _token = make_patient("rg_used")

        result = pipeline.ask(user, "What is hypertension?")

        assert result.citations[0]["cited"] is True


# ---------------------------------------------------------------------------
# Section 31 test 8, section 18 — patient data is not automatically included
# ---------------------------------------------------------------------------
class TestPatientDataIsolation:
    def test_a_grounded_answer_reads_no_patient_record(
            self, corpus, monkeypatch):
        captured = stub_llm(monkeypatch)
        user, _token = make_patient(
            "rg_isolated",
            medical_history="SENTINEL-HISTORY: previous myocardial infarction")

        result = pipeline.ask(user, "What is hypertension?")

        assert "SENTINEL-HISTORY" not in captured["prompt"]
        assert "rg_isolated" not in captured["prompt"]
        # The record-attribution list is empty because nothing was read from it.
        assert result.sources == []

    def test_the_grounded_prompt_contains_only_sources_and_the_question(
            self, corpus, monkeypatch):
        captured = stub_llm(monkeypatch)
        user, _token = make_patient("rg_onlysources")

        pipeline.ask(user, "What is hypertension?")

        prompt = captured["prompt"]
        for patient_word in ("Upcoming appointments", "Profile:",
                             "Medical history the patient recorded"):
            assert patient_word not in prompt

    def test_rag_cannot_reach_patient_tables(self):
        """Nothing in the RAG package touches a clinical model."""
        text = "\n".join(p.read_text(encoding="utf-8")
                         for p in pathlib.Path("knowledge/rag").glob("*.py"))
        for model in ("Appointment", "Prescription", "LabOrder",
                      "RadiologyOrder", "PatientProfile", "MedicalRecord"):
            assert model not in text


# ---------------------------------------------------------------------------
# Section 15 — duplicate chunks
# ---------------------------------------------------------------------------
class TestDeduplication:
    def _passage(self, text, document_id=1, score=0.5):
        return {"text": text, "score": score, "section": "",
                "reference": f"Doc {document_id}",
                "provenance": {"document_id": document_id,
                               "document_title": f"Doc {document_id}",
                               "source_id": 1, "source_name": "Src"}}

    def test_an_overlapping_chunk_from_the_same_document_is_not_sent_twice(self):
        long_text = ("Hypertension is a long term condition in which the force "
                     "of blood against the artery walls stays elevated.")
        built = context_builder.build([
            self._passage(long_text, score=0.9),
            # The same statement with a few words added — what chunk overlap
            # produces.
            self._passage(long_text + " Over time this damages the arteries.",
                          score=0.7),
        ])
        assert built.count == 1
        assert built.deduplicated == 1
        assert built.dropped == 0

    def test_the_better_matching_passage_is_the_one_kept(self):
        text = "Blood pressure is measured at rest with a correctly sized cuff."
        built = context_builder.build([
            self._passage(text + " Repeat on another occasion.", score=0.9),
            self._passage(text, score=0.4),
        ])
        assert "Repeat on another occasion" in built.text

    def test_the_same_statement_from_two_documents_keeps_both_citations(self):
        text = "Hypertension is often symptomless in its early stages."
        built = context_builder.build([
            self._passage(text, document_id=1, score=0.9),
            self._passage(text, document_id=2, score=0.8),
        ])
        # Two sources agreeing is corroboration, and each earns its own number.
        assert built.count == 2
        assert built.deduplicated == 0

    def test_deduplication_can_be_disabled(self):
        text = "Hypertension is often symptomless in its early stages."
        built = context_builder.build(
            [self._passage(text, score=0.9), self._passage(text, score=0.8)],
            dedupe_overlap=1.0)
        assert built.count == 2

    def test_distinct_passages_are_left_alone(self):
        built = context_builder.build([
            self._passage("Hypertension is often symptomless.", score=0.9),
            self._passage("Risk factors include age and excess weight.",
                          score=0.8),
        ])
        assert built.count == 2
        assert built.deduplicated == 0


# ---------------------------------------------------------------------------
# Section 29 — latency, measured where it is spent
# ---------------------------------------------------------------------------
class TestPerformanceSignals:
    def test_retrieval_and_generation_are_timed_separately(
            self, corpus, monkeypatch):
        stub_llm(monkeypatch)
        result = rag_service.answer("What is hypertension?")

        assert result.latency_ms >= 0
        assert result.retrieval_ms >= 0
        assert result.llm_ms >= 0
        # The parts cannot exceed the whole.
        assert result.retrieval_ms + result.llm_ms <= result.latency_ms + 5

    def test_retrieval_is_timed_even_when_it_fails(self, corpus, monkeypatch):
        def unavailable(*args, **kwargs):
            raise knowledge_retrieval.RetrievalUnavailable("index unreadable")

        monkeypatch.setattr(rag_service.knowledge_retrieval, "search",
                            unavailable)
        result = rag_service.answer("What is hypertension?")

        assert result.reason == "unavailable"
        assert result.llm_ms == 0

    def test_the_trace_records_the_split_without_the_question(
            self, corpus, monkeypatch, caplog):
        stub_llm(monkeypatch)
        with caplog.at_level("INFO", logger="appointments"):
            rag_service.answer("What is hypertension?")

        traces = [r.getMessage() for r in caplog.records
                  if "RAG trace" in r.getMessage()]
        assert traces
        assert "retrieval_ms" in traces[0] and "llm_ms" in traces[0]
        assert "hypertension" not in traces[0].lower()


# ---------------------------------------------------------------------------
# Section 33 — the mock provider still works, so RAG is not tied to Groq
# ---------------------------------------------------------------------------
class TestProviderIndependence:
    def test_the_whole_pipeline_runs_on_the_mock_provider(
            self, corpus, monkeypatch):
        """No network, no key: the real adapter, chosen by configuration."""
        monkeypatch.setenv("AI_PROVIDER", "mock")
        result = rag_service.answer("What is hypertension?")

        assert result.provider == "mock"
        assert result.degraded is False
        assert result.sources

    def test_switching_provider_is_configuration_not_code(
            self, corpus, monkeypatch):
        text = pathlib.Path("knowledge/rag/service.py").read_text(
            encoding="utf-8")
        assert "GROQ" not in text
        assert "AI_PROVIDER" not in text

    def test_the_assistant_reports_which_provider_answered(
            self, corpus, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "mock")
        user, _token = make_patient("rg_which")

        result = pipeline.ask(user, "What is hypertension?")

        assert result.provider == "mock"
        assert result.prompt_version.startswith("rag_answer@")


# ---------------------------------------------------------------------------
# Sections 17 and 30 — the API surface
# ---------------------------------------------------------------------------
class TestApiAndSecurity:
    def test_the_chat_endpoint_returns_the_answer_and_its_sources(
            self, corpus, monkeypatch):
        stub_llm(monkeypatch, text="Hypertension is symptomless [1].")
        _user, token = make_patient("rg_api")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")

        response = client.post("/api/chat/ask/",
                               {"message": "What is hypertension?"},
                               format="json")

        assert response.status_code == 200
        body = response.json()
        assert body["grounded"] is True
        assert body["citations"]
        assert body["citations"][0]["reference"]

    def test_the_response_exposes_no_key_prompt_or_vector_internals(
            self, corpus, monkeypatch):
        # Deliberately NOT shaped like a real key. A ``gsk_`` literal here would
        # be a Groq-shaped string in a committed file, which is exactly what the
        # repository-wide secret scan exists to find.
        fake_key = "unit-test-groq-key-not-real"
        monkeypatch.setenv("GROQ_API_KEY", fake_key)
        stub_llm(monkeypatch)
        _user, token = make_patient("rg_apisec")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")

        body = client.post("/api/chat/ask/",
                           {"message": "What is hypertension?"},
                           format="json").content.decode()

        for leak in (fake_key, "gsk_", "GROQ_API_KEY", "api.groq.com",
                     "embedding", "Answer the question using", "Traceback"):
            assert leak not in body

    def test_an_anonymous_caller_cannot_ask(self, corpus):
        response = APIClient().post("/api/chat/ask/",
                                    {"message": "What is hypertension?"},
                                    format="json")
        assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Section 19 — medical safety on the grounded path
# ---------------------------------------------------------------------------
class TestMedicalSafety:
    def test_an_answer_is_labelled_as_information_not_diagnosis(
            self, corpus, monkeypatch):
        stub_llm(monkeypatch)
        user, _token = make_patient("rg_notice")

        reply = pipeline.ask(user, "What is hypertension?").reply

        assert rag_service.INFORMATION_NOTICE in reply

    def test_an_emergency_notice_survives_the_grounded_path(
            self, corpus, monkeypatch):
        from shared.safety import EMERGENCY_NOTICE
        stub_llm(monkeypatch)
        user, _token = make_patient("rg_emergency")

        result = pipeline.ask(
            user, "I have crushing chest pain — what causes chest pain?")

        assert result.emergency["detected"] is True
        assert result.reply.startswith(EMERGENCY_NOTICE)

    def test_a_dosing_answer_is_flagged_on_the_grounded_path(
            self, corpus, monkeypatch):
        stub_llm(monkeypatch,
                 text="Take 500 mg of amlodipine twice daily [1].")
        user, _token = make_patient("rg_dose")

        result = pipeline.ask(user, "What is the treatment for hypertension?")

        assert result.warnings or result.degraded


# ---------------------------------------------------------------------------
# The routing heuristic, on its own
# ---------------------------------------------------------------------------
class TestRouting:
    @pytest.mark.parametrize("message", [
        "What is hypertension?",
        "what causes high blood pressure",
        "How does metformin work",
        "symptoms of diabetes",
        "is it safe to exercise with asthma",
        "ما هو ارتفاع ضغط الدم؟",
        "ما هي أعراض السكري",
        "علاج الصداع النصفي",
    ])
    def test_medical_questions_are_recognised(self, message):
        assert grounding.looks_like_medical_question(message) is True

    @pytest.mark.parametrize("message", [
        "hello",
        "thanks!",
        "When is my appointment?",
        "cancel my appointment please",
        "book an appointment with a cardiologist",
        "I forgot my password",
        "موعدي القادم امتى",
        "احجز لي موعد",
    ])
    def test_personal_and_conversational_messages_are_not(self, message):
        assert grounding.looks_like_medical_question(message) is False

    def test_a_personal_phrasing_wins_over_a_medical_word(self):
        assert grounding.looks_like_medical_question(
            "what is my appointment time") is False

    def test_the_mode_falls_back_to_auto_when_misconfigured(self, monkeypatch):
        monkeypatch.setenv("AI_GROUNDING", "yes-please")
        assert grounding.mode() == "auto"

    def test_a_broken_grounding_branch_does_not_cost_the_user_an_answer(
            self, corpus, monkeypatch):
        def boom(message):
            raise RuntimeError("grounding exploded")

        monkeypatch.setattr(pipeline.grounding, "attempt", boom)
        stub_llm(monkeypatch, text="A general answer.")
        user, _token = make_patient("rg_boom")

        result = pipeline.ask(user, "What is hypertension?")

        assert result.reply
        assert result.grounded is False


# ---------------------------------------------------------------------------
# Nothing was duplicated to build this
# ---------------------------------------------------------------------------
class TestNothingWasDuplicated:
    def test_no_second_rag_service_or_context_builder(self):
        text = pathlib.Path(
            "appointments/services/ai/grounding.py").read_text(encoding="utf-8")
        for forbidden in ("class RAGService", "class ContextBuilder",
                          "class Retriever", "def retrieve(", "def embed("):
            assert forbidden not in text
        # It delegates rather than reimplementing.
        assert "rag.answer(" in text

    def test_no_second_endpoint_was_added_for_the_assistant(self):
        text = pathlib.Path("appointments/urls.py").read_text(encoding="utf-8")
        assert text.count("chat/ask/") == 1

    def test_the_conversation_store_recognises_grounded_non_answers(self):
        from appointments.services import chat
        assert chat.is_non_answer(rag_service.NO_CONTEXT_REPLY)
        assert chat.is_non_answer(rag_service.UNAVAILABLE_REPLY)
        assert not chat.is_non_answer("Hypertension is symptomless [1].")
