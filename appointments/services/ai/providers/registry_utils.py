"""Credential hygiene, shared by every adapter.

Kept in its own module so :mod:`.registry` can import the adapters and the
adapters can import this, without a cycle.
"""
import os

#: Fragments that only ever appear in an unedited ``.env.example`` value.
#: A real key from any provider (``sk-…``, ``sk-or-v1-…``, ``AIza…``) contains
#: none of them.
PLACEHOLDER_MARKERS = (
    "your-", "your_", "-key-here", "change-me", "changeme",
    "replace-me", "replaceme", "generate-a-", "xxxxxxxx",
)


def is_placeholder(value) -> bool:
    """True when a credential is still the example value.

    Every provider is checked, not just one. Previously only the OpenAI client
    did this, so a verbatim copy of ``.env.example`` made
    ``GROQ_API_KEY`` look configured — the assistant would select
    a provider, then fail with a 401 at call time instead of reporting itself as
    unconfigured up front.
    """
    lowered = (value or "").strip().lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def env_key(name) -> str:
    """A credential from the environment, blank when absent or a placeholder."""
    value = os.environ.get(name) or ""
    return "" if is_placeholder(value) else value
