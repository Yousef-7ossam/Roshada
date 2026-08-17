"""Backend API client + auth handlers shared by the portal frontends."""
import os

import requests
import streamlit as st

from shared.ui import api_error_text

# Base URL of the Django API. Overridable via env for containers/deployments.
API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000/api/")
API_URL = API_BASE_URL

# Seconds before a request is abandoned. Without this the UI hangs indefinitely
# whenever the backend accepts a connection but never answers.
API_TIMEOUT = float(os.environ.get("API_TIMEOUT", "30"))

_SUPPORTED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


def api_request(method, endpoint, data=None, files=None, timeout=None):
    """Call the backend and return the Response, or None if it was unreachable.

    Every failure mode that leaves us without a response reports itself to the
    user here and returns None; callers only ever have to check for None or the
    status code. Previously only ConnectionError was caught, so a timeout or TLS
    error surfaced as a raw Streamlit traceback.

    ``timeout`` overrides the default for calls that legitimately take longer
    than a database read — the AI endpoint holds an LLM round-trip open inside
    the request.
    """
    method = method.upper()
    if method not in _SUPPORTED_METHODS:
        raise ValueError(f"Unsupported HTTP method: {method}")

    headers = {}
    if st.session_state.get('token'):
        headers['Authorization'] = f"Token {st.session_state['token']}"
    url = f"{API_URL}{endpoint}"

    kwargs = {"headers": headers, "timeout": timeout or API_TIMEOUT}
    if files is not None:
        # multipart: requests sets the boundary/content-type itself. `data`
        # rides along as regular form fields so callers can send both.
        kwargs["files"] = files
        if data:
            kwargs["data"] = data
    elif data is not None:
        kwargs["json"] = data

    try:
        response = requests.request(method, url, **kwargs)
    except requests.exceptions.Timeout:
        st.error("The backend took too long to respond. Please try again.")
        return None
    except requests.exceptions.ConnectionError:
        st.error("Could not connect to backend.")
        return None
    except requests.exceptions.RequestException as exc:
        st.error(f"Request failed: {exc}")
        return None

    # Tokens expire (AUTH_TOKEN_TTL_HOURS), so a 401 on a request we authenticated
    # is a routine end-of-session event, not an anomaly. Without this the caller
    # gets None, show_api_error() stays silent by design, and the page renders
    # blank with no way back to the login screen.
    if response.status_code == 401 and headers.get('Authorization'):
        _expire_session()

    return response


#: Session keys that belong to the signed-in account and must not survive a
#: sign-out — the chat thread in particular would otherwise still be on screen
#: for whoever signs in next on the same browser session.
_ACCOUNT_SESSION_DEFAULTS = (
    ('token', None), ('user_id', None), ('user_name', ""), ('user_email', ""),
    ('role', None), ('messages', list), ('_chat_loaded', False),
    ('notifications', list), ('_notif_role', None),
)


def _clear_account_session():
    """Reset every per-account key. `list` means "a fresh empty list"."""
    for key, default in _ACCOUNT_SESSION_DEFAULTS:
        st.session_state[key] = default() if callable(default) else default


def _expire_session():
    """Drop the dead credential and send the user back to the login screen."""
    _clear_account_session()
    st.warning("Your session expired. Please sign in again.")
    st.rerun()


def handle_login(username, password):
    res = api_request("POST", "login/",
                      {"username": username, "password": password})
    if res is None:
        return  # api_request already reported the connection problem

    if res.status_code != 200:
        # Distinguish "wrong password" from "server said no for another reason";
        # every non-200 used to read as "Invalid Username or Password".
        if res.status_code == 400:
            st.error("Invalid Username or Password")
        elif res.status_code == 429:
            st.error("Too many sign-in attempts. Please wait a minute and retry.")
        else:
            st.error(api_error_text(res, "Sign-in failed. Please try again."))
        return

    _adopt_session(res.json(), username)
    st.success("Login Successful!")
    st.rerun()


def _adopt_session(data, fallback_username):
    """Install a freshly issued token + role, then enrich from /profile/.

    Shared by sign-in and registration so both land in the same session shape —
    the role in particular, since it is what the frontend routes on.
    """
    # Start from a clean slate so nothing from a previous account lingers.
    _clear_account_session()
    st.session_state['token'] = data['token']
    st.session_state['role'] = data.get('role')

    # Profile enrichment is best-effort: authentication itself already
    # succeeded, so a failure here must not strand the user on the login screen
    # (the rerun used to live inside the success branch).
    profile_res = api_request("GET", "profile/")
    if profile_res is not None and profile_res.status_code == 200:
        profile_data = profile_res.json()
        st.session_state['user_id'] = profile_data.get('id', profile_data.get('username'))
        st.session_state['user_name'] = (
                profile_data.get('name')
                or profile_data.get('full_name')
                or profile_data.get('first_name')
                or profile_data.get('username')
                or fallback_username
        )
        # Shown as the secondary line of the sidebar profile card.
        st.session_state['user_email'] = profile_data.get('email') or ""
        # The server's answer wins: the role must never come from anything the
        # browser can edit, and /profile/ is derived from the token.
        st.session_state['role'] = profile_data.get('role') or st.session_state['role']
    else:
        st.session_state['user_id'] = fallback_username
        st.session_state['user_name'] = fallback_username


def handle_registration(data, username):
    """Sign a newly registered account straight in to its own portal.

    Registration already returns a token, so requiring the user to re-enter the
    credentials they just chose would add a step and no security.
    """
    _adopt_session(data, username)
    st.success("Account created. Welcome to Roshada!")
    st.rerun()


def handle_logout():
    # Invalidate the token server-side too, so it cannot be replayed.
    if st.session_state.get('token'):
        api_request("POST", "logout/")
    _clear_account_session()
    st.rerun()
