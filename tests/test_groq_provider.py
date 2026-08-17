"""The Groq provider, and the abstraction it plugs into.

Two things are being proved here, and they are different:

* **Groq works** — the adapter builds the right request, normalises the
  response, and maps every failure onto the shared taxonomy.
* **Nothing above it knows Groq exists** — the same calling code answers
  through the mock, and switching is one environment variable.

Every test is offline. The HTTP layer is stubbed, so the suite never spends a
request, never needs a key, and cannot fail because of someone's network. The
live connection test is a management command (``manage.py ai_check``), which is
where a real call belongs.
"""
import pytest

from appointments.services.ai import llm, providers
from appointments.services.ai.providers import groq as groq_module
from appointments.services.ai.providers import http as http_module
from appointments.services.ai.providers.base import (
    AuthError, ChatRequest, Message, ModelNotFound, ProviderNotConfigured,
    ProviderTimeout, ProviderUnavailable, QuotaExceeded, RateLimited,
)

#: Every environment variable that can influence provider selection. Cleared
#: for each test so the suite never depends on the developer's own .env.
ALL_KEYS = ("AI_PROVIDER", "LLM_PROVIDER", "GROQ_API_KEY", "GROQ_MODEL",
            "GROQ_BASE_URL", "GROQ_TIMEOUT", "GROQ_MODEL_FALLBACKS",
            "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
            "GEMINI_API_KEY", "GEMINI_MODEL")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)


def request_of(text="Explain what RAG is in one short paragraph."):
    return ChatRequest(messages=[Message(providers.USER, text)])


