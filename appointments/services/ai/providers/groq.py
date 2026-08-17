"""Groq (https://groq.com).

Groq serves an OpenAI-compatible ``/chat/completions`` API, so this is the
OpenAI-compatible adapter with a different base URL, its own credential and its
own default model. Nothing else differs, which is the point of having the
adapter: adding a provider is configuration plus a subclass, not a second
architecture.

**Why not the ``openai`` SDK.** The brief suggests it, and it would work — Groq
documents exactly that usage. This project deliberately speaks the HTTP API
directly instead, so every provider shares one retry, timeout and error-taxonomy
policy (:mod:`.http`) and the project carries one fewer dependency. Using the
SDK here would give Groq a different failure taxonomy from every other provider,
which is the thing the adapter layer exists to prevent.

This adapter does **one** thing: talk to Groq. It holds no database access, no
RAG logic, no patient logic and no tool execution — those live above it, and a
test asserts this module imports none of them.
"""
from .base import Capabilities
from .openai_compat import OpenAICompatibleProvider

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
#: The model this deployment is configured for. Overridden by ``GROQ_MODEL``;
#: never read from anywhere but configuration.
DEFAULT_MODEL = "openai/gpt-oss-20b"


class GroqProvider(OpenAICompatibleProvider):
    name = "groq"
    label = "Groq"
    #: Groq supports tool calling on several models. It is declared ``False``
    #: because Roshada has no tool layer yet: advertising a capability nothing
    #: implements would be a promise the platform cannot keep. Flipping it is a
    #: one-line change when the agent step lands.
    # Tool calling comes from the OpenAI dialect Groq speaks — same wire
    # format, same adapter code. Streaming is still not implemented.
    capabilities = Capabilities(streaming=False, tools=True,
                                native_system_prompt=True)

    key_var = "GROQ_API_KEY"
    base_url_var = "GROQ_BASE_URL"
    model_var = "GROQ_MODEL"
    fallbacks_var = "GROQ_MODEL_FALLBACKS"
    timeout_var = "GROQ_TIMEOUT"
    default_base_url = DEFAULT_BASE_URL
    fallback_model = DEFAULT_MODEL
