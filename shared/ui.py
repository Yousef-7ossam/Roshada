"""Reusable UI helpers shared across the app.

Centralises session-state setup, consistent API-error messaging, accessible
result rendering, and the cardiovascular input form that the Blood-Pressure and
Heart-Disease pages both use (removing duplicated components).
"""
import re

import streamlit as st


def clean_html(text):
    """Strip HTML tags from user or model text before it is rendered or stored.

    Lived in ``shared/gemini.py`` until the providers moved server-side — a view
    helper in a provider client, which is why importing the Gemini module used
    to drag Streamlit into anything that touched it.
    """
    return re.sub("<.*?>", "", text or "")


# Plain text, deliberately: this is rendered by theme.page_header(), which
# HTML-escapes its subtitle, so markdown emphasis would show as literal
# asterisks ("**not a medical diagnosis**") on the page.
DISCLAIMER = (
    "ℹ️ This is an AI assistant — NOT a medical diagnosis. "
    "Always consult a licensed clinician."
)

_SESSION_DEFAULTS = {
    "token": None,
    "user_id": None,
    "user_name": "",
    "user_email": "",
    "role": None,
    "messages": [],
    # Desktop sidebar: icons-only when True.
    "sidebar_collapsed": False,
    # National-ID auto-fill values (persist across reruns of the signup form)
    "auto_name": "",
    "auto_gender": "",
    "auto_address": "",
    "auto_age": 0,
}


def init_session_state():
    """Initialise every session key exactly once, in one place."""
    for key, value in _SESSION_DEFAULTS.items():
        st.session_state.setdefault(key, value)


def api_error_text(res, fallback):
    """Extract the backend's standardized {"error": ...} message, else fallback."""
    try:
        body = res.json()
        if isinstance(body, dict) and body.get("error"):
            return str(body["error"])
    except Exception:
        pass
    return fallback


def show_api_error(res, fallback="Something went wrong. Please try again."):
    """Show a friendly error. If res is None the connection error was already
    reported by api_request, so we stay silent to avoid duplicate messages."""
    if res is None:
        return
    st.error(api_error_text(res, fallback))
