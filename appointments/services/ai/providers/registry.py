"""Provider registry and selection.

One place decides which adapter answers a request, from the environment only.
No credential is ever read anywhere but the adapter that needs it, and none is
hardcoded.

``AI_PROVIDER`` selects explicitly (``groq`` | ``openai`` | ``gemini`` |
``local`` | ``mock``) or, unset/``auto``, picks the first configured provider in
:data:`AUTO_ORDER`.

``LLM_PROVIDER`` is accepted as an alias for ``AI_PROVIDER``, because that is
the name the deployment instructions use. ``AI_PROVIDER`` wins when both are
set, so there is still exactly one answer.

Selection is resolved **per call**, never cached at import, so adding a key to
``.env`` takes effect without restarting the worker — and so provider switching
is testable by setting an environment variable.
"""
import os

from .base import ProviderNotConfigured
from .gemini import GeminiProvider
from .groq import GroqProvider
from .mock import MockProvider
from .openai_compat import OpenAICompatibleProvider

#: Registered adapters, by the name ``AI_PROVIDER`` is matched against.
PROVIDERS = {
    GroqProvider.name: GroqProvider,
    OpenAICompatibleProvider.name: OpenAICompatibleProvider,
    GeminiProvider.name: GeminiProvider,
    MockProvider.name: MockProvider,
}

#: ``local`` is an alias, not another adapter: Ollama, LM Studio and vLLM all
#: expose the OpenAI chat-completions API, so a local model is the
#: OpenAI-compatible adapter with ``OPENAI_BASE_URL`` pointed at localhost.
ALIASES = {"local": OpenAICompatibleProvider.name,
           "ollama": OpenAICompatibleProvider.name,
           "tokenrouter": OpenAICompatibleProvider.name,
           "offline": MockProvider.name,
           "test": MockProvider.name}

#: Order ``auto`` tries. First configured wins.
#:
#: **The mock is deliberately absent.** It reports itself configured (it needs
#: no key), so including it here would make it the answer whenever a real
#: provider's key is missing — serving invented text to someone asking a medical
#: question. It answers only when an operator names it explicitly.
AUTO_ORDER = (GroqProvider.name, OpenAICompatibleProvider.name,
              GeminiProvider.name)


def selection_variable():
    """The configured provider name, from either accepted variable.

    ``AI_PROVIDER`` is the project's own name and wins; ``LLM_PROVIDER`` is
    accepted because that is what the deployment instructions use. Reading both
    in one place keeps it a single answer rather than two settings that can
    disagree.
    """
    return (os.environ.get("AI_PROVIDER")
            or os.environ.get("LLM_PROVIDER")
            or "auto")


def canonical(name):
    """Resolve an alias to a registered provider name."""
    name = (name or "").strip().lower()
    return ALIASES.get(name, name)


def available():
    """Names of every provider with a usable credential right now."""
    return [name for name in AUTO_ORDER if PROVIDERS[name].is_configured()]


def selected_name():
    """Which provider will answer, or ``None`` when none is usable.

    An explicitly named provider is never silently swapped for another: if the
    operator asked for Gemini and Gemini has no key, the assistant reports
    itself unavailable rather than sending patient messages to a different
    company.
    """
    preference = canonical(selection_variable())

    if preference and preference != "auto":
        if preference not in PROVIDERS:
            return None
        return preference if PROVIDERS[preference].is_configured() else None

    for name in AUTO_ORDER:
        if PROVIDERS[name].is_configured():
            return name
    return None


def get(name):
    """Instantiate a provider by name. Raises if it is unknown."""
    name = canonical(name)
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ProviderNotConfigured(
            f"Unknown AI provider '{name}'. "
            f"Choose one of: {', '.join(sorted(PROVIDERS))}, local.")
    return provider()


def resolve():
    """The active provider instance, or ``None`` when the assistant is off."""
    name = selected_name()
    return get(name) if name else None
