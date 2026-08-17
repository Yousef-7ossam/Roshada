"""How radiology attaches to the unified appointment engine.

Two callbacks, registered at startup. Everything else about scheduling —
availability, slots, rescheduling, double-booking protection — stays in the
engine and is not reimplemented, referenced or worked around here.
"""
import logging

from accounts import roles
from accounts.services import role_of
from appointments.services import dashboard, scheduling
from records import access
from records import timeline as records_timeline

logger = logging.getLogger("appointments")


def _is_radiology_booking(appointment):
    return role_of(appointment.provider) == roles.RADIOLOGY


@scheduling.on_appointment_created
def create_examination_for_booking(appointment):
    """Every booking with a radiology centre becomes a scheduled examination.

    Created at booking time rather than at check-in so the centre's day is
    visible before anyone arrives — and so "scheduled" is a real examination
    state rather than a gap before the record exists.

    The order is attached separately by ``services.book_for_order``, which is
    the only caller that knows which order this booking answers.
    """
    if not _is_radiology_booking(appointment):
        return

    from .models import Examination
    examination = Examination.objects.create(appointment=appointment)
    logger.info("Examination %s created for appointment %s",
                examination.id, appointment.id)
    return examination


@scheduling.on_appointment_cancelled
def release_examination_for_booking(appointment):
    """Cancelling the booking cancels the study and frees the order.

    Without this the order would stay ``scheduled`` forever, pointing at an
    appointment that is not going to happen, and the patient could never
    re-book it.
    """
    if not _is_radiology_booking(appointment):
        return

    from .models import Examination, ImagingOrder
    examination = (Examination.objects
                   .select_related("order")
                   .filter(appointment=appointment).first())
    if examination is None:
        return

    if examination.status != Examination.CANCELLED:
        examination.status = Examination.CANCELLED
        examination.save(update_fields=["status", "updated_at"])

    order = examination.order
    if order is not None and order.status == ImagingOrder.SCHEDULED:
        # Back to 'ordered': the clinical request still stands, it just has no
        # booking any more.
        order.status = ImagingOrder.ORDERED
        order.save(update_fields=["status", "updated_at"])
        logger.info("Imaging order %s released back to 'ordered'", order.id)


@records_timeline.source
def radiology_timeline_entries(viewer, patient, limit):
    """Imaging orders and released reports, on the unified medical timeline.

    Registered rather than imported: ``records`` knows nothing about radiology,
    which is what lets a domain publish its history without the aggregation
    layer being edited for every module.

    The report entries come from ``patient_visible_reports``, so an unreleased
    report is never fetched — the medical record cannot expose a draft even if
    something here forgot to check, because there is nothing to expose.
    """
    from . import services

    if not access.may_view(viewer, patient):
        return []

    entries = []
    for report in services.patient_visible_reports(patient)[:limit]:
        examination = report.examination
        appointment = examination.appointment
        provider = getattr(appointment.provider, "radiology_profile", None)
        entries.append(records_timeline.Entry(
            type=records_timeline.RADIOLOGY_REPORT,
            # The release time, not the draft time: what matters is the
            # source's own relevant timestamp, and a report enters the
            # patient's history when it is released to them.
            at=report.released_at or report.updated_at,
            title=(examination.order.study_name if examination.order
                   else (appointment.service.name if appointment.service
                         else "Radiology report")),
            source="radiology.RadiologyReport",
            reference=report.id,
            status=report.status,
            status_label=report.get_status_display(),
            provider=(provider.name if provider
                      else appointment.provider.get_full_name()
                      or appointment.provider.username),
            detail=(report.impression or "")[:160],
        ))

    for order in services.patient_orders(patient)[:limit]:
        entries.append(records_timeline.Entry(
            type=records_timeline.RADIOLOGY_ORDER,
            at=order.created_at,
            title=order.study_name,
            source="radiology.ImagingOrder",
            reference=order.id,
            status=order.status,
            status_label=order.get_status_display(),
            provider=("Dr. " + (order.doctor.get_full_name()
                                or order.doctor.username)
                      if order.doctor else "Requested by the patient"),
            detail=order.clinical_indication or order.modality_label,
            extra={"modality": order.modality,
                   "self_requested": order.doctor_id is None},
        ))
    return entries


@dashboard.contribute
def radiology_dashboard_block(user, role):
    """The radiology figures each role's dashboard shows.

    Registered rather than imported: ``appointments.services.dashboard`` knows
    nothing about this module, which is what lets a domain be added without
    editing the engine.
    """
    from . import services

    if role == roles.RADIOLOGY:
        # Real counts replace the placeholder tiles: the centre is no longer a
        # portal waiting for its domain, so those figures stop being None.
        return {"stats": services.center_counts(user)}
    if role == roles.PATIENT:
        return {"radiology": services.patient_radiology_summary(user)}
    if role == roles.DOCTOR:
        return {"radiology": services.doctor_radiology_summary(user)}
    return {}
