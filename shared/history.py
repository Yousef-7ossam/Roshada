"""Chat transcript display for the AI assistant — read and clear, nothing else.

This module previously read a shared ``chat_history.jsonl`` on the frontend host.
That file carried no user identity, so the history panel showed every signed-in
user the most recent questions asked by anyone. History now lives in the database
behind authenticated endpoints, so a user can only ever see their own.

Two helpers were removed when the assistant moved server-side:

* ``save_exchange`` — ``POST /api/chat/ask/`` records the turn pair itself.
  Calling both stored every exchange twice.
* ``conversation_context`` — it wrapped ``GET /api/chat/context/`` but nothing
  ever called it: the page kept its own divergent 20-turn window in
  ``st.session_state`` instead, so the server's policy was dead code. The
  pipeline now assembles that window itself, on the server, where the records
  are. The endpoint remains for clients that want to inspect the window.
"""
from shared.api import api_request


def load_messages(limit=50):
    """The signed-in user's own history, oldest-first. [] when unavailable."""
    res = api_request("GET", f"chat/history/?limit={int(limit)}")
    if res is not None and res.status_code == 200:
        return res.json()
    return []


def clear_history():
    """Delete the signed-in user's history. Returns True on success."""
    res = api_request("DELETE", "chat/history/")
    return res is not None and res.status_code == 200
