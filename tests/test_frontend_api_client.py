"""Frontend API-client regressions (shared/api.py).

Streamlit is stubbed out so these run as plain unit tests without a script
context.
"""
from unittest import mock

import pytest
import requests


class _Rerun(Exception):
    """Stands in for streamlit.rerun(), which aborts the script run."""


class _FakeStreamlit:
    def __init__(self):
        self.session_state = {}
        self.errors = []
        self.warnings = []
        self.successes = []
        self.reran = False

    def error(self, msg):
        self.errors.append(msg)

    def warning(self, msg):
        self.warnings.append(msg)

    def success(self, msg):
        self.successes.append(msg)

    def rerun(self):
        self.reran = True
        raise _Rerun()


@pytest.fixture
def api(monkeypatch):
    import shared.api as module
    fake = _FakeStreamlit()
    monkeypatch.setattr(module, "st", fake)
    monkeypatch.setattr(module.ui if hasattr(module, "ui") else module, "st", fake, raising=False)
    return module, fake


def _response(status, payload=None):
    r = requests.Response()
    r.status_code = status
    r._content = (payload or b"{}")
    r.headers["Content-Type"] = "application/json"
    return r


# ---------------------------------------------------------------------------
# BUG-018 — no timeout, and only ConnectionError was handled
# ---------------------------------------------------------------------------
def test_every_request_carries_a_timeout(api):
    module, fake = api
    with mock.patch.object(module.requests, "request",
                           return_value=_response(200)) as req:
        module.api_request("GET", "doctors/")
    assert req.call_args.kwargs["timeout"] == module.API_TIMEOUT


def test_timeout_is_reported_not_raised(api):
    module, fake = api
    with mock.patch.object(module.requests, "request",
                           side_effect=requests.exceptions.Timeout()):
        assert module.api_request("GET", "doctors/") is None
    assert any("too long" in e for e in fake.errors)


def test_tls_error_is_reported_not_raised(api):
    module, fake = api
    with mock.patch.object(module.requests, "request",
                           side_effect=requests.exceptions.SSLError("bad cert")):
        assert module.api_request("GET", "doctors/") is None
    assert fake.errors


def test_unsupported_method_is_a_programming_error(api):
    module, _ = api
    with pytest.raises(ValueError):
        module.api_request("TRACE", "doctors/")


# ---------------------------------------------------------------------------
# BUG-015 follow-up — expired tokens must return the user to the login screen
# ---------------------------------------------------------------------------
def test_401_on_an_authenticated_request_clears_the_session(api):
    module, fake = api
    fake.session_state.update({"token": "dead-token", "role": "patient",
                               "user_name": "Pat", "user_id": 1})
    with mock.patch.object(module.requests, "request",
                           return_value=_response(401)):
        with pytest.raises(_Rerun):
            module.api_request("GET", "profile/")

    assert fake.session_state["token"] is None
    assert fake.session_state["role"] is None
    assert fake.reran is True
    assert any("expired" in w.lower() for w in fake.warnings), \
        "the user must be told why they were signed out"


def test_401_without_a_token_does_not_touch_the_session(api):
    """A 401 on an unauthenticated call must not trigger a bogus 'expired' rerun."""
    module, fake = api
    fake.session_state.update({"token": None})
    with mock.patch.object(module.requests, "request",
                           return_value=_response(401)):
        res = module.api_request("GET", "doctors/")
    assert res is not None and res.status_code == 401
    assert fake.reran is False


# ---------------------------------------------------------------------------
# BUG-017 — a failing profile fetch stranded the user on the login screen
# ---------------------------------------------------------------------------
def test_login_completes_even_if_the_profile_fetch_fails(api):
    module, fake = api

    def responses(method, url, **kwargs):
        if url.endswith("login/"):
            return _response(200, b'{"token": "t0k", "role": "patient"}')
        return _response(500, b'{"error": "boom"}')

    with mock.patch.object(module.requests, "request", side_effect=responses):
        with pytest.raises(_Rerun):
            module.handle_login("jane", "pw")

    assert fake.session_state["token"] == "t0k"
    assert fake.session_state["user_name"] == "jane"
    assert fake.reran is True, "the app must advance past the login screen"


def test_bad_credentials_report_invalid_login(api):
    module, fake = api
    with mock.patch.object(module.requests, "request",
                           return_value=_response(400, b'{"error": "Invalid credentials"}')):
        module.handle_login("jane", "wrong")
    assert any("Invalid Username or Password" in e for e in fake.errors)
    assert fake.session_state.get("token") is None


def test_throttled_login_says_so(api):
    module, fake = api
    with mock.patch.object(module.requests, "request",
                           return_value=_response(429, b'{"error": "throttled"}')):
        module.handle_login("jane", "pw")
    assert any("Too many" in e for e in fake.errors)


def test_unreachable_backend_does_not_claim_bad_credentials(api):
    module, fake = api
    with mock.patch.object(module.requests, "request",
                           side_effect=requests.exceptions.ConnectionError()):
        module.handle_login("jane", "pw")
    assert not any("Invalid Username" in e for e in fake.errors)


# ---------------------------------------------------------------------------
# BUG-012 (client half) — uploads must carry filename + content type
# ---------------------------------------------------------------------------
def test_upload_part_sends_name_bytes_and_type():
    import ast
    from pathlib import Path
    source = Path(__file__).resolve().parent.parent / "streamlit_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    helper = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_upload_part")
    returned = ast.unparse(helper.body[-1])
    assert "uploaded.name" in returned
    assert "getvalue()" in returned
    assert "uploaded.type" in returned

    # ...and no caller passes bare bytes any more.
    text = source.read_text(encoding="utf-8")
    assert "getvalue()}" not in text, "a bare-bytes upload remains in streamlit_app.py"
