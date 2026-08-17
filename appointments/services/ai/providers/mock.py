"""A deterministic provider that never leaves the process.

For proving the abstraction without spending a request: set ``AI_PROVIDER=mock``
and every completion is answered locally, with the same
:class:`~.base.ChatResponse` shape a real provider returns.

**It is excluded from automatic selection, deliberately.** ``is_configured()``
is true — it needs no credential — but it is absent from
:data:`~.registry.AUTO_ORDER`, so ``AI_PROVIDER=auto`` can never fall through to
it. A mock that silently wins when a real key is missing would serve invented
text to a patient asking a medical question, with nothing in the response saying
so. It answers only when an operator names it.

The reply says what it is. A mock whose output is indistinguishable from a real
answer is a mock that will eventually be mistaken for one.
"""
import os

from .base import Capabilities, ChatResponse, LLMProvider, Usage

MOCK_NOTICE = (
    "[mock provider] No language model was called. This is Roshada's offline "
    "test provider, configured with AI_PROVIDER=mock.")


class MockProvider(LLMProvider):
    """Answers locally, deterministically, and says that it did."""

    name = "mock"
    label = "Mock (offline)"
    capabilities = Capabilities(streaming=False, tools=False,
                               native_system_prompt=True)

    key_var = "MOCK_API_KEY"          # never read; declared for interface parity
    model_var = "MOCK_MODEL"
    fallback_model = "roshada-mock-1"

    @classmethod
    def is_configured(cls):
        """Always usable — but see the module docstring: never auto-selected."""
        return True

    @property
    def default_model(self):
        return os.environ.get(self.model_var) or self.fallback_model

    def complete(self, request):
        """Echo the shape of a real completion without making one.

        The last user message is quoted back so a test can assert the prompt
        actually reached the provider through the abstraction — which is the
        only thing a mock is good for.
        """
        last = ""
        for message in reversed(request.messages or []):
            if message.role == "user":
                last = message.content
                break

        preview = " ".join((last or "").split())[:200]
        text = f"{MOCK_NOTICE}\n\nReceived: {preview}" if preview else MOCK_NOTICE

        return ChatResponse(
            text=text,
            provider=self.name,
            model=request.model or self.default_model,
            # No tokens were spent, and reporting a made-up count would put a
            # fabricated number into the usage logs.
            usage=Usage(),
            finish_reason="stop",
        )
