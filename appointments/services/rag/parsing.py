"""Stage 1 — parsing: raw bytes or a file into plain text plus a title.

Deliberately narrow. Only formats the corpus actually contains are supported;
each extra parser is a dependency and an attack surface, and an unsupported
format must fail loudly rather than silently ingesting mojibake that then gets
embedded and retrieved as though it were prose.

Markdown is parsed structurally rather than stripped, because its headings are
the best available signal for the section metadata that :mod:`.chunking` needs.
"""
import re
from dataclasses import dataclass, field
from pathlib import Path

#: Extensions we can parse, mapped to the stored ``Document.source_type``.
SUPPORTED_SUFFIXES = {
    ".txt": "text",
    ".text": "text",
    ".md": "markdown",
    ".markdown": "markdown",
}

_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT_RE = re.compile(r"^(=+|-+)\s*$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class UnsupportedDocument(Exception):
    """The file cannot be parsed. An ingestion-time fault, never a query fault."""


@dataclass
class ParsedDocument:
    text: str
    title: str = ""
    source_type: str = "text"
    metadata: dict = field(default_factory=dict)


def _decode(raw):
    """Bytes to text, tolerating a BOM and non-UTF-8 corpora."""
    if isinstance(raw, str):
        return raw
    for encoding in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # latin-1 cannot fail, so reaching here means `raw` was not bytes at all.
    raise UnsupportedDocument("Could not decode the document.")


def _split_frontmatter(text):
    """Pull a leading ``---`` block off as metadata, as prompts do."""
    match = _FRONTMATTER_RE.match(text.replace("\r\n", "\n"))
    if not match:
        return text, {}

    metadata = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        metadata[key.strip()] = value.strip().strip("'\"")
    return text[match.end():], metadata


def heading_of(line, previous_line=None):
    """The heading text and level for a line, or ``None``.

    Handles both ATX (``## Title``) and setext (``Title`` over ``-----``).
    """
    atx = _ATX_HEADING_RE.match(line)
    if atx:
        return atx.group(2).strip(), len(atx.group(1))
    if previous_line and previous_line.strip() and _SETEXT_RE.match(line):
        return previous_line.strip(), 1 if line.startswith("=") else 2
    return None


def _first_heading(text):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        found = heading_of(line, lines[index - 1] if index else None)
        if found:
            return found[0]
    return ""


def parse_text(raw, *, title="", source_type="text"):
    """Parse in-memory content."""
    text, metadata = _split_frontmatter(_decode(raw))
    if not text.strip():
        raise UnsupportedDocument("The document is empty.")

    resolved_title = (title or metadata.get("title")
                      or (_first_heading(text) if source_type == "markdown" else "")
                      or "Untitled document")
    return ParsedDocument(text=text, title=resolved_title,
                          source_type=source_type, metadata=metadata)


def parse_file(path):
    """Parse a file from disk, choosing the parser by extension."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedDocument(
            f"Cannot parse '{path.name}': {suffix or 'no extension'} is not "
            f"supported. Supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}.")
    if not path.is_file():
        raise UnsupportedDocument(f"No such document: {path}")

    return parse_text(path.read_bytes(),
                      title=path.stem.replace("_", " ").replace("-", " ").strip(),
                      source_type=SUPPORTED_SUFFIXES[suffix])
