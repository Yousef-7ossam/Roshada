"""The provider contract.

Every adapter speaks this vocabulary, so the service layer above never needs to
know which company is answering. Before this, the three clients had three
different signatures: only one accepted a system prompt, only one had a retry
policy, none reported token usage, and each raised its own exception type. The
same outage behaved differently depending on which key happened to be set.

Three things are defined here and nowhere else:

* the **request/response types** — what a completion takes and returns,
* the **capability flags** — what an adapter can do, so callers can ask instead
  of assuming. Tool calling is now real on the OpenAI-dialect adapters;
  streaming still is not, and the flag says so rather than leaving a caller to
  find out,
* the **error taxonomy** — one exception family, mapped from each provider's own
  status codes, so "rate limited" means the same thing everywhere.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

SYSTEM = "system"
USER = "user"
ASSISTANT = "assistant"
#: A tool's result, fed back to the model as its own turn.
TOOL = "tool"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class ProviderError(Exception):
    """Base for every provider failure.

    ``str(exc)`` is for logs, never for a patient: it can carry URLs, model ids
    and upstream detail. The pipeline maps these to fixed user-facing copy.
    """
    #: Whether trying a different *model* on the same provider could help.
    retry_other_model = False


class ProviderNotConfigured(ProviderError):
    """No usable credential — missing, blank, or still a placeholder."""


class AuthError(ProviderError):
    """The provider rejected the credential (401/403)."""


class QuotaExceeded(ProviderError):
    """The account is out of credit (402)."""


class RateLimited(ProviderError):
    """Too many requests (429)."""


class ModelNotFound(ProviderError):
    """The configured model id does not exist for this provider."""
    # The one failure a different model can actually fix.
    retry_other_model = True


class ProviderTimeout(ProviderError):
    """The provider did not answer within the timeout."""


class ProviderUnavailable(ProviderError):
    """Network failure or upstream 5xx."""


class InvalidResponse(ProviderError):
    """The provider answered, but not in a shape we can read."""


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolCall:
    """One tool the model asked to run.

    ``arguments`` is the model's JSON *as text*. It is deliberately not parsed
    here: the adapter's job is to report faithfully what the model said, and a
    malformed argument object is information the executor needs, not an error
    the transport should swallow.
    """
    id: str
    name: str
    arguments: str = "{}"

    def as_dict(self):
        return {"id": self.id, "type": "function",
                "function": {"name": self.name, "arguments": self.arguments}}


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    #: Set on an assistant turn that asked for tools.
    tool_calls: tuple = ()
    #: Set on a ``tool`` turn: which call this is the result of.
    tool_call_id: Optional[str] = None
    #: Set on a ``tool`` turn: which tool produced it.
    name: Optional[str] = None

    def as_dict(self):
        payload = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [call.as_dict() for call in self.tool_calls]
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        if self.name:
            payload["name"] = self.name
        return payload


@dataclass(frozen=True)
class Capabilities:
    """What an adapter can do.

    A caller asks rather than assumes. ``tools`` is the load-bearing one: the
    assistant degrades to context-only answering against a provider that cannot
    call tools, instead of failing or pretending.
    """
    streaming: bool = False
    tools: bool = False
    #: False when the provider has no first-class system role and the adapter
    #: has to fold instructions in some other way.
    native_system_prompt: bool = True


@dataclass(frozen=True)
class Usage:
    """Token accounting, when the provider reports it."""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    def as_dict(self):
        return {"prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens}

    @property
    def known(self):
        return self.total_tokens is not None


@dataclass
class ChatRequest:
    """One completion request, provider-independent."""
    messages: list = field(default_factory=list)
    model: Optional[str] = None
    temperature: float = 0.7
    timeout: Optional[float] = None
    max_tokens: Optional[int] = None
    #: OpenAI-style tool schemas the model may call. Empty means "answer only".
    tools: list = field(default_factory=list)
    #: ``auto`` | ``none`` | ``required``.
    tool_choice: str = "auto"

    @property
    def system_prompt(self):
        for message in self.messages:
            if message.role == SYSTEM:
                return message.content
        return None

    @property
    def conversation(self):
        """Messages excluding the system prompt, in order."""
        return [m for m in self.messages if m.role != SYSTEM]


@dataclass(frozen=True)
class ChatResponse:
    text: str
    provider: str
    model: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: Optional[str] = None
    #: Tools the model asked to run, in the order it asked. Empty on an
    #: ordinary answer.
    tool_calls: tuple = ()

    @property
    def wants_tools(self):
        return bool(self.tool_calls)


# ---------------------------------------------------------------------------
# The adapter contract
# ---------------------------------------------------------------------------
class LLMProvider(ABC):
    """One provider, behind one method.

    Adapters own exactly three things: how to authenticate, how to shape the
    request, and how to read the answer. Retry, timeouts and status-code
    classification are shared (see :mod:`.http`) so they cannot drift apart.
    """
    #: Registry key, and the value ``AI_PROVIDER`` is matched against.
    name = "base"
    #: Human-readable, for the UI caption.
    label = "Base"
    capabilities = Capabilities()

    @classmethod
    @abstractmethod
    def is_configured(cls) -> bool:
        """True when this provider has a usable credential *right now*.

        Read from the environment on every call, never cached at import: a key
        added to ``.env`` must take effect without restarting the worker.
        """

    @property
    @abstractmethod
    def default_model(self) -> str:
        """The model used when the caller does not name one."""

    @abstractmethod
    def complete(self, request: ChatRequest) -> ChatResponse:
        """Run one completion, or raise a :class:`ProviderError`."""

    def describe(self):
        return f"{self.label} · {self.default_model}"
