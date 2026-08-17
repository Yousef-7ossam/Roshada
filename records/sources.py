"""Timeline sources for the records the scheduling engine already owns.

Appointments live in ``appointments``, which is a lower layer
than any clinical module and must not import one — so their contributors are
registered here rather than from a domain app. Radiology and pharmacy register
their own (in their ``hooks`` modules), the same way they contribute to the
dashboard.

**Consultations.** Roshada has no consultation model. A record should show "the
minimum integration point necessary" rather than a new consultation system, so
a consultation is derived: a *completed* appointment with a doctor. That is a
real event with a real timestamp, and it invents nothing — an appointment that
has not happened yet is an appointment, not a consultation.
"""
from accounts import roles
from appointments.models import Appointment
from appointments.serializers import provider_brief

from . import access, timeline


def _visible_appointments(viewer, patient):
    """Appointments this viewer may see about this patient.

    Scoped from the viewer, never from an id in a request. A treating doctor
    sees the patient's booking history rather than only their own visits —
    sections 12 and 13 ask for "previous consultations" and "appointments",
    and a doctor who could see only the appointments they personally attended
    would be reading their own diary, not the patient's history. The gate for
    that is ``access.may_view``, i.e. an existing care relationship.
    """
    if not access.may_view(viewer, patient):
        return Appointment.objects.none()
    return (Appointment.objects
            .filter(patient=patient)
            .select_related("patient", "provider", "provider__account",
                            "provider__doctor_profile",
                            "provider__laboratory_profile",
                            "provider__radiology_profile", "service"))


@timeline.source
def appointment_entries(viewer, patient, limit):
    """Bookings of every kind — doctor, laboratory and radiology alike.

    One contributor covers all three because the unified appointment engine
    made them one thing. A completed doctor visit is emitted as a consultation
    instead, so the two never double-count.
    """
    entries = []
    for appointment in _visible_appointments(viewer, patient)[:limit]:
        provider = provider_brief(appointment.provider)
        is_consultation = (provider["role"] == roles.DOCTOR
                           and appointment.status == Appointment.COMPLETED)
        service = appointment.service.name if appointment.service else None
        entries.append(timeline.Entry(
            type=(timeline.CONSULTATION if is_consultation
                  else timeline.APPOINTMENT),
            at=appointment.start_at,
            title=(service or ("Consultation" if is_consultation
                               else f"{roles.label(provider['role'])} appointment")),
            source="appointments.Appointment",
            reference=appointment.id,
            status=appointment.status,
            status_label=appointment.get_status_display(),
            provider=provider["name"],
            detail=appointment.reason or provider["detail"] or "",
            extra={"provider_role": provider["role"]},
        ))
    return entries
