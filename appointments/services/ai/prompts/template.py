"""Prompt files: parsing, composition and rendering.

A prompt is a Markdown file with a small frontmatter block. Prose belongs in a
prose file — not in a Python string literal — so a prompt can be read, reviewed
and edited by someone who does not write Python, and a change to what the model
is told is never a change to application logic.

    ---
    name: example_prompt
    version: 1.2.0
    purpose: medical_assistant
    description: One line on what this prompt is for.
    includes: [style, safety]
    variables: [context_block]
    ---
    <the instructions, as prose>

(The real ones live in ``library/``; this module deliberately contains no
instruction text of its own — a test enforces that.)

Substitution uses ``{{name}}`` rather than ``str.format``'s ``{name}``: prompts
are prose that legitimately contains braces (JSON examples, units like
``{0,1}``), and a single stray brace in ``format`` is a crash at request time.

Two rules are enforced at load, not at request time, so a malformed prompt fails
the test suite instead of a patient's question:

* every variable used in the body must be declared in ``variables``,
* every declared variable must be supplied at render time.
"""
import re
from dataclasses import dataclass, field

#: ``{{ variable_name }}`` — whitespace tolerated.
PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


class PromptError(Exception):
    """A prompt file is malformed, missing, or rendered with bad variables."""


def _parse_scalar(raw):
    """Frontmatter values: a bare string, or an inline ``[a, b]`` list."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [part.strip().strip("'\"") for part in inner.split(",") if part.strip()]
    return raw.strip("'\"")


def parse(text, *, source="<string>"):
    """Split a prompt file into (metadata dict, body)."""
    match = _FRONTMATTER_RE.match(text.replace("\r\n", "\n"))
    if not match:
        raise PromptError(f"{source}: missing the '---' frontmatter block.")

    meta = {}
    for line_number, line in enumerate(match.group(1).splitlines(), start=2):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise PromptError(f"{source}:{line_number}: expected 'key: value'.")
        key, _, value = line.partition(":")
        meta[key.strip()] = _parse_scalar(value)
    return meta, match.group(2).strip()


@dataclass(frozen=True)
class PromptTemplate:
    """One named, versioned prompt."""
    name: str
    version: str
    body: str
    purpose: str = "general"
    description: str = ""
    includes: tuple = ()
    variables: tuple = ()
    #: ``active`` — used by running code. ``planned`` — registered so the
    #: category exists and is reviewable, but nothing calls it yet (RAG, agents).
    #: A test asserts nothing in application code renders a ``planned`` prompt,
    #: so the distinction cannot quietly rot.
    status: str = "active"
    source: str = "<string>"

    # -- identity --------------------------------------------------------
    @property
    def id(self):
        """``name@version`` — what gets recorded against an answer.

        A stored reply can always be traced to the exact instructions that
        produced it, which a single library-wide version number could not do.
        """
        return f"{self.name}@{self.version}"

    # -- construction ----------------------------------------------------
    @classmethod
    def from_text(cls, text, *, source="<string>"):
        meta, body = parse(text, source=source)

        for required in ("name", "version"):
            if not meta.get(required):
                raise PromptError(f"{source}: frontmatter is missing '{required}'.")
        if not _SEMVER_RE.match(str(meta["version"])):
            raise PromptError(
                f"{source}: version '{meta['version']}' is not MAJOR.MINOR.PATCH.")
        if not body:
            raise PromptError(f"{source}: the prompt body is empty.")

        as_tuple = lambda value: tuple(value) if isinstance(value, list) else \
            ((value,) if value else ())

        template = cls(
            name=str(meta["name"]), version=str(meta["version"]), body=body,
            purpose=str(meta.get("purpose") or "general"),
            description=str(meta.get("description") or ""),
            includes=as_tuple(meta.get("includes")),
            variables=as_tuple(meta.get("variables")),
            status=str(meta.get("status") or "active"),
            source=source,
        )
        if template.status not in ("active", "planned"):
            raise PromptError(
                f"{source}: status must be 'active' or 'planned', "
                f"got '{template.status}'.")
        template.check()
        return template

    # -- validation ------------------------------------------------------
    def used_variables(self):
        return set(PLACEHOLDER_RE.findall(self.body))

    def check(self):
        """Fail at load time for anything that would fail at request time."""
        undeclared = self.used_variables() - set(self.variables)
        if undeclared:
            raise PromptError(
                f"{self.source}: uses {sorted(undeclared)} but does not declare "
                f"them in 'variables'.")

    # -- rendering -------------------------------------------------------
    def render(self, *, fragments=None, **values):
        """Compose includes + body, then substitute every ``{{variable}}``.

        Included fragments are prepended in declared order, so shared rules
        (style, safety) sit above the specific instructions.
        """
        parts = []
        for name in self.includes:
            fragment = (fragments or {}).get(name)
            if fragment is None:
                raise PromptError(
                    f"{self.source}: includes unknown fragment '{name}'.")
            parts.append(fragment.render(fragments=fragments, **values))
        parts.append(self.body)
        composed = "\n\n".join(part for part in parts if part)

        missing = set(PLACEHOLDER_RE.findall(composed)) - set(values)
        if missing:
            raise PromptError(
                f"{self.name}: no value supplied for {sorted(missing)}.")

        return PLACEHOLDER_RE.sub(
            lambda match: str(values[match.group(1)]), composed).strip()


@dataclass
class PromptBundle:
    """Every prompt discovered, keyed by name."""
    templates: dict = field(default_factory=dict)
    fragments: dict = field(default_factory=dict)

    def get(self, name):
        template = self.templates.get(name) or self.fragments.get(name)
        if template is None:
            raise PromptError(
                f"No prompt named '{name}'. Known: "
                f"{', '.join(sorted(self.templates))}.")
        return template

    def by_purpose(self, purpose):
        return sorted((t for t in self.templates.values() if t.purpose == purpose),
                      key=lambda t: t.name)
