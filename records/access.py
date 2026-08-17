"""Who may open a patient's medical record at all.

Deliberately its own module with almost no dependencies, so a domain app can
import the rule without pulling in the records API layer — and so there is
exactly one answer to the question rather than one per contributor.

**This is the outer gate, not the whole check.** Passing it means "you may look
at this record"; it says nothing about which events you then see. Each source
module still decides that for itself from its own scoped queryset, which is
what keeps an unreleased report invisible here even to someone who cleared this
gate.
"""
from accounts import roles
from accounts.services import role_of
from appointments.services import care


def may_view(viewer, patient):
    """True when this viewer is entitled to open this patient's record.

    A patient may open their own. A doctor may open one for a patient they
    actually treat — the platform's existing care-relationship rule, reused
    rather than restated so the medical record cannot drift into a looser
    definition of "my patient" than the rest of the product uses.

    Everyone else is refused, administrators included: administering the
    platform is not a reason to read somebody's medical history, which is the
    same line the radiology and pharmacy modules already draw.
    """
    if viewer is None or patient is None:
        return False
    role = role_of(viewer)
    if role == roles.PATIENT:
        return viewer.pk == patient.pk
    if role == roles.DOCTOR:
        return care.treats_patient(viewer, patient)
    return False


def is_subject(viewer, patient):
    """True when the viewer is the patient whose record this is."""
    return viewer is not None and patient is not None and viewer.pk == patient.pk
