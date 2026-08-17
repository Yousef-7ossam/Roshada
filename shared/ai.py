"""AI-assistant client.

The assistant itself now lives behind the API
(:mod:`appointments.services.ai`). This module is the thin client for it: it
posts a question, returns the structured answer, and holds no provider
credentials, no prompt and no conversation state of its own.

It used to be the assistant — selecting a provider, assembling the prompt, and
calling the LLM from inside the Streamlit process. That put provider keys on the
frontend host and left the model unable to see the patient's own record without
the browser tier fetching and forwarding it.

**One piece of logic is deliberately duplicated here:** the emergency red-flag
check. If the backend is unreachable, a patient describing chest pain must still
be told to seek care. That guarantee cannot be allowed to depend on our own API
being up, so :func:`ask` falls back to the local check
(:mod:`shared.safety` is pure pattern matching, no network) whenever the request
does not come back.
"""
import os

import streamlit as st

from shared import safety
from shared.api import api_request

#: Ask requests wait longer than an ordinary API call: the backend is doing an
#: LLM round-trip inside them. Without this the UI aborts at API_TIMEOUT while
#: the server is still working — and then records an answer the user never saw.
ASK_TIMEOUT = float(os.environ.get("AI_REQUEST_TIMEOUT", "60"))

_STATUS_KEY = "_ai_status"

#: Shown when our own API cannot be reached. Distinct from the backend's
#: provider-outage message so the two failures stay tellable apart in support.
OFFLINE_REPLY = (
    "The assistant is unreachable right now because Roshada's server didn't "
    "respond. Your message was not saved. Please try again in a moment.")


def _degraded(reply, message):
    """A client-side answer for when the API call itself failed.

    Carries the emergency notice on its own, so the one message a user must
    never miss survives a backend outage.
    """
    label = safety.detect_emergency(message)
    if label:
        reply = f"{safety.EMERGENCY_NOTICE}\n\n---\n\n{reply}"
    return {
        "reply": reply,
        "emergency": {"detected": bool(label), "label": label},
        "sources": [],
        "citations": [],
        "grounded": False,
        "warnings": [],
        "provider": None,
        "model": None,
        "degraded": True,
        "offline": True,
    }


def status(refresh=False):
    """Assistant availability, cached for the session.

    Cached because the caption renders on every rerun and this would otherwise
    cost one API round-trip per keystroke-triggered rerender.
    """
    if refresh or _STATUS_KEY not in st.session_state:
        res = api_request("GET", "chat/status/")
        if res is not None and res.status_code == 200:
            st.session_state[_STATUS_KEY] = res.json()
        else:
            # Unknown, not disabled: a transient blip must not make the page
            # claim the assistant is switched off.
            st.session_state[_STATUS_KEY] = {
                "enabled": True, "provider": None, "label": "unavailable"}
    return st.session_state[_STATUS_KEY]


def is_enabled():
    return bool(status().get("enabled"))


def provider_label():
    return status().get("label") or "disabled"


def ask(message):
    """Ask the assistant. Always returns a payload — never raises, never None."""
    res = api_request("POST", "chat/ask/", {"message": message},
                      timeout=ASK_TIMEOUT)
    if res is None:
        return _degraded(OFFLINE_REPLY, message)

    if res.status_code == 429:
        return _degraded(
            "You've sent a lot of questions in a short time. Please wait a "
            "minute before asking again.", message)

    if res.status_code != 200:
        return _degraded(OFFLINE_REPLY, message)

    return res.json()
