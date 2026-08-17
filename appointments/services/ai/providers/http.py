"""Shared HTTP transport for every adapter.

Resilience lives here rather than in each adapter, because when it lived in the
adapters it diverged: one had a pooled session with a retry policy,
OpenAI passed a timeout but never retried, and Gemini had neither. The same
upstream outage produced three different behaviours depending on which key was
set.

One session, one retry policy, one timeout resolution, one mapping from status
code to :mod:`.base` error type. An adapter supplies a URL, headers and a JSON
body, and reads the answer.
"""
import os
from functools import lru_cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .base import (
    AuthError, InvalidResponse, ModelNotFound, ProviderTimeout,
    ProviderUnavailable, QuotaExceeded, RateLimited,
)

#: Default ceiling for one provider call. Kept below the frontend's request
#: timeout so the backend finishes and records the turn before the UI gives up.
DEFAULT_TIMEOUT = 25.0

#: Retried automatically: transient upstream conditions only. A 4xx that means
#: "your key is wrong" or "that model doesn't exist" is never worth repeating.
RETRY_STATUSES = (429, 500, 502, 503, 504)


def timeout_for(explicit=None, env_var=None):
    """Resolve a call timeout.

    Precedence, most specific first:
    explicit argument > provider-specific env var > ``AI_TIMEOUT`` > default.
    """
    if explicit:
        return float(explicit)
    if env_var:
        value = os.environ.get(env_var)
        if value:
            try:
                return float(value)
            except ValueError:
                pass
    try:
        return float(os.environ.get("AI_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT


@lru_cache(maxsize=1)
def session() -> requests.Session:
    """A pooled session with automatic retry.

    Cached: building a Session per call meant a new connection pool every
    request, so the advertised pooling never actually happened.
    """
    pooled = requests.Session()
    retry = Retry(
        total=2, backoff_factor=0.6,
        status_forcelist=RETRY_STATUSES,
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    pooled.mount("https://", adapter)
    pooled.mount("http://", adapter)
    return pooled


def _detail(response):
    """Best-effort upstream message. For logs only — never shown to a user."""
    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:200]

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        if error:
            return str(error)[:300]
        if body.get("message"):
            return str(body["message"])[:300]
    return str(body)[:300]


def raise_for_status(response, *, provider, model):
    """Map a provider's HTTP status onto the shared error taxonomy."""
    status = response.status_code
    if status < 400:
        return

    detail = _detail(response)

    lowered = detail.lower()

    if status in (401, 403):
        raise AuthError(f"{provider} rejected the credential ({status}): {detail}")
    if status == 402:
        raise QuotaExceeded(f"{provider} account is out of credit (402): {detail}")
    if status == 429:
        raise RateLimited(f"{provider} rate limited the request (429): {detail}")
    # Not every provider uses 401 for a bad credential — Gemini answers 400
    # with "API key not valid". Classify on the message so the operator is told
    # to check the key rather than being sent to look at the model id.
    if any(phrase in lowered for phrase in
           ("api key", "api_key", "unauthenticated", "unauthorized",
            "permission denied", "invalid authentication")):
        raise AuthError(f"{provider} rejected the credential ({status}): {detail}")
    if status == 404 or "model" in lowered:
        # By far the most common cause of a 400/404 here is a model id that does
        # not exist. Naming that is far more useful than "error 400" — and it is
        # the one failure trying a different model can actually fix.
        raise ModelNotFound(
            f"{provider} rejected model '{model}' ({status}): {detail}")
    if status >= 500:
        raise ProviderUnavailable(f"{provider} upstream error {status}: {detail}")
    raise ProviderUnavailable(f"{provider} error {status}: {detail}")


def post_json(url, *, headers, payload, timeout, provider, model):
    """POST JSON and return the decoded body, or raise a ``ProviderError``."""
    try:
        response = session().post(url, headers=headers, json=payload,
                                  timeout=timeout)
    except requests.exceptions.Timeout as exc:
        raise ProviderTimeout(f"{provider} timed out after {timeout}s") from exc
    except requests.exceptions.ConnectionError as exc:
        raise ProviderUnavailable(f"Could not reach {provider}: {exc}") from exc
    except requests.exceptions.RequestException as exc:
        raise ProviderUnavailable(f"{provider} request failed: {exc}") from exc

    raise_for_status(response, provider=provider, model=model)

    try:
        return response.json()
    except ValueError as exc:
        raise InvalidResponse(f"{provider} returned a non-JSON body") from exc
