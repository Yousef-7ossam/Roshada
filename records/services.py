"""Unified Medical Record use-cases.

This layer only ever *reads*. Nothing here writes to a lab result, a radiology
report, a prescription or an appointment. The medical record is a
viewing and aggregation layer, and the modules that own the data are the only
ones that change it. The one row this module owns is the record's own metadata.

Access is two gates, and keeping them separate is the point:

1. ``access.may_view`` — may this viewer open this record at all?
2. each source's own scoped queryset — which events do they then see?

The second gate is what makes the aggregation safe. A viewer who clears the
first gate still only sees what each module would have shown them directly, so
the medical record cannot become a way around a rule the source enforces.
"""
import logging

from django.contrib.auth.models import User
from django.utils import timezone

from accounts import roles
from accounts.services import role_of
from appointments.models import Appointment

from . import access, timeline
from .models import MedicalRecord

logger = logging.getLogger("appointments")


class NotFound(Exception):
    """No such record — also raised instead of a permission error where telling
    the two apart would confirm that a patient id exists."""


def record_for(patient):
    """The patient's record, created on first access.

    ``get_or_create`` rather than a backfill: a record is brought into
    existence when somebody actually looks at one, so the table holds records
    that mean something instead of a row per user account.
    """
    record, created = MedicalRecord.objects.get_or_create(patient=patient)
    if created:
        logger.info("Medical record %s opened for %s", record.id,
                    patient.username)
    return record


def resolve_patient(viewer, patient_id=None):
    """The subject of this request, after checking the viewer may see them.

    ``patient_id`` is never trusted on its own — it selects a candidate, and
    ``access.may_view`` decides. A patient asking for somebody else's id, and a
    doctor asking for a patient they do not treat, both get ``NotFound``:
    answering "forbidden" would confirm the record exists, which is what
    someone probing ids wants to learn.
    """
    if patient_id is None:
        return viewer
    try:
        patient = User.objects.select_related("account").get(pk=int(patient_id))
    except (User.DoesNotExist, ValueError, TypeError):
        raise NotFound()
    if role_of(patient) != roles.PATIENT:
        raise NotFound()
    if not access.may_view(viewer, patient):
        raise NotFound()
    return patient


def open_record(viewer, patient_id=None):
    """Resolve the subject, check access, return (patient, record)."""
    patient = resolve_patient(viewer, patient_id)
    if not access.may_view(viewer, patient):
        raise NotFound()
    if not access.is_subject(viewer, patient):
        # Roshada has no audit-log table; the platform's existing behaviour for
        # sensitive reads is an application log line, the same as radiology
        # writes when it serves an imaging file. Preserved here rather than
        # replaced with a second audit system.
        logger.info("Medical record of %s opened by %s (%s)", patient.username,
                    viewer.username, role_of(viewer))
    return patient, record_for(patient)


def timeline_for(viewer, patient, types=None, since=None, until=None,
                 search="", limit=25, offset=0):
    """The merged medical timeline, already filtered and paginated."""
    entries, seen, has_more = timeline.build(
        viewer, patient, types=types, since=since, until=until,
        search=search, limit=limit, offset=offset)
    return {
        "results": [entry.as_dict() for entry in entries],
        "count": len(entries),
        "offset": offset,
        "limit": limit,
        "has_more": has_more,
        # What the sources produced within the window that was fetched — not a
        # claim about the patient's entire history, which would mean loading
        # all of it to count.
        "matched_in_window": seen,
    }


