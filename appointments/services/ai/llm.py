"""The LLM service.

    Application  →  **LLM Service**  →  Provider Adapter  →  LLM Provider

This is the only module the pipeline talks to. It owns *policy* — which provider
answers, what the request looks like, how failures are categorised for the
caller — and delegates *protocol* to the adapters in :mod:`.providers`.

This module began as a narrow seam over three hand-rolled clients with three
different signatures. Its interior is now a real adapter layer, but
:func:`complete` kept its signature throughout, so the pipeline, the views and
their tests never had to move.

Failures reach the caller as one of two categories, because those are the only
two the pipeline can act on:

* :class:`LLMUnavailable` — nothing is configured to answer (operator problem),
* :class:`LLMFailed` — something was configured, and it did not answer.

The specific cause (auth, quota, rate limit, timeout, bad model) is logged with
its provider and taxonomy class, never returned: provider text carries URLs,
model ids and library internals that must not reach a patient.
"""
import logging

from . import providers
from .providers import (
    ChatRequest, ChatResponse, Message, ProviderError, ProviderNotConfigured,
)
from .providers.http import DEFAULT_TIMEOUT

logger = logging.getLogger("appointments")

#: Kept under its original name for callers that read the default from here.
TIMEOUT = DEFAULT_TIMEOUT

#: What :func:`complete` returns. Named ``LLMResult`` before the adapter layer
#: existed; the alias is kept so callers written against the older seam keep
#: working unchanged.
LLMResult = ChatResponse


class LLMUnavailable(Exception):
    """No provider is configured. An operator problem, not a user problem."""


class LLMFailed(Exception):
    """A configured provider was reached but did not return a usable answer."""


# ---------------------------------------------------------------------------
# Availability (used by the status endpoint and the UI caption)
# ---------------------------------------------------------------------------
def active_provider():
    """The provider that will handle a request, or ``None`` when disabled."""
    return providers.selected_name()


def is_enabled():
    return active_provider() is not None


def model_for(provider=None):
    """The model the given (or active) provider would use."""
    provider = provider or active_provider()
    if not provider:
        return ""
    try:
        return providers.get(provider).default_model
    except ProviderError:
        return ""


def provider_label(provider=None):
    """Human-readable "Provider · model", for the UI caption."""
    provider = provider or active_provider()
    if not provider:
        return "disabled"
    try:
        return providers.get(provider).describe()
    except ProviderError:
        return "disabled"


def supports_tools(provider=None):
    """Whether the active provider can call tools.

    Asked before the agent loop is entered, so a provider without tool support
    degrades to context-only answering instead of being sent a request it will
    ignore or reject.
    """
    return bool(capabilities(provider).tools)


def capabilities(provider=None):
    """What the active provider can do."""
    provider = provider or active_provider()
    if not provider:
        return providers.Capabilities()
    try:
        return providers.get(provider).capabilities
    except ProviderError:
        return providers.Capabilities()


def describe():
    """Everything the status endpoint needs, without exposing a credential."""
    provider = active_provider()
    caps = capabilities(provider)
    return {
        "enabled": provider is not None,
        "provider": provider,
        "model": model_for(provider),
        "label": provider_label(provider),
        "configured_providers": providers.available(),
        "capabilities": {"streaming": caps.streaming, "tools": caps.tools},
    }


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------
def _build_request(prompt, history, system_prompt, model, temperature, timeout):
    messages = []
    if system_prompt:
        messages.append(Message(providers.SYSTEM, system_prompt))
    for turn in history or []:
        role = turn.get("role") or providers.USER
        messages.append(Message(role, turn.get("content", "")))
    messages.append(Message(providers.USER, prompt))
    return ChatRequest(messages=messages, model=model,
                       temperature=temperature, timeout=timeout)


def complete(prompt, *, history=None, system_prompt=None, model=None,
             temperature=0.7, timeout=None, tools=None, tool_choice="auto"):
    """Run one completion.

    Returns the adapter's :class:`~.providers.base.ChatResponse`, which carries
    the text, the provider and model that actually answered, token usage when
    the provider reports it, and any tools the model asked to run.

    Raises :class:`LLMUnavailable` when nothing is configured, and
    :class:`LLMFailed` for every provider-side failure.
    """
    request = _build_request(prompt, history, system_prompt, model,
                             temperature, timeout)
    if tools:
        request.tools = tools
        request.tool_choice = tool_choice
    return converse(request)


def converse(request):
    """Run one completion from a fully-built request.

    The seam the agent loop needs: a tool-using turn is a *conversation* —
    assistant asks for tools, tool results come back as their own turns, the
    model answers — and that cannot be expressed as (prompt, history) without
    losing which result belongs to which call.

    Everything :func:`complete` guarantees still holds here, because
    :func:`complete` is now a thin caller of this.
    """
    provider = providers.resolve()
    if provider is None:
        raise LLMUnavailable("No AI provider is configured.")

    if request.tools and not provider.capabilities.tools:
        # Never send tools to an adapter that cannot honour them: some ignore
        # the field and answer as if the tools did not exist, which would look
        # like the model choosing not to use them.
        request.tools = []

    try:
        response = provider.complete(request)
    except ProviderNotConfigured as exc:
        # Selected but not actually usable — e.g. the key is a placeholder.
        logger.warning("AI provider %s is not usable: %s", provider.name, exc)
        raise LLMUnavailable(str(exc)) from exc
    except ProviderError as exc:
        # Category and provider to the log; nothing to the caller.
        logger.warning("AI provider %s failed [%s]: %s",
                       provider.name, type(exc).__name__, exc, exc_info=True)
        raise LLMFailed(provider.name) from exc
    except Exception as exc:  # an adapter bug must not 500 the request
        logger.exception("Unexpected error from AI provider %s", provider.name)
        raise LLMFailed(provider.name) from exc

    if response.usage.known:
        logger.info("AI completion: provider=%s model=%s tokens=%s",
                    response.provider, response.model, response.usage.total_tokens)
    return response
