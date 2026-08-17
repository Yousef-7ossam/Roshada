"""Google Gemini, over the REST API.

Written against ``generativelanguage.googleapis.com`` rather than the
``google-generativeai`` SDK, which fixes three problems at once:

* the SDK is **end of life** — it prints "All support for the
  `google.generativeai` package has ended" on import;
* the old module imported **Streamlit** at module scope and cached the client
  with ``@st.cache_resource``, so it could not be imported from a Django worker
  at all;
* it shared no retry or timeout policy with the other providers (D-7).

Gemini is also the reason the abstraction earns its keep: its wire format has
nothing in common with ``/chat/completions``. Instructions go in a top-level
``systemInstruction`` rather than a message, turns are ``contents`` with
``parts``, and the assistant role is called ``model``. Translating that is
exactly what an adapter is for.
"""
import os

from . import http
from .base import (
    ASSISTANT, Capabilities, ChatResponse, InvalidResponse, LLMProvider,
    ProviderNotConfigured, Usage,
)
from .registry_utils import is_placeholder

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.0-flash"


def _to_contents(messages):
    """Conversation turns in Gemini's shape.

    Gemini names the assistant role ``model``, and has no ``system`` role — the
    system prompt is passed separately.
    """
    contents = []
    for message in messages:
        role = "model" if message.role == ASSISTANT else "user"
        contents.append({"role": role, "parts": [{"text": message.content}]})
    return contents


def _extract_text(body):
    candidates = body.get("candidates") or []
    if not candidates:
        # A prompt blocked by Gemini's own safety filters returns no candidate.
        # Surface it as an unreadable answer so validation withholds it, rather
        # than showing the patient an empty bubble.
        feedback = body.get("promptFeedback") or {}
        raise InvalidResponse(
            f"Gemini returned no candidate (promptFeedback={feedback})")

    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts
                   if isinstance(part, dict))


def _extract_usage(body):
    usage = body.get("usageMetadata") or {}
    return Usage(
        prompt_tokens=usage.get("promptTokenCount"),
        completion_tokens=usage.get("candidatesTokenCount"),
        total_tokens=usage.get("totalTokenCount"),
    )


class GeminiProvider(LLMProvider):
    name = "gemini"
    label = "Google Gemini"
    # Gemini does have a system instruction, just not as a message role.
    capabilities = Capabilities(streaming=False, tools=False,
                                native_system_prompt=True)

    key_var = "GEMINI_API_KEY"
    timeout_var = "GEMINI_TIMEOUT"

    @classmethod
    def is_configured(cls):
        key = os.environ.get(cls.key_var) or ""
        return bool(key) and not is_placeholder(key)

    @property
    def api_key(self):
        key = os.environ.get(self.key_var) or ""
        if not key:
            raise ProviderNotConfigured(f"{self.key_var} is not set.")
        if is_placeholder(key):
            raise ProviderNotConfigured(
                f"{self.key_var} is still the .env.example placeholder.")
        return key

    @property
    def base_url(self):
        return (os.environ.get("GEMINI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")

    @property
    def default_model(self):
        # Accept both "gemini-2.0-flash" and the SDK's "models/gemini-2.0-flash".
        return (os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL).removeprefix("models/")

    def complete(self, request):
        model = (request.model or self.default_model).removeprefix("models/")
        timeout = http.timeout_for(request.timeout, self.timeout_var)

        payload = {
            "contents": _to_contents(request.conversation),
            "generationConfig": {"temperature": request.temperature},
        }
        if request.system_prompt:
            payload["systemInstruction"] = {
                "parts": [{"text": request.system_prompt}]}
        if request.max_tokens:
            payload["generationConfig"]["maxOutputTokens"] = request.max_tokens

        body = http.post_json(
            f"{self.base_url}/models/{model}:generateContent",
            # Header rather than the ?key= query parameter, so the credential
            # never lands in a URL, a proxy log or an exception message.
            headers={"x-goog-api-key": self.api_key,
                     "Content-Type": "application/json"},
            payload=payload, timeout=timeout, provider=self.label, model=model)

        return ChatResponse(
            text=_extract_text(body) or "",
            provider=self.name, model=model,
            usage=_extract_usage(body),
            finish_reason=(body.get("candidates") or [{}])[0].get("finishReason"),
        )
