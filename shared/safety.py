"""Medical safety guardrails for the AI assistant.

The assistant answers health questions from the public. Two failure modes matter
more than answer quality:

1. A user describing a medical emergency gets a chat reply instead of being told
   to seek immediate care.
2. A user asks for a specific dose or a prescription-only medicine and receives
   what reads like a prescription.

These checks run locally before and after the model call. They are deliberately
conservative: a false positive costs an extra banner, a false negative can cost
much more. They are a safety net, not a diagnostic tool, and never replace the
model's own refusals.
"""
import re

# Red-flag presentations where the correct answer is always "seek care now".
# Kept as word-boundary patterns so "heartburn" does not trigger "heart attack".
EMERGENCY_PATTERNS = [
    (r"\bchest (pain|pressure|tightness)\b", "chest pain"),
    (r"\bheart attack\b", "heart attack"),
    (r"\b(can'?t|cannot|difficulty|trouble) breath(e|ing)\b", "breathing difficulty"),
    (r"\bshort(ness)? of breath\b", "shortness of breath"),
    (r"\bstroke\b", "stroke"),
    (r"\b(face|facial) droop", "stroke sign"),
    (r"\bslurred speech\b", "stroke sign"),
    (r"\bnumb(ness)? (on |in )?(one|left|right) side\b", "stroke sign"),
    (r"\bsevere bleeding\b|\bbleeding heavily\b|\bwon'?t stop bleeding\b", "severe bleeding"),
    (r"\bunconscious\b|\bpassed out\b|\bfaint(ed|ing)\b", "loss of consciousness"),
    (r"\bseizure\b|\bconvuls", "seizure"),
    (r"\bsuicid|\bkill myself\b|\bend my life\b|\bself[- ]harm\b", "self-harm"),
    (r"\boverdose\b|\btook too many\b", "overdose"),
    (r"\bpoison(ed|ing)?\b", "poisoning"),
    (r"\banaphylax|\bthroat closing\b|\bswollen (throat|tongue)\b", "severe allergic reaction"),
    (r"\bblood in (my )?(vomit|stool)\b|\bvomiting blood\b", "internal bleeding"),
    (r"\bsevere abdominal pain\b", "severe abdominal pain"),
    (r"\bhigh fever\b.*\b(stiff neck|rash)\b", "possible meningitis"),
]

_EMERGENCY_RE = [(re.compile(p, re.IGNORECASE), label) for p, label in EMERGENCY_PATTERNS]

EMERGENCY_NOTICE = (
    "🚨 **This may be a medical emergency.**\n\n"
    "If you or someone else is experiencing this now, stop and get help "
    "immediately — call your local emergency number (**123** in Egypt) or go to "
    "the nearest emergency department.\n\n"
    "Do not wait for an answer here."
)

# The model-facing safety rules used to live here as a Python string, appended
# to the system prompt. They now live in the prompt library, as
# `appointments/services/ai/prompts/library/_fragments/safety.md`, with every
# other instruction Roshada gives a model — a prompt in a module that also does
# regex matching is exactly the scattering the prompt system exists to end.
#
# What stays here is what is *not* a prompt: the deterministic red-flag patterns
# below, and the notice shown to the user. Both run with no model involved, at
# both tiers, so they survive a provider outage.


def detect_emergency(text):
    """Return the matched red-flag label, or None.

    >>> detect_emergency("I have chest pain and my arm is numb")
    'chest pain'
    >>> detect_emergency("what foods lower cholesterol?") is None
    True
    """
    if not text:
        return None
    for pattern, label in _EMERGENCY_RE:
        if pattern.search(text):
            return label
    return None


def is_emergency(text):
    return detect_emergency(text) is not None
