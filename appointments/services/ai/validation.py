"""Validation of what the model actually said.

The safety fragment in the prompt library *asks* the model not to prescribe or
state a dose. This module checks whether it complied. The distinction matters:
an instruction is a request, and a patient acting on a hallucinated dose is not
recoverable.

Scope is deliberately modest:

* a reply that is empty or unusable is rejected outright,
* a reply that states a specific dose is flagged,
* a reply that reads like a prescription or diagnosis is flagged,
* a reply that describes an emergency without saying "seek care" is flagged.

Flagged replies are **surfaced with a warning, never silently rewritten** — a
patient should see what the assistant said plus the caveat, not an edited
version they cannot audit. The one exception is rejection, where there is no
answer to show.

A fuller safety layer — multilingual red flags, PHI redaction, jailbreak
resistance — is not built. This is the guard that ships with the pipeline,
and it is deliberately the kind that cannot fail open.
"""
import re
from dataclasses import dataclass, field

from shared.safety import detect_emergency

#: "500 mg", "10mcg", "2 units", "1.5 ml" — a specific quantity of a medicine.
_DOSE_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:mg|mcg|µg|ml|g|iu|units?|tablets?|pills?|capsules?)\b",
    re.IGNORECASE)

#: Language that asserts a diagnosis or issues a prescription.
_PRESCRIPTION_RE = re.compile(
    r"\b(i (?:prescribe|diagnose)|you (?:have|are suffering from|are diagnosed with)"
    r"|take \d|start taking|increase your dose|stop taking your)\b",
    re.IGNORECASE)

#: Wording that counts as directing the user to in-person care.
_SEEK_CARE_RE = re.compile(
    r"\b(emergency|911|123|ambulance|urgent(ly)?|immediate(ly)?|"
    r"seek (medical )?(care|help|attention)|go to (the )?(hospital|er)|"
    r"see a (doctor|clinician|physician))\b",
    re.IGNORECASE)

#: Below this a "reply" is a truncation or an error string, not an answer.
MIN_USEFUL_LENGTH = 2


@dataclass
class ValidationOutcome:
    """Result of inspecting a model reply."""
    text: str
    ok: bool = True
    warnings: list = field(default_factory=list)

    def as_dict(self):
        return {"ok": self.ok, "warnings": list(self.warnings)}


DOSE_WARNING = (
    "This answer mentions a specific dose. Roshada cannot prescribe — "
    "confirm any dose with your doctor or pharmacist before acting on it.")
PRESCRIPTION_WARNING = (
    "This answer reads like a diagnosis or prescription. It is general "
    "information only, not a clinical decision.")
UNSAFE_EMERGENCY_WARNING = (
    "This answer touches on a possible emergency without telling you to get "
    "help. If this is happening now, seek immediate in-person care.")


def validate(text) -> ValidationOutcome:
    """Inspect a model reply and report whether it is safe to show as-is."""
    text = (text or "").strip()

    if len(text) < MIN_USEFUL_LENGTH:
        return ValidationOutcome(text="", ok=False,
                                 warnings=["The assistant returned an empty reply."])

    outcome = ValidationOutcome(text=text)

    if _DOSE_RE.search(text):
        outcome.warnings.append(DOSE_WARNING)

    if _PRESCRIPTION_RE.search(text):
        outcome.warnings.append(PRESCRIPTION_WARNING)

    # A reply that raises a red flag must also point at real-world care.
    if detect_emergency(text) and not _SEEK_CARE_RE.search(text):
        outcome.warnings.append(UNSAFE_EMERGENCY_WARNING)

    return outcome
