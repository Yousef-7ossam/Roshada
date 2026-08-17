"""OpenAI-compatible chat completions.

This one adapter covers three deployments, because they all speak the same
``POST {base}/chat/completions`` dialect — only the base URL differs:

* **OpenAI** — the default base URL.
* **TokenRouter** or any other gateway — set ``OPENAI_BASE_URL``.
* **A local model** — Ollama (``http://localhost:11434/v1``), LM Studio
  (``http://localhost:1234/v1``) and vLLM all expose this API. Point
  ``OPENAI_BASE_URL`` at it. No extra adapter, no extra dependency; a local
  model needs no API key, so ``OPENAI_API_KEY`` may be any non-placeholder
  value.

Written against the HTTP API rather than the ``openai`` SDK so every provider
shares one retry/timeout policy (see :mod:`.http`) — and so the project carries
one fewer dependency.
"""
import os

from . import http
from .base import (
    Capabilities, ChatResponse, InvalidResponse, LLMProvider, ModelNotFound,
    ProviderError, ProviderNotConfigured, ToolCall, Usage,
)
from .registry_utils import is_placeholder

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


def _extract_content(payload):
    """Read the assistant text out of a chat-completions body.

    Handles both the plain string form and the content-array form some
    gateways return.
    """
    try:
        message = payload["choices"][0].get("message", {})
    except (KeyError, IndexError, TypeError) as exc:
        raise InvalidResponse(f"Unexpected chat-completions shape: {exc}") from exc

    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content
                 if isinstance(item, dict) and isinstance(item.get("text"), str)]
        return "".join(parts)
    return ""


def _extract_tool_calls(payload):
    """Read the tool calls out of a chat-completions body.

    Anything malformed is skipped rather than raised: a model that emits one
    good call and one broken one should still get the good one run, and the
    executor already refuses arguments it cannot parse.
    """
    try:
        message = payload["choices"][0].get("message", {})
    except (KeyError, IndexError, TypeError):
        return ()

    calls = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        calls.append(ToolCall(
            # Some gateways omit the id. The conversation still has to pair
            # each result with its call, so synthesise a stable one.
            id=raw.get("id") or f"call_{index}",
            name=name,
            arguments=function.get("arguments") or "{}"))
    return tuple(calls)


def _extract_usage(payload):
    usage = payload.get("usage") or {}
    return Usage(
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
    )


class OpenAICompatibleProvider(LLMProvider):
    name = "openai"
    label = "OpenAI"
    capabilities = Capabilities(streaming=False, tools=True,
                                native_system_prompt=True)

    key_var = "OPENAI_API_KEY"
    base_url_var = "OPENAI_BASE_URL"
    model_var = "OPENAI_MODEL"
    fallbacks_var = "OPENAI_MODEL_FALLBACKS"
    timeout_var = "OPENAI_TIMEOUT"
    default_base_url = DEFAULT_BASE_URL
    fallback_model = DEFAULT_MODEL

    # -- configuration ---------------------------------------------------
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
        return (os.environ.get(self.base_url_var) or self.default_base_url).rstrip("/")

    @property
    def default_model(self):
        return os.environ.get(self.model_var) or self.fallback_model

    def _fallback_models(self):
        """Alternative model ids to try when the configured one does not exist.

        Only ever consulted for :class:`ModelNotFound`. The previous
        implementation retried every candidate on *any* exception, so a 401 or a
        content-policy refusal triggered a full billable sweep and then reported
        itself as a model error. It also appended a
        hardcoded ``gpt-4o-mini`` regardless of configuration, which could
        silently bill a model nobody chose — that is gone: fallbacks are now
        exactly what the operator configured, and nothing else.
        """
        raw = os.environ.get(self.fallbacks_var, "")
        return [part.strip() for part in raw.split(",") if part.strip()]

    def candidate_models(self, model=None):
        ordered = [model or self.default_model, *self._fallback_models()]
        return list(dict.fromkeys(m for m in ordered if m))

    # -- request ---------------------------------------------------------
    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"}

    def _payload(self, request, model):
        payload = {
            "model": model,
            "messages": [m.as_dict() for m in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.tools:
            payload["tools"] = request.tools
            payload["tool_choice"] = request.tool_choice
        return payload

    def complete(self, request):
        timeout = http.timeout_for(request.timeout, self.timeout_var)
        candidates = self.candidate_models(request.model)
        last_error = None

        for model in candidates:
            try:
                body = http.post_json(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    payload=self._payload(request, model),
                    timeout=timeout, provider=self.label, model=model)
            except ModelNotFound as exc:
                # The only error a different model can fix. Keep trying.
                last_error = exc
                continue
            except ProviderError:
                # Auth, quota, rate limit, timeout, network: retrying with a
                # different model cannot help and costs another call.
                raise

            return ChatResponse(
                text=_extract_content(body) or "",
                provider=self.name, model=model,
                usage=_extract_usage(body),
                finish_reason=(body.get("choices") or [{}])[0].get("finish_reason"),
                tool_calls=_extract_tool_calls(body),
            )

        raise last_error or ModelNotFound(
            f"{self.label}: no usable model among {', '.join(candidates)}")