def overview(viewer, patient, record):
    """The record's landing view: recent activity, grouped by kind.

    Built from the same timeline the detail view uses, so the overview cannot
    show an event the timeline would have hidden — one code path, one set of
    permissions.
    """
    entries, _seen, _more = timeline.build(viewer, patient, limit=60)

    by_type = {}
    for entry in entries:
        by_type.setdefault(entry.type, []).append(entry)

    def recent(event_type, count=3):
        return [entry.as_dict() for entry in by_type.get(event_type, [])[:count]]

    upcoming = _upcoming_appointments(viewer, patient)

    return {
        "record": {
            "id": record.id,
            "status": record.status,
            "status_label": record.get_status_display(),
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
        },
        "patient": {
            "id": patient.id,
            "name": patient.get_full_name() or patient.username,
            "username": patient.username,
        },
        "is_own_record": access.is_subject(viewer, patient),
        "counts": {event_type: len(by_type.get(event_type, []))
                   for event_type in timeline.ALL_TYPES},
        "recent_activity": [entry.as_dict() for entry in entries[:8]],
        "recent_consultations": recent(timeline.CONSULTATION),
        "recent_lab_results": recent(timeline.LAB_RESULT),
        "recent_radiology": recent(timeline.RADIOLOGY_REPORT),
        "recent_prescriptions": recent(timeline.PRESCRIPTION),
        "recent_medications": recent(timeline.MEDICATION_ORDER),
        "upcoming_appointments": upcoming,
        # Kinds the platform genuinely has no source for yet. Named rather than
        # shown as an empty list, so "nothing here" is distinguishable from
        # "not built" — the same honesty the dashboards already apply.
        "unavailable_types": unavailable_types(),
    }


def _upcoming_appointments(viewer, patient):
    """Future bookings, which a timeline of *history* would otherwise bury."""
    if not access.may_view(viewer, patient):
        return []
    from appointments.serializers import provider_brief

    queryset = (Appointment.objects
                .filter(patient=patient, status=Appointment.SCHEDULED,
                        start_at__gte=timezone.now())
                .select_related("provider", "provider__account",
                                "provider__doctor_profile",
                                "provider__laboratory_profile",
                                "provider__radiology_profile", "service")
                .order_by("start_at")[:5])
    upcoming = []
    for appointment in queryset:
        provider = provider_brief(appointment.provider)
        upcoming.append({
            "id": appointment.id,
            "date": appointment.date.isoformat(),
            "time": appointment.time.strftime("%H:%M"),
            "provider": provider["name"],
            "provider_role": provider["role"],
            "service": appointment.service.name if appointment.service else None,
            "status": appointment.status,
        })
    return upcoming


def unavailable_types():
    """Event kinds with no module behind them yet.

    Derived from which types no registered source can produce, so it stops
    being reported the moment a module starts publishing them — the same
    self-correcting shape the dashboard's ``unsupported_metrics`` uses.
    """
    return sorted(set(timeline.ALL_TYPES) - producible_types())


#: Types the registered sources are able to emit. Declared alongside each
#: contributor would be tidier, but a contributor deciding at runtime which
#: kinds it yields is exactly what makes this a *derived* answer.
_PRODUCIBLE = {
    "appointment_entries": {timeline.APPOINTMENT, timeline.CONSULTATION},
    "radiology_timeline_entries": {timeline.RADIOLOGY_ORDER,
                                   timeline.RADIOLOGY_REPORT},
    "pharmacy_timeline_entries": {timeline.PRESCRIPTION,
                                  timeline.MEDICATION_ORDER},
}


def producible_types():
    produced = set()
    for contributor in timeline.registered():
        produced |= _PRODUCIBLE.get(getattr(contributor, "__name__", ""), set())
    return produced


def patients_for(doctor):
    """The patients whose records this doctor may open.

    The platform's existing care-relationship rule: an appointment between
    them. Reused rather than restated, so the medical record cannot drift into
    a looser definition of "my patient" than the care-relationship rule uses.
    """
    if role_of(doctor) != roles.DOCTOR:
        return User.objects.none()
    patient_ids = (Appointment.objects.filter(provider=doctor)
                   .values_list("patient_id", flat=True).distinct())
    from django.db.models import Q
    return (User.objects.filter(pk__in=patient_ids)
            .filter(Q(account__role=roles.PATIENT) | Q(account__isnull=True))
            .order_by("first_name", "username"))
