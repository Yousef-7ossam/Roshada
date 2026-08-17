"""Prompt discovery, caching and overrides.

Prompts are loaded from ``library/`` on first use and cached. Fragments live in
``library/_fragments/`` and are composable building blocks rather than prompts
in their own right.

**Overrides.** ``PROMPT_DIR`` points at a directory of ``.md`` files that
replace or add to the shipped library, matched by filename. An operator can
change what the model is told — reword the safety rules, retune the persona,
add a prompt — by dropping a file in a mounted volume. No code change, no
rebuild, no redeploy of the package. That is the point of keeping prompts in
prose files.

Overrides are validated exactly like shipped prompts: a malformed override
raises rather than silently reverting to the built-in, because quietly ignoring
an operator's safety wording would be worse than failing loudly.
"""
import logging
import os
import threading
from pathlib import Path

from .template import PromptBundle, PromptError, PromptTemplate

logger = logging.getLogger("appointments")

LIBRARY_DIR = Path(__file__).resolve().parent / "library"
FRAGMENTS_DIRNAME = "_fragments"

_lock = threading.Lock()
_bundle = None


def override_dir():
    """Operator-supplied prompt directory, or ``None``."""
    raw = os.environ.get("PROMPT_DIR", "").strip()
    return Path(raw) if raw else None


def _load_dir(directory, bundle, *, fragments):
    if not directory or not directory.is_dir():
        return
    target = bundle.fragments if fragments else bundle.templates
    for path in sorted(directory.glob("*.md")):
        template = PromptTemplate.from_text(
            path.read_text(encoding="utf-8"), source=str(path))
        if template.name in target:
            logger.info("Prompt '%s' overridden by %s", template.name, path)
        target[template.name] = template


def _build():
    bundle = PromptBundle()

    _load_dir(LIBRARY_DIR / FRAGMENTS_DIRNAME, bundle, fragments=True)
    _load_dir(LIBRARY_DIR, bundle, fragments=False)

    custom = override_dir()
    if custom:
        if not custom.is_dir():
            # A typo in PROMPT_DIR must not silently mean "no overrides".
            raise PromptError(f"PROMPT_DIR '{custom}' is not a directory.")
        _load_dir(custom / FRAGMENTS_DIRNAME, bundle, fragments=True)
        _load_dir(custom, bundle, fragments=False)

    # Composition is resolved once, here, so a prompt that includes a fragment
    # nobody shipped fails at startup rather than mid-conversation.
    for template in list(bundle.templates.values()) + list(bundle.fragments.values()):
        for name in template.includes:
            if name not in bundle.fragments:
                raise PromptError(
                    f"{template.source}: includes unknown fragment '{name}'.")
    return bundle


def bundle():
    """The loaded prompt library (cached)."""
    global _bundle
    if _bundle is None:
        with _lock:
            if _bundle is None:
                _bundle = _build()
    return _bundle


def reload():
    """Drop the cache. Used by tests and after changing ``PROMPT_DIR``."""
    global _bundle
    with _lock:
        _bundle = None
    return bundle()


def get(name):
    """One prompt by name. Raises :class:`PromptError` if it does not exist."""
    return bundle().get(name)


def names():
    return sorted(bundle().templates)


def fragment_names():
    return sorted(bundle().fragments)


def by_purpose(purpose):
    return bundle().by_purpose(purpose)


def render(name, **values):
    """Render a prompt by name, composing its fragments."""
    loaded = bundle()
    return loaded.get(name).render(fragments=loaded.fragments, **values)


def catalogue():
    """Every prompt with its version and purpose — for docs and diagnostics."""
    loaded = bundle()
    return [
        {"name": t.name, "version": t.version, "purpose": t.purpose,
         "description": t.description, "includes": list(t.includes),
         "variables": list(t.variables)}
        for t in sorted(loaded.templates.values(), key=lambda t: (t.purpose, t.name))
    ]