def stub_post(monkeypatch, body=None, error=None):
    """Replace the shared HTTP layer and capture what the adapter sent."""
    captured = {}

    def fake_post_json(url, *, headers, payload, timeout, provider, model):
        captured.update(url=url, headers=headers, payload=payload,
                        timeout=timeout, provider=provider, model=model)
        if error is not None:
            raise error
        return body or {
            "choices": [{"message": {"content": "RAG combines retrieval with "
                                                "generation."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 20,
                      "total_tokens": 32},
        }

    monkeypatch.setattr(http_module, "post_json", fake_post_json)
    return captured


# ---------------------------------------------------------------------------
# Registered on the existing abstraction, not beside it
# ---------------------------------------------------------------------------
class TestRegistration:
    def test_groq_implements_the_existing_provider_interface(self):
        assert issubclass(groq_module.GroqProvider, providers.LLMProvider)
        assert providers.PROVIDERS["groq"] is groq_module.GroqProvider

    def test_openrouter_is_gone(self):
        """It was removed, not left dormant."""
        assert "openrouter" not in providers.PROVIDERS
        assert "openrouter" not in providers.AUTO_ORDER
        import importlib
        with pytest.raises(ImportError):
            importlib.import_module(
                "appointments.services.ai.providers.openrouter")

    def test_no_second_provider_architecture_was_created(self):
        import pathlib
        package = pathlib.Path("appointments/services/ai/providers")
        modules = {p.stem for p in package.glob("*.py")} - {"__init__"}
        assert modules == {"base", "http", "registry", "registry_utils",
                           "openai_compat", "gemini", "groq", "mock"}

    def test_groq_is_the_first_choice_for_auto(self):
        assert providers.AUTO_ORDER[0] == "groq"

    def test_the_provider_only_talks_to_groq(self):
        """Section 2: no database, RAG, appointment, patient or tool logic.

        Checked against the module's actual imports rather than its text —
        prose legitimately mentions "models", and a substring match on that
        would fail for a comment.
        """
        import ast
        import pathlib

        tree = ast.parse(pathlib.Path(
            "appointments/services/ai/providers/groq.py").read_text(
                encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")

        # Only siblings inside the provider package. (ast records a relative
        # import's module without its leading dots; `level` carries those.)
        assert imported == {"base", "openai_compat"}, imported
        for forbidden in ("django", "appointments.models", "knowledge",
                          "records", "comms", "pharmacy", "radiology"):
            assert not any(name.startswith(forbidden) for name in imported), \
                f"groq.py imports {forbidden}"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class TestConfiguration:
    def test_the_key_comes_from_the_environment(self, monkeypatch):
        assert groq_module.GroqProvider.is_configured() is False
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        assert groq_module.GroqProvider.is_configured() is True
        assert groq_module.GroqProvider().api_key == "gsk-test-value"

    def test_no_key_is_hardcoded_anywhere_in_the_package(self):
        """Section 24: the real key must exist only in the environment."""
        import pathlib
        import re
        for path in pathlib.Path("appointments/services/ai").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            assert not re.search(r"gsk_[A-Za-z0-9]{20,}", text), \
                f"{path} contains something shaped like a Groq key"

    def test_no_key_is_committed_in_the_example_file(self):
        import pathlib
        import re
        text = pathlib.Path(".env.example").read_text(encoding="utf-8")
        assert not re.search(r"gsk_[A-Za-z0-9]{20,}", text)
        assert "GROQ_API_KEY=your-groq-api-key-here" in text

    def test_the_model_comes_from_configuration(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        assert groq_module.GroqProvider().default_model == "openai/gpt-oss-20b"
        monkeypatch.setenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        assert groq_module.GroqProvider().default_model == \
            "llama-3.3-70b-versatile"

    def test_the_base_url_is_groq(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        assert groq_module.GroqProvider().base_url == \
            "https://api.groq.com/openai/v1"

    def test_a_placeholder_key_counts_as_unconfigured(self, monkeypatch):
        """A verbatim copy of .env.example must disable the provider cleanly
        rather than 401 at call time."""
        monkeypatch.setenv("GROQ_API_KEY", "your-groq-api-key-here")
        assert groq_module.GroqProvider.is_configured() is False


# ---------------------------------------------------------------------------
# Provider switching — the abstraction's whole point
# ---------------------------------------------------------------------------
class TestProviderSwitching:
    def test_selecting_groq(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        assert llm.active_provider() == "groq"

    def test_selecting_the_mock(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "mock")
        assert llm.active_provider() == "mock"

    def test_llm_provider_is_accepted_as_an_alias(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        assert llm.active_provider() == "mock"

    def test_ai_provider_wins_when_both_are_set(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("LLM_PROVIDER", "mock")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        assert llm.active_provider() == "groq"

    def test_the_same_call_answers_through_either_provider(self, monkeypatch):
        """Nothing above the provider changes between the two."""
        monkeypatch.setenv("AI_PROVIDER", "mock")
        mock_reply = llm.complete("What is RAG?")
        assert mock_reply.provider == "mock"

        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        stub_post(monkeypatch)
        groq_reply = llm.complete("What is RAG?")
        assert groq_reply.provider == "groq"

        # Same shape from both — which is what lets the caller not care.
        for reply in (mock_reply, groq_reply):
            assert isinstance(reply.text, str) and reply.text
            assert reply.model

    def test_the_mock_is_never_chosen_automatically(self, monkeypatch):
        """A mock winning by default would serve invented medical text."""
        monkeypatch.setenv("AI_PROVIDER", "auto")
        assert llm.active_provider() is None
        assert "mock" not in providers.AUTO_ORDER

    def test_auto_selects_groq_when_it_has_a_key(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "auto")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        assert llm.active_provider() == "groq"

    def test_a_named_provider_is_never_silently_swapped(self, monkeypatch):
        """Asked for Groq with no key: report unavailable, do not use Gemini."""
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
        assert llm.active_provider() is None


# ---------------------------------------------------------------------------
# The request Groq actually receives
# ---------------------------------------------------------------------------
class TestRequestShape:
    def test_the_request_goes_to_groq_with_the_configured_model(self,
                                                                monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        captured = stub_post(monkeypatch)
        groq_module.GroqProvider().complete(request_of())

        assert captured["url"] == \
            "https://api.groq.com/openai/v1/chat/completions"
        assert captured["payload"]["model"] == "openai/gpt-oss-20b"
        assert captured["payload"]["messages"][0]["content"].startswith("Explain")

    def test_the_key_is_sent_as_a_bearer_token(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        captured = stub_post(monkeypatch)
        groq_module.GroqProvider().complete(request_of())
        assert captured["headers"]["Authorization"] == "Bearer gsk-test-value"

    def test_a_timeout_is_always_set(self, monkeypatch):
        """Section 12: a request must never hang indefinitely."""
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        captured = stub_post(monkeypatch)
        groq_module.GroqProvider().complete(request_of())
        assert captured["timeout"] and captured["timeout"] > 0

    def test_the_system_prompt_and_context_reach_the_request(self,
                                                             monkeypatch):
        """Section 17: the shape RAG will later fill — system + context + user."""
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        captured = stub_post(monkeypatch)
        llm.complete("What does the source say?",
                     system_prompt="Answer only from the sources below.\n[1] ...")
        roles = [m["role"] for m in captured["payload"]["messages"]]
        assert roles[0] == "system"
        assert roles[-1] == "user"

    def test_history_sits_between_system_and_the_new_question(self,
                                                              monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        captured = stub_post(monkeypatch)
        llm.complete("And after that?", system_prompt="You are helpful.",
                     history=[{"role": "user", "content": "First question"},
                              {"role": "assistant", "content": "First answer"}])
        assert [m["role"] for m in captured["payload"]["messages"]] == \
            ["system", "user", "assistant", "user"]


# ---------------------------------------------------------------------------
# Response normalisation
# ---------------------------------------------------------------------------
class TestResponseNormalisation:
    def test_the_response_is_normalised_to_the_shared_shape(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        stub_post(monkeypatch)
        response = groq_module.GroqProvider().complete(request_of())

        assert response.text == "RAG combines retrieval with generation."
        assert response.provider == "groq"
        assert response.model == "openai/gpt-oss-20b"
        assert response.usage.total_tokens == 32
        assert response.finish_reason == "stop"

    def test_a_malformed_body_is_an_invalid_response_not_a_crash(self,
                                                                 monkeypatch):
        from appointments.services.ai.providers.base import InvalidResponse
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        stub_post(monkeypatch, body={"unexpected": "shape"})
        with pytest.raises(InvalidResponse):
            groq_module.GroqProvider().complete(request_of())


# ---------------------------------------------------------------------------
# Failures — controlled, and never leaking the credential
# ---------------------------------------------------------------------------
class TestFailureHandling:
    def test_a_missing_key_is_a_configuration_error(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "groq")
        with pytest.raises(llm.LLMUnavailable):
            llm.complete("hello")

    def test_the_provider_names_the_missing_variable(self, monkeypatch):
        with pytest.raises(ProviderNotConfigured, match="GROQ_API_KEY"):
            groq_module.GroqProvider().api_key

    @pytest.mark.parametrize("error", [
        AuthError("401 unauthorized"),
        RateLimited("429 too many requests"),
        QuotaExceeded("402 payment required"),
        ProviderTimeout("read timed out"),
        ProviderUnavailable("503 service unavailable"),
    ])
    def test_every_provider_failure_becomes_one_controlled_error(
            self, monkeypatch, error):
        """Section 10/11: the backend must not crash, and must not leak."""
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-secret-value")
        stub_post(monkeypatch, error=error)
        with pytest.raises(llm.LLMFailed) as raised:
            llm.complete("hello")
        # The caller learns which provider failed, never why in provider terms.
        assert "gsk-secret-value" not in str(raised.value)

    def test_an_unknown_model_is_reported_as_such(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        stub_post(monkeypatch, error=ModelNotFound("no such model"))
        with pytest.raises(ModelNotFound):
            groq_module.GroqProvider().complete(request_of())

    def test_the_key_never_appears_in_a_log_line(self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-secret-value")
        stub_post(monkeypatch, error=AuthError("401 unauthorized"))
        with caplog.at_level(logging.DEBUG, logger="appointments"):
            with pytest.raises(llm.LLMFailed):
                llm.complete("hello")
        assert "gsk-secret-value" not in caplog.text

    def test_the_key_never_appears_in_the_status_payload(self, monkeypatch):
        """Section 19: nothing the frontend can read carries the credential."""
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-secret-value")
        described = str(llm.describe())
        assert "gsk-secret-value" not in described
        assert "groq" in described


# ---------------------------------------------------------------------------
# Input handling (section 14, cases 4 and 5)
# ---------------------------------------------------------------------------
class TestInputs:
    def test_an_empty_input_still_produces_a_controlled_result(self,
                                                               monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "mock")
        response = llm.complete("")
        assert isinstance(response.text, str) and response.text

    def test_a_very_short_input_is_passed_through_unchanged(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        captured = stub_post(monkeypatch)
        llm.complete("hi")
        assert captured["payload"]["messages"][-1]["content"] == "hi"

    def test_arabic_is_sent_through_unchanged(self, monkeypatch):
        """Section 14: the provider must not mangle non-Latin script."""
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        captured = stub_post(monkeypatch)
        llm.complete("ما هو نظام RAG؟")
        assert captured["payload"]["messages"][-1]["content"] == "ما هو نظام RAG؟"

    def test_a_bilingual_question_is_sent_through_unchanged(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        captured = stub_post(monkeypatch)
        llm.complete("Explain RAG بالعربي في جملة واحدة.")
        sent = captured["payload"]["messages"][-1]["content"]
        assert "بالعربي" in sent and "Explain RAG" in sent


# ---------------------------------------------------------------------------
# The mock provider
# ---------------------------------------------------------------------------
class TestMockProvider:
    def test_it_needs_no_credential(self):
        assert providers.PROVIDERS["mock"].is_configured() is True

    def test_it_says_that_it_is_a_mock(self, monkeypatch):
        """A mock indistinguishable from a real answer will be mistaken for one."""
        monkeypatch.setenv("AI_PROVIDER", "mock")
        response = llm.complete("What is hypertension?")
        assert "mock provider" in response.text.lower()
        assert "no language model was called" in response.text.lower()

    def test_it_echoes_the_question_so_the_seam_is_testable(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "mock")
        response = llm.complete("What is RAG?")
        assert "What is RAG?" in response.text

    def test_it_reports_no_token_usage(self, monkeypatch):
        """Nothing was spent; a made-up count would pollute the usage logs."""
        monkeypatch.setenv("AI_PROVIDER", "mock")
        assert llm.complete("hello").usage.total_tokens is None

    def test_it_makes_no_network_call(self, monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError("the mock must not touch the network")

        monkeypatch.setattr(http_module, "post_json", explode)
        monkeypatch.setenv("AI_PROVIDER", "mock")
        assert llm.complete("hello").provider == "mock"


# ---------------------------------------------------------------------------
# Health reporting
# ---------------------------------------------------------------------------
class TestHealth:
    def test_describe_reports_the_active_provider_and_model(self, monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        described = llm.describe()
        assert described["enabled"] is True
        assert described["provider"] == "groq"
        assert described["model"] == "openai/gpt-oss-20b"
        assert "Groq" in described["label"]

    def test_an_unconfigured_platform_reports_itself_disabled(self,
                                                              monkeypatch):
        monkeypatch.setenv("AI_PROVIDER", "auto")
        described = llm.describe()
        assert described["enabled"] is False
        assert described["provider"] is None

    def test_the_platform_still_works_when_groq_is_down(self, monkeypatch):
        """Section 21: an unavailable provider must not break Roshada."""
        from appointments.services.ai import pipeline
        monkeypatch.setenv("AI_PROVIDER", "groq")
        monkeypatch.setenv("GROQ_API_KEY", "gsk-test-value")
        stub_post(monkeypatch, error=ProviderUnavailable("503"))

        import django.contrib.auth.models as auth_models
        user = auth_models.User(username="groq_health_probe")
        # The pipeline degrades rather than raising, so a chat UI still renders.
        reply = pipeline.ask(user, "What is a healthy blood pressure?")
        assert reply.degraded is True
        assert reply.reply
        assert "gsk-test-value" not in reply.reply


pytestmark = pytest.mark.django_db
