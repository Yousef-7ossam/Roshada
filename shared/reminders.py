"""Medication reminder helpers shared by the frontends.

Reminders are evaluated on the Streamlit script run, not on a background thread.
A worker thread cannot drive the UI: ``st.toast`` outside a ScriptRunContext is a
no-op, so the previous thread-based implementation never showed anything while
leaking one never-terminating thread (and one global job registration) per
session. Checking elapsed time during the normal rerun removes both problems and
drops the external ``schedule`` dependency.
"""
import time

import streamlit as st

# Minutes between medication reminders.
REMINDER_INTERVAL_MINUTES = 60

_LAST_KEY = "_reminder_last_shown"


def ensure_reminders(interval_minutes: int = REMINDER_INTERVAL_MINUTES):
    """Show a medication reminder when the interval has elapsed.

    Safe to call on every rerun: it only fires once per interval per session.
    Returns True when a reminder was shown.
    """
    now = time.monotonic()
    last = st.session_state.get(_LAST_KEY)

    if last is None:
        # First run of the session starts the clock without nagging immediately.
        st.session_state[_LAST_KEY] = now
        return False

    if now - last < interval_minutes * 60:
        return False

    st.session_state[_LAST_KEY] = now
    st.toast("Reminder: Time to take your medication!", icon="💊")
    return True
