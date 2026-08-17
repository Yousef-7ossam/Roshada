"""User-aware context assembly.

The assistant answers questions about the user's own health, so it needs to know
who is asking. Running server-side is what makes that possible: the pipeline
reads the record directly instead of asking the browser tier to send it.

Two things are assembled here:

* **facts** — a compact block folded into the system prompt (profile, upcoming
  appointments; for a doctor, their own schedule).
* **sources** — the provenance of those facts, returned to the UI so a reply
  can show *what it was allowed to look at*. This is attribution over the
  user's own record only. Retrieval from the medical knowledge base is a
  separate branch of the pipeline (:mod:`.grounding`) and deliberately does
  not read anything assembled here.

Conversation turns come from :mod:`appointments.services.chat`, which is now the
single source of the assistant's memory. The frontend previously kept its own
20-turn window in ``st.session_state``, so the server's policy was dead code and
the real window depended on the browser session.

Everything is bounded: the block is capped by :data:`MAX_CONTEXT_CHARS`, because
context is re-sent on every message and unbounded growth costs latency, tokens
and eventually the model's context window.
"""
from dataclasses import dataclass, field

from ...serializers import provider_brief
from .. import chat, scheduling

#: Upper bound on the rendered facts block. Prompts are re-sent every turn.
MAX_CONTEXT_CHARS = 1200
#: Free-text medical history is the one unbounded field a user can type into.
MAX_HISTORY_CHARS = 400
#: How many records of each kind to include.
MAX_APPOINTMENTS = 3


@dataclass(frozen=True)
class Source:
    """One piece of the user's record that informed the answer."""
    kind: str      # profile | appointments | conversation
    label: str     # shown to the user as an attribution chip

    def as_dict(self):
        return {"kind": self.kind, "label": self.label}


@dataclass
class AssembledContext:
    role: str
    facts: str = ""
    turns: list = field(default_factory=list)
    sources: list = field(default_factory=list)

    def source_dicts(self):
        return [s.as_dict() for s in self.sources]


def role_of(user) -> str:
    """The caller's role, from the account record.

    Read from ``accounts.services``, which is the platform's single source of
    truth and covers all six roles including users created outside registration.
    Sniffing for a ``doctor_profile`` attribute — what this used to do — could
    only ever answer "doctor or patient", and answered "patient" for a
    laboratory.
    """
    from accounts.services import role_of as resolve

    return resolve(user)


def _truncate(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ---------------------------------------------------------------------------
# Fact builders — each returns (lines, Source) or (None, None)
# ---------------------------------------------------------------------------
def _patient_profile_facts(user):
    profile = getattr(user, "patient_profile", None)
    if profile is None:
        return None, None

    bits = []
    if profile.age:
        bits.append(f"age {profile.age}")
    if profile.gender:
        bits.append(profile.gender.lower())
    history = _truncate(profile.medical_history, MAX_HISTORY_CHARS)

    if not bits and not history:
        return None, None

    lines = ["Profile: " + (", ".join(bits) if bits else "not stated")]
    if history:
        lines.append(f"Medical history the patient recorded: {history}")
    return lines, Source("profile", "Your profile")


def _appointment_facts(user, role):
    lister = (scheduling.upcoming_for_provider if role == "doctor"
              else scheduling.upcoming_for_patient)
    upcoming = list(lister(user, limit=MAX_APPOINTMENTS))
    if not upcoming:
        return None, None

    lines = ["Upcoming appointments:"]
    for appointment in upcoming:
        # The counterparty is a provider of some kind, so it is described
        # through the provider brief rather than assumed to be a doctor.
        if role == "doctor":
            who = (appointment.patient.get_full_name()
                   or appointment.patient.username)
        else:
            provider = provider_brief(appointment.provider)
            title = "Dr. " if provider["role"] == "doctor" else ""
            detail = f" ({provider['detail']})" if provider["detail"] else ""
            who = f"{title}{provider['name']}{detail}"
        when = f"{appointment.date.isoformat()} at {appointment.time.strftime('%H:%M')}"
        reason = _truncate(appointment.reason, 80)
        service = f" for {appointment.service.name}" if appointment.service else ""
        lines.append(f"- {when} with {who}{service}"
                     + (f" — {reason}" if reason else ""))

    label = ("Your schedule" if role == "doctor" else "Your upcoming appointments")
    return lines, Source("appointments", label)


def _doctor_profile_facts(user):
    profile = getattr(user, "doctor_profile", None)
    if profile is None:
        return None, None
    return ([f"You are speaking with Dr. {profile.name}, "
             f"specialisation: {profile.specialization}."],
            Source("profile", "Your profile"))


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build(user, turns=chat.CONTEXT_TURNS) -> AssembledContext:
    """Assemble prompt context for ``user``.

    Must be called **before** the current exchange is recorded, so the prompt
    does not contain the question it is being asked to answer.
    """
    role = role_of(user)
    assembled = AssembledContext(role=role)

    if role == "doctor":
        builders = [_doctor_profile_facts(user), _appointment_facts(user, role)]
    else:
        builders = [_patient_profile_facts(user),
                    _appointment_facts(user, role)]

    blocks = []
    for lines, source in builders:
        if not lines:
            continue
        blocks.append("\n".join(lines))
        assembled.sources.append(source)

    assembled.facts = _truncate("\n\n".join(blocks), MAX_CONTEXT_CHARS)

    assembled.turns = chat.context_messages(user, turns=turns)
    if assembled.turns:
        assembled.sources.append(
            Source("conversation", "Your earlier messages"))

    return assembled
