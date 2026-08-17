"""Provider adapters.

    Application
        └─ LLM Service        appointments/services/ai/llm.py
             └─ Provider Adapter   this package
                  └─ LLM Provider  Groq · OpenAI-compatible · Gemini · Mock

Adapters translate between the shared contract in :mod:`.base` and one
provider's wire format. Retry, timeouts and status-code classification are
shared (:mod:`.http`) so they cannot drift apart per provider.

Which providers exist, and why only these:

* **Groq** — the configured provider for this deployment. OpenAI-compatible
  wire format, so it is a subclass and a base URL rather than a new client.
* **OpenAI-compatible** — OpenAI, other gateways, and **local models** (Ollama,
  LM Studio, vLLM all serve this API); the deployment differs only by base URL.
* **Gemini** — a genuinely different wire format, over REST rather than the
  end-of-life SDK.
* **Mock** — offline and deterministic, for proving the abstraction without
  spending a request. Never selected automatically; see :mod:`.mock`.

OpenRouter was removed when Groq was configured: it was the previous route to
other model families, nothing in this deployment used it, and keeping an
unconfigured adapter around is a credential surface and a maintenance cost for
no benefit. Anything it reached is still reachable through the
OpenAI-compatible adapter with a base URL.
"""
from .base import (
    ASSISTANT, SYSTEM, TOOL, USER, AuthError, Capabilities, ChatRequest,
    ChatResponse, InvalidResponse, LLMProvider, Message, ModelNotFound,
    ProviderError, ProviderNotConfigured, ProviderTimeout, ProviderUnavailable,
    QuotaExceeded, RateLimited, ToolCall, Usage,
)
from .gemini import GeminiProvider
from .groq import GroqProvider
from .mock import MockProvider
from .openai_compat import OpenAICompatibleProvider
from .registry import (
    ALIASES, AUTO_ORDER, PROVIDERS, available, canonical, get, resolve,
    selected_name, selection_variable,
)
from .registry_utils import is_placeholder

__all__ = [
    "SYSTEM", "USER", "ASSISTANT", "TOOL",
    "Message", "ToolCall", "ChatRequest", "ChatResponse", "Usage",
    "Capabilities",
    "LLMProvider",
    "ProviderError", "ProviderNotConfigured", "AuthError", "QuotaExceeded",
    "RateLimited", "ModelNotFound", "ProviderTimeout", "ProviderUnavailable",
    "InvalidResponse",
    "GroqProvider", "OpenAICompatibleProvider", "GeminiProvider",
    "MockProvider",
    "PROVIDERS", "ALIASES", "AUTO_ORDER",
    "available", "canonical", "get", "resolve", "selected_name",
    "selection_variable",
    "is_placeholder",
]
