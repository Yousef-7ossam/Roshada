"""Stage 2 — cleaning: normalise text before it is chunked and embedded.

Cleaning is not cosmetic here. Two identical passages that differ only in
whitespace or Unicode form embed to different vectors and are stored as
different chunks, so the corpus quietly accumulates near-duplicates that then
crowd out genuine matches in the top-k.

Arabic needs explicit handling: Roshada serves Arabic users, and Arabic text
arrives with presentation forms, tatweel padding and optional diacritics that
carry no meaning for retrieval but do change the token stream.
"""
import hashlib
import re
import unicodedata

#: Arabic diacritics (harakat) and the tatweel elongation character. Both are
#: decorative: "مَرِيض" and "مريض" are the same word to a reader and must be the
#: same token to the index.
_ARABIC_DIACRITICS_RE = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
_TATWEEL = "ـ"

#: Zero-width and bidi control characters — invisible, and they split tokens.
_INVISIBLE_RE = re.compile(r"[​-‏‪-‮⁠﻿]")

_TRAILING_SPACE_RE = re.compile(r"[ \t]+$", re.MULTILINE)
_SPACE_RUN_RE = re.compile(r"[ \t]{2,}")
#: Three or more newlines collapse to a paragraph break.
_BLANK_RUN_RE = re.compile(r"\n{3,}")
#: Markdown/plain-text horizontal rules and table separator rows carry no prose.
_RULE_RE = re.compile(r"^\s*([-*_=]\s*){3,}$", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]{5,}\|?\s*$", re.MULTILINE)


def normalise_arabic(text):
    """Strip decorative marks so Arabic variants map to one token stream."""
    text = _ARABIC_DIACRITICS_RE.sub("", text)
    return text.replace(_TATWEEL, "")


def clean(text):
    """Normalise a document's text for chunking and embedding."""
    if not text:
        return ""

    # NFKC folds Arabic presentation forms and full-width Latin onto their
    # canonical code points, so the same word indexes identically whatever
    # keyboard or PDF produced it.
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _INVISIBLE_RE.sub("", text)
    text = normalise_arabic(text)

    text = _RULE_RE.sub("", text)
    text = _TABLE_SEP_RE.sub("", text)
    text = _TRAILING_SPACE_RE.sub("", text)
    text = _SPACE_RUN_RE.sub(" ", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


def checksum(text):
    """Stable digest of cleaned text, so re-ingesting unchanged content is free."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()
