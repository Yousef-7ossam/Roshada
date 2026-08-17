"""TASK 03 — the LLM provider abstraction.

    Application → LLM Service → Provider Adapter → LLM Provider

No test here makes a network call: the shared HTTP core (`providers.http`) is
stubbed, so each adapter is exercised against a fake upstream that returns the
real wire shapes. Provider *switching* is driven purely through environment
variables, which is how it works in production.

Ports the four tests that lived in `tests/test_openai_provider.py`, plus the
Groq message-ordering test from `tests/test_improvements.py`.
"""
import pytest
import requests

from appointments.services.ai import llm, providers
from appointments.services.ai.providers import gemini as gemini_mod
from appointments.services.ai.providers import http as http_mod
from appointments.services.ai.providers import openai_compat, groq

ALL_KEYS = ("AI_PROVIDER", "GROQ_API_KEY", "OPENAI_API_KEY",
            "GEMINI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
            "GROQ_MODEL", "GEMINI_MODEL", "OPENAI_MODEL_FALLBACKS",
            "GROQ_MODEL_FALLBACKS", "AI_TIMEOUT", "OPENAI_TIMEOUT",
            "GROQ_TIMEOUT", "GEMINI_TIMEOUT", "GEMINI_BASE_URL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Nothing in the developer's .env may influence provider selection."""
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# A fake upstream
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


OPENAI_BODY = {
    "choices": [{"message": {"content": "Hello from the model."},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
}

GEMINI_BODY = {
    "candidates": [{"content": {"parts": [{"text": "Hello from Gemini."}],
                                "role": "model"},
                    "finishReason": "STOP"}],
    "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 4,
                      "totalTokenCount": 12},
}


@pytest.fixture
def upstream(monkeypatch):
    """Capture what the adapter sent, and control what comes back."""
    calls = []
    queue = []

    class _Session:
        def post(self, url, headers=None, json=None, timeout=None):
            calls.append({"url": url, "headers": headers or {},
                          "payload": json or {}, "timeout": timeout})
            if queue:
                nxt = queue.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return nxt
            return FakeResponse(200, OPENAI_BODY)

    monkeypatch.setattr(http_mod, "session", lambda: _Session())
    return type("Upstream", (), {"calls": calls, "queue": queue})()


# ===========================================================================
# Provider switching — the headline requirement
# ===========================================================================
class TestProviderSwitching:
    def test_nothing_configured_means_no_provider(self):
        assert providers.selected_name() is None
        assert providers.resolve() is None
        assert llm.is_enabled() is False

    @pytest.mark.parametrize("env_key,expected", [
        ("GROQ_API_KEY", "groq"),
        ("OPENAI_API_KEY", "openai"),
        ("GEMINI_API_KEY", "gemini"),
    ])
    def test_auto_selects_whichever_single_provider_is_configured(
            self, monkeypatch, env_key, expected):
        monkeypatch.setenv(env_key, "sk-a-real-looking-value")
        assert providers.selected_name() == expected

    def test_auto_prefers_groq_then_openai_then_gemini(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "AIzaSomething")
        assert providers.selected_name() == "gemini"
        monkeypatch.setenv("OPENAI_API_KEY", "sk-something")
        assert providers.selected_name() == "openai"
        monkeypatch.setenv("GROQ_API_KEY", "sk-or-v1-something")
        assert providers.selected_name() == "groq"

    @pytest.mark.parametrize("choice,expected", [
        ("groq", "groq"), ("openai", "openai"),
        ("gemini", "gemini"), ("local", "openai"),
        ("ollama", "openai"), ("tokenrouter", "openai"),
        ("Groq", "groq"), ("  gemini  ", "gemini"),
    ])
    def test_an_explicit_choice_overrides_the_auto_order(
            self, monkeypatch, choice, expected):
        for key in ("GROQ_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
            monkeypatch.setenv(key, "sk-a-real-looking-value")
        monkeypatch.setenv("AI_PROVIDER", choice)
        assert providers.selected_name() == expected

    def test_an_explicit_provider_without_a_key_is_never_silently_swapped(
            self, monkeypatch):
        """Sending a patient's messages to a company the operator did not choose
        is a privacy decision, not a fallback."""
        monkeypatch.setenv("GROQ_API_KEY", "sk-or-v1-real")
        monkeypatch.setenv("AI_PROVIDER", "gemini")
        assert providers.selected_name() is None
        assert llm.is_enabled() is False

    def test_an_unknown_provider_name_disables_rather_than_guessing(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "sk-or-v1-real")
        monkeypatch.setenv("AI_PROVIDER", "wizard")
        assert providers.selected_name() is None

    def test_switching_takes_effect_without_a_restart(self, monkeypatch):
        """Selection is resolved per call, never cached at import."""
        assert providers.selected_name() is None
        monkeypatch.setenv("OPENAI_API_KEY", "sk-added-at-runtime")
        assert providers.selected_name() == "openai"
        monkeypatch.delenv("OPENAI_API_KEY")
        assert providers.selected_name() is None

    def test_available_lists_every_configured_provider(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")
        assert providers.available() == ["openai", "gemini"]

    def test_the_service_reports_the_active_provider_and_model(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "sk-or-v1-real")
        monkeypatch.setenv("GROQ_MODEL", "meta-llama/llama-3.1-8b-instruct")
        described = llm.describe()
        assert described["enabled"] is True
        assert described["provider"] == "groq"
        assert described["model"] == "meta-llama/llama-3.1-8b-instruct"
        assert "Groq" in described["label"]

    def test_status_never_exposes_a_credential(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "sk-or-v1-SUPERSECRET")
        assert "SUPERSECRET" not in str(llm.describe())


# ===========================================================================
# Credential hygiene
# ===========================================================================
class TestCredentials:
    @pytest.mark.parametrize("value", [
        "your-groq-api-key-here", "your-openai-api-key-here",
        "your-gemini-api-key-here", "CHANGE-ME", "replace-me", "",
    ])
    def test_placeholder_values_are_not_credentials(self, value):
        assert providers.is_placeholder(value) or not value

    @pytest.mark.parametrize("value", [
        "sk-or-v1-abc123", "sk-proj-abc123", "AIzaSyAbc123", "ollama",
    ])
    def test_real_looking_keys_are_accepted(self, value):
        assert providers.is_placeholder(value) is False

    @pytest.mark.parametrize("env_key", [
        "GROQ_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"])
    def test_every_provider_rejects_the_example_placeholder(self, monkeypatch, env_key):
        """A verbatim copy of .env.example used to make Groq look
        configured, so the assistant selected it and 401'd at call time."""
        monkeypatch.setenv(env_key, "your-provider-api-key-here")
        assert providers.selected_name() is None

    def test_no_api_key_is_hardcoded_anywhere_in_the_provider_layer(self):
        import inspect
        import pathlib
        import re

        # A real credential, not a prose mention of the prefix: the marker must
        # be followed by a run of key-shaped characters.
        key_like = re.compile(r"(sk-or-v1-|sk-proj-|sk-ant-|AIzaSy)[A-Za-z0-9_\-]{8,}")

        package = pathlib.Path(inspect.getfile(providers)).parent
        for path in package.glob("*.py"):
            match = key_like.search(path.read_text(encoding="utf-8"))
            assert match is None, f"{path.name} contains a literal key"

    def test_credentials_are_only_ever_read_from_the_environment(self):
        import inspect
        import pathlib

        package = pathlib.Path(inspect.getfile(providers)).parent
        for path in package.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            if "_API_KEY" not in source or path.name == "registry_utils.py":
                continue
            # A module either reads the environment itself, or declares
            # ``key_var`` and inherits the reading from the OpenAI-compatible
            # base — which is what a thin adapter like Groq's does, and is the
            # point of having a base class. What must never appear is a
            # literal credential.
            assert "os.environ" in source or "key_var =" in source, \
                f"{path.name} references a key without reading the environment"


# ===========================================================================
# OpenAI-compatible adapter (OpenAI · TokenRouter · local models)
# ===========================================================================
class TestOpenAICompatible:
    def _provider(self, monkeypatch, **env):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return openai_compat.OpenAICompatibleProvider()

    def test_it_posts_chat_completions_with_a_bearer_token(self, monkeypatch, upstream):
        provider = self._provider(monkeypatch)
        provider.complete(providers.ChatRequest(
            messages=[providers.Message("user", "hi")]))
        call = upstream.calls[0]
        assert call["url"] == "https://api.openai.com/v1/chat/completions"
        assert call["headers"]["Authorization"] == "Bearer sk-test"

    def test_the_system_prompt_is_sent_as_the_first_message(self, monkeypatch, upstream):
        provider = self._provider(monkeypatch)
        provider.complete(providers.ChatRequest(messages=[
            providers.Message("system", "RULES"),
            providers.Message("user", "earlier"),
            providers.Message("assistant", "noted"),
            providers.Message("user", "hi")]))
        sent = upstream.calls[0]["payload"]["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
        assert sent[0]["content"] == "RULES"

    def test_it_reads_the_reply_and_the_token_usage(self, monkeypatch, upstream):
        provider = self._provider(monkeypatch)
        response = provider.complete(providers.ChatRequest(
            messages=[providers.Message("user", "hi")]))
        assert response.text == "Hello from the model."
        assert response.usage.total_tokens == 17
        assert response.finish_reason == "stop"

    def test_content_arrays_are_flattened(self):
        """Ported from tests/test_openai_provider.py — some gateways return the
        content-array form instead of a plain string."""
        payload = {"choices": [{"message": {"content": [
            {"type": "text", "text": "Hello"},
            {"type": "text", "text": " world"}]}}]}
        assert openai_compat._extract_content(payload) == "Hello world"

    def test_a_local_model_is_this_adapter_with_another_base_url(
            self, monkeypatch, upstream):
        """Ollama, LM Studio and vLLM all serve the same API."""
        provider = self._provider(monkeypatch,
                                  OPENAI_BASE_URL="http://localhost:11434/v1",
                                  OPENAI_MODEL="llama3.1")
        provider.complete(providers.ChatRequest(
            messages=[providers.Message("user", "hi")]))
        call = upstream.calls[0]
        assert call["url"] == "http://localhost:11434/v1/chat/completions"
        assert call["payload"]["model"] == "llama3.1"

    # -- model configuration ------------------------------------------------
    def test_the_model_comes_from_the_environment(self, monkeypatch):
        provider = self._provider(monkeypatch, OPENAI_MODEL="gpt-4.1-mini")
        assert provider.default_model == "gpt-4.1-mini"

    def test_an_explicit_model_beats_the_environment(self, monkeypatch):
        provider = self._provider(monkeypatch, OPENAI_MODEL="env-model")
        assert provider.candidate_models("call-model")[0] == "call-model"

    def test_configured_fallbacks_follow_the_chosen_model(self, monkeypatch):
        """Ported. Behaviour change (audit F-B1): the hardcoded 'gpt-4o-mini'
        is no longer appended — fallbacks are exactly what the operator set."""
        provider = self._provider(monkeypatch,
                                  OPENAI_MODEL_FALLBACKS="model-b,model-c")
        assert provider.candidate_models("model-a") == ["model-a", "model-b", "model-c"]

    def test_no_model_is_ever_billed_that_nobody_configured(self, monkeypatch):
        provider = self._provider(monkeypatch, OPENAI_MODEL="only-this-one")
        assert provider.candidate_models() == ["only-this-one"]


# ===========================================================================
# Groq adapter
# ===========================================================================
class TestGroq:
    def _provider(self, monkeypatch, **env):
        monkeypatch.setenv("GROQ_API_KEY", "sk-or-v1-test")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return groq.GroqProvider()

    def test_it_targets_the_groq_endpoint(self, monkeypatch, upstream):
        """Replaces the old OpenRouter attribution-header test.

        ``X-Title`` and ``HTTP-Referer`` were OpenRouter's integrator
        requirement and went with that adapter. Groq needs only the bearer
        token, and asserting their absence keeps the removal honest.
        """
        provider = self._provider(monkeypatch)
        provider.complete(providers.ChatRequest(
            messages=[providers.Message("user", "hi")]))
        call = upstream.calls[0]
        assert call["url"] == "https://api.groq.com/openai/v1/chat/completions"
        assert call["headers"]["Authorization"] == "Bearer sk-or-v1-test"
        assert "X-Title" not in call["headers"]
        assert "HTTP-Referer" not in call["headers"]

    def test_history_sits_between_the_system_prompt_and_the_new_question(
            self, monkeypatch, upstream):
        """Ported from tests/test_improvements.py."""
        provider = self._provider(monkeypatch)
        provider.complete(providers.ChatRequest(messages=[
            providers.Message("system", "RULES"),
            providers.Message("user", "earlier"),
            providers.Message("user", "follow-up?")]))
        sent = upstream.calls[0]["payload"]["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "user"]
        assert sent[1]["content"] == "earlier"

    def test_it_uses_its_own_key_not_the_openai_one(self, monkeypatch, upstream):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-the-wrong-key")
        provider = self._provider(monkeypatch)
        provider.complete(providers.ChatRequest(
            messages=[providers.Message("user", "hi")]))
        assert upstream.calls[0]["headers"]["Authorization"] == "Bearer sk-or-v1-test"


# ===========================================================================
# Gemini adapter — a genuinely different wire format
# ===========================================================================
class TestGemini:
    def _provider(self, monkeypatch, **env):
        monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return gemini_mod.GeminiProvider()

    def test_it_translates_to_contents_and_parts(self, monkeypatch, upstream):
        upstream.queue.append(FakeResponse(200, GEMINI_BODY))
        provider = self._provider(monkeypatch)
        provider.complete(providers.ChatRequest(messages=[
            providers.Message("system", "RULES"),
            providers.Message("user", "earlier"),
            providers.Message("assistant", "noted"),
            providers.Message("user", "hi")]))

        payload = upstream.calls[0]["payload"]
        # Gemini calls the assistant "model" and has no system role.
        assert [c["role"] for c in payload["contents"]] == ["user", "model", "user"]
        assert payload["contents"][0]["parts"][0]["text"] == "earlier"
        assert payload["systemInstruction"]["parts"][0]["text"] == "RULES"
        assert "messages" not in payload

    def test_the_key_travels_in_a_header_not_the_url(self, monkeypatch, upstream):
        """A credential in a query string lands in proxy logs."""
        upstream.queue.append(FakeResponse(200, GEMINI_BODY))
        provider = self._provider(monkeypatch)
        provider.complete(providers.ChatRequest(
            messages=[providers.Message("user", "hi")]))
        call = upstream.calls[0]
        assert call["headers"]["x-goog-api-key"] == "AIza-test"
        assert "AIza-test" not in call["url"]

    def test_it_reads_the_reply_and_the_token_usage(self, monkeypatch, upstream):
        upstream.queue.append(FakeResponse(200, GEMINI_BODY))
        provider = self._provider(monkeypatch)
        response = provider.complete(providers.ChatRequest(
            messages=[providers.Message("user", "hi")]))
        assert response.text == "Hello from Gemini."
        assert response.usage.total_tokens == 12

    def test_the_sdk_style_models_prefix_is_accepted(self, monkeypatch, upstream):
        upstream.queue.append(FakeResponse(200, GEMINI_BODY))
        provider = self._provider(monkeypatch, GEMINI_MODEL="models/gemini-2.0-flash")
        provider.complete(providers.ChatRequest(
            messages=[providers.Message("user", "hi")]))
        assert upstream.calls[0]["url"].endswith(
            "/models/gemini-2.0-flash:generateContent")

    def test_a_safety_blocked_prompt_is_an_error_not_an_empty_bubble(
            self, monkeypatch, upstream):
        upstream.queue.append(FakeResponse(
            200, {"promptFeedback": {"blockReason": "SAFETY"}}))
        provider = self._provider(monkeypatch)
        with pytest.raises(providers.InvalidResponse):
            provider.complete(providers.ChatRequest(
                messages=[providers.Message("user", "hi")]))

    def test_it_does_not_import_streamlit(self):
        """The old Gemini client imported Streamlit at module scope, so it could
        not be used from a Django worker at all."""
        import inspect
        source = inspect.getsource(gemini_mod)
        assert "streamlit" not in source


# ===========================================================================
# Error taxonomy, timeouts and failure handling
# ===========================================================================
class TestErrorHandling:
    def _call(self, monkeypatch, upstream, response):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        upstream.queue.append(response)
        return openai_compat.OpenAICompatibleProvider().complete(
            providers.ChatRequest(messages=[providers.Message("user", "hi")]))

    @pytest.mark.parametrize("status,payload,expected", [
        (401, {"error": {"message": "bad key"}}, providers.AuthError),
        (403, {"error": {"message": "forbidden"}}, providers.AuthError),
        (402, {"error": {"message": "no credit"}}, providers.QuotaExceeded),
        (429, {"error": {"message": "slow down"}}, providers.RateLimited),
        (404, {"error": {"message": "not found"}}, providers.ModelNotFound),
        (400, {"error": {"message": "model xyz does not exist"}},
         providers.ModelNotFound),
        (500, {"error": {"message": "boom"}}, providers.ProviderUnavailable),
        (503, {"error": {"message": "down"}}, providers.ProviderUnavailable),
    ])
    def test_status_codes_map_to_one_taxonomy(self, monkeypatch, upstream,
                                              status, payload, expected):
        with pytest.raises(expected):
            self._call(monkeypatch, upstream, FakeResponse(status, payload))

    def test_a_bad_key_reported_as_400_is_still_an_auth_error(
            self, monkeypatch, upstream):
        """Gemini answers 400 'API key not valid' rather than 401 — the operator
        must be sent to the key, not to the model id."""
        with pytest.raises(providers.AuthError):
            self._call(monkeypatch, upstream, FakeResponse(
                400, {"error": {"message": "API key not valid."}}))

    def test_a_timeout_is_its_own_category(self, monkeypatch, upstream):
        with pytest.raises(providers.ProviderTimeout):
            self._call(monkeypatch, upstream, requests.exceptions.Timeout())

    def test_an_unreachable_provider_is_its_own_category(self, monkeypatch, upstream):
        with pytest.raises(providers.ProviderUnavailable):
            self._call(monkeypatch, upstream,
                       requests.exceptions.ConnectionError("no route"))

    def test_a_non_json_body_is_an_invalid_response(self, monkeypatch, upstream):
        with pytest.raises(providers.InvalidResponse):
            self._call(monkeypatch, upstream, FakeResponse(200, None, text="<html>"))

    # -- the F-B1 fix -------------------------------------------------------
    def test_only_a_missing_model_triggers_the_next_candidate(
            self, monkeypatch, upstream):
        monkeypatch.setenv("OPENAI_MODEL_FALLBACKS", "model-b")
        upstream.queue.append(FakeResponse(404, {"error": {"message": "no such model"}}))
        upstream.queue.append(FakeResponse(200, OPENAI_BODY))
        response = self._call(monkeypatch, upstream, FakeResponse(200, OPENAI_BODY))
        assert response.model == "model-b"
        assert len(upstream.calls) == 2

    @pytest.mark.parametrize("status,payload", [
        (401, {"error": {"message": "bad key"}}),
        (402, {"error": {"message": "no credit"}}),
        (429, {"error": {"message": "slow down"}}),
    ])
    def test_an_unrecoverable_error_is_never_retried_across_models(
            self, monkeypatch, upstream, status, payload):
        """It used to catch bare Exception per candidate, so one bad key meant a
        billable sweep of every model and a misleading 'model' error."""
        monkeypatch.setenv("OPENAI_MODEL_FALLBACKS", "model-b,model-c,model-d")
        upstream.queue.append(FakeResponse(status, payload))
        with pytest.raises(providers.ProviderError):
            self._call(monkeypatch, upstream, FakeResponse(200, OPENAI_BODY))
        assert len(upstream.calls) == 1, "a hopeless request was retried"

    # -- timeouts -----------------------------------------------------------
    def test_timeout_precedence_is_explicit_then_provider_then_global(self, monkeypatch):
        monkeypatch.setenv("AI_TIMEOUT", "11")
        assert http_mod.timeout_for(None, "OPENAI_TIMEOUT") == 11
        monkeypatch.setenv("OPENAI_TIMEOUT", "22")
        assert http_mod.timeout_for(None, "OPENAI_TIMEOUT") == 22
        assert http_mod.timeout_for(33, "OPENAI_TIMEOUT") == 33

    def test_a_default_timeout_always_applies(self, monkeypatch):
        assert http_mod.timeout_for(None, "OPENAI_TIMEOUT") == http_mod.DEFAULT_TIMEOUT

    def test_a_malformed_timeout_falls_back_instead_of_crashing(self, monkeypatch):
        monkeypatch.setenv("OPENAI_TIMEOUT", "soon-ish")
        assert http_mod.timeout_for(None, "OPENAI_TIMEOUT") == http_mod.DEFAULT_TIMEOUT

    def test_every_request_carries_the_resolved_timeout(self, monkeypatch, upstream):
        monkeypatch.setenv("AI_TIMEOUT", "7")
        self._call(monkeypatch, upstream, FakeResponse(200, OPENAI_BODY))
        assert upstream.calls[0]["timeout"] == 7

    def test_every_adapter_shares_one_retry_policy(self):
        """Resilience used to differ per provider: Groq retried, OpenAI
        did not, Gemini had neither retry nor timeout."""
        adapter = http_mod.session().get_adapter("https://example.com")
        retries = adapter.max_retries
        assert set(retries.status_forcelist) == set(http_mod.RETRY_STATUSES)
        assert retries.total == 2


# ===========================================================================
# Capabilities
# ===========================================================================
class TestCapabilities:
    @pytest.mark.parametrize("name", ["groq", "openai", "gemini"])
    def test_every_provider_declares_its_capabilities(self, name):
        caps = providers.PROVIDERS[name].capabilities
        assert isinstance(caps.streaming, bool)
        assert isinstance(caps.tools, bool)

    def test_nothing_claims_a_capability_it_does_not_implement(self):
        """A flag is a promise. Streaming is still unimplemented everywhere.

        Tool calling now is implemented — but only by the adapters that
        actually serialise tools onto the request. An adapter claiming it
        without that code would silently drop the tools and look like a model
        choosing not to use them.
        """
        import inspect

        for name, provider in providers.PROVIDERS.items():
            assert provider.capabilities.streaming is False, name
            if not provider.capabilities.tools:
                continue
            source = inspect.getsource(provider)
            if "_payload" not in source:
                # Inherited: the parent must be the one doing the work.
                source = inspect.getsource(provider.__mro__[1])
            assert '"tools"' in source, f"{name} claims tools but never sends any"

    def test_the_mock_does_not_claim_tool_support(self):
        """So the tool-free fallback path is exercised by the suite."""
        assert providers.MockProvider.capabilities.tools is False

    def test_every_registered_provider_implements_the_contract(self):
        for name, provider in providers.PROVIDERS.items():
            assert issubclass(provider, providers.LLMProvider), name
            assert provider.name == name
            assert provider.label
