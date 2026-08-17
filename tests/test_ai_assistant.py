"""TASK 02 — the production AI assistant pipeline.

    user -> context -> prompt -> llm -> validation -> response

Every test here injects the provider seam (:func:`_stub_llm`). None of them
resolve a real provider or touch the network: the suite must not depend on which
API keys happen to be in the developer's ``.env``.
"""
import datetime
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient

from appointments.models import (
    Appointment, ChatMessage, Doctor, PatientProfile,
)
from appointments.services.ai import context, llm, pipeline, prompts, validation

pytestmark = pytest.mark.django_db

PW = "Str0ng!Passw0rd"


@pytest.fixture(autouse=True)
def _no_throttling(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_CLASSES": []}


@pytest.fixture(autouse=True)
def _no_ambient_provider(monkeypatch):
    """Nothing in the environment may select a live provider during tests."""
    for key in ("AI_PROVIDER", "GROQ_API_KEY", "OPENAI_API_KEY",
                "GEMINI_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def client():
    return APIClient()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _patient(username="pat", **profile):
    user = User.objects.create_user(username=username, password=PW)
    PatientProfile.objects.create(
        user=user,
        age=profile.get("age", 54),
        gender=profile.get("gender", "Male"),
        medical_history=profile.get("medical_history", "Type 2 diabetes since 2019"),
    )
    return user


def _doctor(username="doc", name="Sarah Ahmed", specialization="Cardiology"):
    user = User.objects.create_user(username=username, password=PW)
    Doctor.objects.create(user=user, name=name, specialization=specialization)
    return user


def _appointment(patient, provider_user, days=3, reason="follow-up"):
    """A booking on the unified schema.

    The provider is the *user*, not a Doctor row, and the appointment occupies a
    period rather than an instant — see appointments.models.Appointment.
    """
    start = (timezone.localtime() + datetime.timedelta(days=days)).replace(
        microsecond=0)
    return Appointment.objects.create(
        provider=provider_user, patient=patient, start_at=start,
        end_at=start + datetime.timedelta(minutes=30), reason=reason)


def _stub_llm(monkeypatch, text="A clear, safe answer."):
    """Replace the provider seam; return a dict of what the pipeline sent."""
    captured = {}

    def fake_complete(prompt, *, history=None, system_prompt=None):
        captured.update(prompt=prompt, history=history, system_prompt=system_prompt)
        return llm.LLMResult(text=text, provider="stub", model="stub-1")

    monkeypatch.setattr(pipeline.llm, "complete", fake_complete)
    return captured


def _token(client, user):
    from rest_framework.authtoken.models import Token
    token, _ = Token.objects.get_or_create(user=user)
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return token.key


# ===========================================================================
# Prompt management
# ===========================================================================
class TestPromptManagement:
    """Prompt *mechanics* moved to tests/test_prompts.py when the library
    replaced the hardcoded templates (Task 04). Each assertion has a stronger
    equivalent there:

        every_template_carries_the_safety_rules
          -> TestLibrary::test_every_model_facing_prompt_carries_the_safety_rules
        templates_are_versioned
          -> TestParsing::test_versions_must_be_semver
        patient_and_doctor_get_different_briefs
          -> TestVersioning::test_each_role_gets_its_own_prompt
        an_unknown_role_falls_back_rather_than_crashing
          -> TestVersioning::test_an_unknown_role_falls_back_rather_than_crashing
        the_context_block_is_actually_substituted
          -> TestComposition::test_the_context_block_is_substituted_...
        a_template_missing_its_variable_fails_loudly
          -> TestVariables::test_a_missing_value_raises_rather_than_leaving_a_hole

    What stays here is the *integration*: that the pipeline actually renders
    from the library and reports which prompt it used.
    """

    def test_the_pipeline_sends_a_prompt_built_from_the_library(self, monkeypatch):
        captured = _stub_llm(monkeypatch)
        pipeline.ask(_patient("pm1"), "hello")
        sent = captured["system_prompt"]
        assert prompts.get("safety").body in sent, "the safety fragment is missing"
        assert prompts.get("medical_assistant").body in sent

    def test_the_reply_records_which_prompt_produced_it(self, monkeypatch):
        _stub_llm(monkeypatch)
        result = pipeline.ask(_patient("pm2"), "hello")
        assert result.prompt_version == prompts.get("medical_assistant").id

    def test_a_doctor_gets_the_clinician_prompt(self, monkeypatch):
        _stub_llm(monkeypatch)
        result = pipeline.ask(_doctor("pm3doc"), "hello")
        assert result.prompt_version.startswith("doctor_copilot@")

    def test_an_unusable_prompt_library_degrades_instead_of_500ing(self, monkeypatch):
        def boom(*args, **kwargs):
            raise prompts.PromptError("library is broken")

        monkeypatch.setattr(pipeline.prompts, "system_prompt", boom)
        result = pipeline.ask(_patient("pm4"), "hello")
        assert result.degraded is True
        assert result.reply == pipeline.UNCONFIGURED_REPLY


# ===========================================================================
# User-aware context
# ===========================================================================
class TestContext:
    def test_patient_profile_reaches_the_prompt(self, monkeypatch):
        captured = _stub_llm(monkeypatch)
        pipeline.ask(_patient("ctx1"), "how am I doing?")
        assert "Type 2 diabetes since 2019" in captured["system_prompt"]
        assert "age 54" in captured["system_prompt"]

    def test_upcoming_appointments_reach_the_prompt(self, monkeypatch):
        captured = _stub_llm(monkeypatch)
        patient = _patient("ctx2")
        _appointment(patient, _doctor("ctx2doc"), reason="chest tightness")
        pipeline.ask(patient, "what should I prepare?")
        assert "Sarah Ahmed" in captured["system_prompt"]
        assert "chest tightness" in captured["system_prompt"]

    def test_sources_are_attributed_back_to_the_user(self, monkeypatch):
        _stub_llm(monkeypatch)
        patient = _patient("ctx4")
        _appointment(patient, _doctor("ctx4doc"))
        kinds = {s["kind"] for s in pipeline.ask(patient, "hello").sources}
        assert {"profile", "appointments"} <= kinds

    def test_a_user_with_no_records_still_gets_an_answer(self, monkeypatch):
        _stub_llm(monkeypatch)
        bare = User.objects.create_user(username="ctx5", password=PW)
        result = pipeline.ask(bare, "what is hypertension?")
        assert result.reply == "A clear, safe answer."
        assert result.degraded is False

    def test_a_doctor_gets_the_doctor_brief_and_their_own_schedule(self, monkeypatch):
        captured = _stub_llm(monkeypatch)
        doctor = _doctor("ctx6doc", name="Mona Fahmy", specialization="Endocrinology")
        _appointment(_patient("ctx6pat"), doctor)
        result = pipeline.ask(doctor, "what does my day look like?")
        assert result.degraded is False
        assert "Endocrinology" in captured["system_prompt"]
        assert "clinical assistant" in captured["system_prompt"].lower()

    def test_context_never_leaks_another_users_record(self, monkeypatch):
        captured = _stub_llm(monkeypatch)
        _patient("ctx7a", medical_history="HIV positive since 2011")
        asker = _patient("ctx7b", medical_history="Seasonal allergies")
        pipeline.ask(asker, "any advice?")
        assert "HIV" not in captured["system_prompt"], "another patient's record leaked"
        assert "Seasonal allergies" in captured["system_prompt"]

    def test_the_context_block_is_bounded(self, monkeypatch):
        captured = _stub_llm(monkeypatch)
        patient = _patient("ctx8", medical_history="x" * 5000)
        pipeline.ask(patient, "hi")
        assembled = context.build(patient)
        assert len(assembled.facts) <= context.MAX_CONTEXT_CHARS
        assert "x" * 5000 not in captured["system_prompt"]

    def test_a_broken_context_does_not_cost_the_user_an_answer(self, monkeypatch):
        _stub_llm(monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("database on fire")

        monkeypatch.setattr(pipeline.context_module, "build", boom)
        result = pipeline.ask(_patient("ctx9"), "hello?")
        assert result.reply == "A clear, safe answer."


# ===========================================================================
# Response validation
# ===========================================================================
class TestValidation:
    def test_a_clean_answer_passes_without_warnings(self):
        outcome = validation.validate("Walking 30 minutes a day helps blood pressure.")
        assert outcome.ok and outcome.warnings == []

    @pytest.mark.parametrize("reply", [
        "Take 500 mg of metformin twice daily.",
        "Start with 10mcg and increase weekly.",
        "You should take 2 tablets after meals.",
    ])
    def test_a_stated_dose_is_flagged(self, reply):
        assert validation.DOSE_WARNING in validation.validate(reply).warnings

    @pytest.mark.parametrize("reply", [
        "You have type 2 diabetes.",
        "I diagnose this as hypertension.",
        "Stop taking your beta blocker.",
    ])
    def test_diagnosis_or_prescription_language_is_flagged(self, reply):
        assert validation.PRESCRIPTION_WARNING in validation.validate(reply).warnings

    def test_an_empty_reply_is_rejected(self):
        for reply in ("", "   ", None):
            assert validation.validate(reply).ok is False

    def test_an_emergency_answer_without_care_advice_is_flagged(self):
        outcome = validation.validate("Chest pain is often caused by indigestion.")
        assert validation.UNSAFE_EMERGENCY_WARNING in outcome.warnings

    def test_an_emergency_answer_that_directs_to_care_is_not_flagged(self):
        outcome = validation.validate(
            "Chest pain can be serious — seek medical attention immediately.")
        assert validation.UNSAFE_EMERGENCY_WARNING not in outcome.warnings

    def test_ordinary_numbers_are_not_mistaken_for_doses(self):
        outcome = validation.validate("Aim for 30 minutes of exercise, 5 days a week.")
        assert outcome.warnings == []

    def test_warnings_are_surfaced_not_silently_applied(self, monkeypatch):
        """A flagged answer is still shown, with the caveat beside it."""
        _stub_llm(monkeypatch, "Take 500 mg of metformin daily.")
        result = pipeline.ask(_patient("val1"), "how much metformin?")
        assert "500 mg" in result.reply, "the answer was silently rewritten"
        assert validation.DOSE_WARNING in result.warnings

    def test_an_unusable_answer_is_withheld(self, monkeypatch):
        _stub_llm(monkeypatch, "")
        result = pipeline.ask(_patient("val2"), "hello?")
        assert result.degraded is True
        assert result.reply == pipeline.REJECTED_REPLY


# ===========================================================================
# Safe fallback behaviour
# ===========================================================================
class TestSafeFallback:
    def test_no_provider_gives_an_honest_message_not_a_crash(self, monkeypatch):
        monkeypatch.setattr(llm, "active_provider", lambda: None)
        result = pipeline.ask(_patient("fb1"), "what is a normal blood sugar?")
        assert result.degraded is True
        assert result.reply == pipeline.UNCONFIGURED_REPLY

    def test_a_provider_failure_never_leaks_internals(self, monkeypatch):
        def boom(*args, **kwargs):
            raise llm.LLMFailed("groq")

        monkeypatch.setattr(pipeline.llm, "complete", boom)
        reply = pipeline.ask(_patient("fb2"), "what is a normal blood sugar?").reply
        for leak in ("groq", "Traceback", "http", "api_key"):
            assert leak.lower() not in reply.lower(), f"{leak} leaked to the patient"

    def test_the_exchange_is_recorded_even_when_the_provider_is_down(self, monkeypatch):
        monkeypatch.setattr(llm, "active_provider", lambda: None)
        user = _patient("fb3")
        pipeline.ask(user, "hello?")
        assert ChatMessage.objects.filter(user=user).count() == 2

    def test_an_outage_reply_is_stored_but_never_replayed_as_memory(self, monkeypatch):
        """Found live: with the provider rate-limited, a third of a user's recent
        turns were "couldn't reach its provider" — and every one was being sent
        back to the model as conversation history."""
        from appointments.services import chat as chat_service

        user = _patient("fb5")
        monkeypatch.setattr(llm, "active_provider", lambda: None)
        pipeline.ask(user, "first question")

        # The transcript keeps it, so the page matches what the user saw...
        assert ChatMessage.objects.filter(user=user).count() == 2
        # ...but the model never sees it again.
        assert chat_service.context_messages(user) == [
            {"role": "user", "content": "first question"}]

        captured = _stub_llm(monkeypatch)
        pipeline.ask(user, "second question")
        replayed = [m["content"] for m in captured["history"]]
        assert pipeline.FAILED_REPLY not in replayed
        assert pipeline.UNCONFIGURED_REPLY not in replayed

    def test_an_outage_reply_is_still_filtered_when_it_carries_an_emergency_notice(self, monkeypatch):
        from appointments.services import chat as chat_service

        user = _patient("fb6")
        monkeypatch.setattr(llm, "active_provider", lambda: None)
        pipeline.ask(user, "I have crushing chest pain")

        contents = [m["content"] for m in chat_service.context_messages(user)]
        assert not any(pipeline.UNCONFIGURED_REPLY in c for c in contents)

    def test_a_real_answer_is_kept_as_memory(self, monkeypatch):
        """The filter must not swallow genuine answers."""
        from appointments.services import chat as chat_service

        user = _patient("fb7")
        _stub_llm(monkeypatch, "Walking helps.")
        pipeline.ask(user, "what helps?")
        assert {"role": "assistant", "content": "Walking helps."} in \
            chat_service.context_messages(user)

    def test_a_storage_failure_still_returns_the_answer(self, monkeypatch):
        _stub_llm(monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("disk full")

        monkeypatch.setattr(pipeline.chat, "record_exchange", boom)
        assert pipeline.ask(_patient("fb4"), "hi").reply == "A clear, safe answer."


# ===========================================================================
# The endpoint
# ===========================================================================
class TestAskEndpoint:
    def test_anonymous_callers_are_rejected(self, client):
        assert client.post("/api/chat/ask/", {"message": "hi"},
                           format="json").status_code == 401
        assert client.get("/api/chat/status/").status_code == 401

    def test_a_blank_message_is_rejected_before_any_provider_call(self, client, monkeypatch):
        captured = _stub_llm(monkeypatch)
        _token(client, _patient("ep1"))
        assert client.post("/api/chat/ask/", {"message": "   "},
                           format="json").status_code == 400
        assert captured == {}, "a blank message reached the provider"

    def test_the_response_is_structured(self, client, monkeypatch):
        _stub_llm(monkeypatch)
        _token(client, _patient("ep2"))
        res = client.post("/api/chat/ask/", {"message": "what is HbA1c?"}, format="json")
        assert res.status_code == 200
        for key in ("reply", "emergency", "sources", "warnings", "provider",
                    "model", "prompt_version", "degraded", "messages"):
            assert key in res.data, f"{key} missing from the assistant contract"
        assert res.data["provider"] == "stub"
        # name@version of the prompt behind this answer, so it stays traceable.
        assert res.data["prompt_version"] == prompts.get("medical_assistant").id

    def test_one_question_stores_exactly_one_exchange(self, client, monkeypatch):
        """Regression: the UI used to persist the turn the endpoint already saved."""
        _stub_llm(monkeypatch)
        user = _patient("ep3")
        _token(client, user)
        client.post("/api/chat/ask/", {"message": "hello"}, format="json")
        assert ChatMessage.objects.filter(user=user).count() == 2

    def test_the_stored_reply_matches_what_the_user_saw(self, client, monkeypatch):
        _stub_llm(monkeypatch)
        user = _patient("ep4")
        _token(client, user)
        res = client.post("/api/chat/ask/", {"message": "hello"}, format="json")
        stored = ChatMessage.objects.filter(user=user, role="assistant").first()
        assert stored.text == res.data["reply"]

    def test_an_emergency_is_reported_in_the_payload(self, client, monkeypatch):
        _stub_llm(monkeypatch)
        _token(client, _patient("ep5"))
        res = client.post("/api/chat/ask/",
                          {"message": "I have crushing chest pain"}, format="json")
        assert res.data["emergency"]["detected"] is True
        assert res.data["emergency"]["label"] == "chest pain"
        assert "123" in res.data["reply"]

    def test_a_provider_outage_is_a_200_with_degraded_set(self, client, monkeypatch):
        monkeypatch.setattr(llm, "active_provider", lambda: None)
        _token(client, _patient("ep6"))
        res = client.post("/api/chat/ask/", {"message": "hi"}, format="json")
        assert res.status_code == 200
        assert res.data["degraded"] is True

    def test_a_doctor_can_use_the_assistant(self, client, monkeypatch):
        _stub_llm(monkeypatch)
        _token(client, _doctor("ep7doc"))
        res = client.post("/api/chat/ask/", {"message": "summarise my day"},
                          format="json")
        assert res.status_code == 200
        assert res.data["degraded"] is False

    def test_one_user_cannot_read_anothers_exchange(self, client, monkeypatch):
        _stub_llm(monkeypatch)
        alice, bob = _patient("ep8a"), _patient("ep8b")
        _token(client, alice)
        client.post("/api/chat/ask/", {"message": "my private symptom"}, format="json")
        _token(client, bob)
        assert client.get("/api/chat/history/").data == []

    def test_status_reports_availability_without_exposing_keys(self, client, monkeypatch):
        monkeypatch.setattr(llm, "active_provider", lambda: None)
        _token(client, _patient("ep9"))
        res = client.get("/api/chat/status/")
        assert res.status_code == 200
        assert res.data["enabled"] is False
        assert "key" not in str(res.data).lower()

    def test_the_ask_endpoint_has_its_own_throttle_budget(self):
        from appointments.views.chat import ChatAsk
        from django.conf import settings
        assert ChatAsk.throttle_scope == "ai"
        assert "ai" in settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]


# ===========================================================================
# The provider seam
# ===========================================================================
class TestLLMSeam:
    def test_no_configuration_means_no_provider(self):
        assert llm.active_provider() is None
        assert llm.is_enabled() is False
        assert llm.provider_label() == "disabled"

    def test_an_explicit_provider_without_its_key_is_not_selected(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "groq")
        assert llm.active_provider() is None

    def test_auto_prefers_groq_then_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-real-looking-key")
        assert llm.active_provider() == "openai"
        monkeypatch.setenv("GROQ_API_KEY", "or-key")
        assert llm.active_provider() == "groq"

    def test_a_placeholder_key_does_not_count_as_configured(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "your-openai-api-key-here")
        assert llm.active_provider() is None

    def test_an_unconfigured_call_raises_rather_than_returning_prose(self):
        with pytest.raises(llm.LLMUnavailable):
            llm.complete("hello")

    def test_provider_errors_are_wrapped_not_propagated(self, monkeypatch):
        """The pipeline must only ever see LLMUnavailable or LLMFailed."""
        from appointments.services.ai import providers

        monkeypatch.setenv("GROQ_API_KEY", "or-key")

        def boom(request):
            raise providers.RateLimited("upstream on fire")

        monkeypatch.setattr(
            providers.PROVIDERS["groq"], "complete", lambda self, r: boom(r))
        with pytest.raises(llm.LLMFailed):
            llm.complete("hello", system_prompt="be safe")

    def test_an_adapter_bug_is_contained_not_raised_as_a_500(self, monkeypatch):
        from appointments.services.ai import providers

        monkeypatch.setenv("GROQ_API_KEY", "or-key")
        monkeypatch.setattr(providers.PROVIDERS["groq"], "complete",
                            lambda self, r: 1 / 0)
        with pytest.raises(llm.LLMFailed):
            llm.complete("hello")

    def test_the_system_prompt_leads_the_message_list(self, monkeypatch):
        from appointments.services.ai import providers

        monkeypatch.setenv("GROQ_API_KEY", "or-key")
        seen = {}

        def spy(self, request):
            seen["request"] = request
            return providers.ChatResponse(text="ok", provider="groq", model="m")

        monkeypatch.setattr(providers.PROVIDERS["groq"], "complete", spy)
        llm.complete("hello", history=[{"role": "user", "content": "earlier"}],
                     system_prompt="ROSHADA RULES")

        request = seen["request"]
        assert [m.role for m in request.messages] == ["system", "user", "user"]
        assert request.system_prompt == "ROSHADA RULES"
        assert request.messages[1].content == "earlier"
        assert request.messages[-1].content == "hello"

    def test_usage_is_reported_when_the_provider_supplies_it(self, monkeypatch):
        from appointments.services.ai import providers

        monkeypatch.setenv("GROQ_API_KEY", "or-key")
        monkeypatch.setattr(
            providers.PROVIDERS["groq"], "complete",
            lambda self, r: providers.ChatResponse(
                text="ok", provider="groq", model="m",
                usage=providers.Usage(11, 22, 33)))
        assert llm.complete("hi").usage.total_tokens == 33


# ===========================================================================
# The frontend client
# ===========================================================================
class TestFrontendClient:
    """shared/ai.py is now a thin client — but it keeps one guarantee locally."""

    def _client_module(self, monkeypatch, response):
        import shared.ai as module

        class _FakeStreamlit:
            session_state = {}

        monkeypatch.setattr(module, "st", _FakeStreamlit())
        monkeypatch.setattr(module, "api_request", mock.Mock(return_value=response))
        return module

    def test_an_unreachable_backend_still_warns_about_an_emergency(self, monkeypatch):
        """The one message a user must never miss cannot depend on our own API."""
        module = self._client_module(monkeypatch, None)
        answer = module.ask("I have severe chest pain and can't breathe")
        assert answer["emergency"]["detected"] is True
        assert "123" in answer["reply"], "the local emergency number must be shown"
        assert answer["degraded"] is True

    def test_an_unreachable_backend_does_not_invent_an_answer(self, monkeypatch):
        module = self._client_module(monkeypatch, None)
        answer = module.ask("what should I eat?")
        assert answer["emergency"]["detected"] is False
        assert answer["reply"] == module.OFFLINE_REPLY

    def test_a_rate_limit_is_explained_rather_than_shown_as_an_outage(self, monkeypatch):
        response = mock.Mock(status_code=429)
        module = self._client_module(monkeypatch, response)
        assert "wait a minute" in module.ask("hi")["reply"].lower()

    def test_a_successful_call_is_passed_through_untouched(self, monkeypatch):
        payload = {"reply": "hello", "emergency": {"detected": False, "label": None},
                   "sources": [], "warnings": [], "degraded": False}
        response = mock.Mock(status_code=200)
        response.json.return_value = payload
        module = self._client_module(monkeypatch, response)
        assert module.ask("hi") == payload

    def test_the_client_holds_no_provider_credentials(self):
        import inspect

        import shared.ai as module
        source = inspect.getsource(module)
        for secret in ("GROQ_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
            assert secret not in source, f"{secret} is still read on the frontend"

    def test_the_ask_call_outlives_an_ordinary_api_timeout(self):
        """The backend holds an LLM round-trip open inside the request."""
        import shared.ai as module
        from shared.api import API_TIMEOUT
        assert module.ASK_TIMEOUT > API_TIMEOUT
