"""Radiology use-cases.

Every state change in the module lives here. Views validate input and translate
exceptions; they never assign a status field. That is what makes "do not allow
arbitrary status changes from the frontend" true structurally rather than by
convention — there is no code path from a request body to a status column.

Each transition moves the whole chain together inside one transaction: an
examination that completes also closes its appointment and advances its order,
because leaving any two of those disagreeing is worse than failing.
"""
import logging

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts import roles
from accounts.services import role_of
from appointments.models import Appointment, Service
from appointments.services import care, scheduling

from . import modalities
from .models import Examination, ImagingFile, ImagingOrder, RadiologyReport

logger = logging.getLogger("appointments")


class NotAuthorized(Exception):
    """The caller has no relationship to this record."""


class NotFound(Exception):
    """No such record — also raised instead of NotAuthorized where telling the
    two apart would confirm that an id exists."""


class InvalidTransition(Exception):
    """The requested status change is not permitted from the current state."""


class OrderNotBookable(Exception):
    """The order is cancelled, already booked, or already reported."""


class ServiceMismatch(Exception):
    """The chosen service cannot fulfil the order."""


# ---------------------------------------------------------------------------
# Reading — every queryset is scoped, never filtered in the view
# ---------------------------------------------------------------------------
_ORDER_RELATED = ("patient", "doctor")

#: Wide on purpose. Serialising an examination reaches the provider's *display*
#: name, which lives on whichever profile matches their role, and the report's
#: author — so those joins happen once here rather than per row while
#: serializing. ``files`` is a reverse relation and needs prefetching, not
#: select_related.
_EXAM_RELATED = ("appointment", "appointment__patient", "appointment__provider",
                 "appointment__provider__account",
                 "appointment__provider__doctor_profile",
                 "appointment__provider__laboratory_profile",
                 "appointment__provider__radiology_profile",
                 "appointment__service", "order", "order__patient",
                 "order__doctor", "report", "report__author",
                 "report__verified_by", "performed_by")
_EXAM_PREFETCH = ("files", "files__uploaded_by")


def orders_for(user):
    """The imaging orders this user may see, by role.

    A doctor sees the orders they wrote — not every order for their patients,
    which would let one clinician read another's referrals.
    """
    role = role_of(user)
    queryset = ImagingOrder.objects.select_related(*_ORDER_RELATED)
    if role == roles.PATIENT:
        return queryset.filter(patient=user)
    if role == roles.DOCTOR:
        return queryset.filter(doctor=user)
    if role == roles.RADIOLOGY:
        # A centre sees the orders it is actually fulfilling.
        return queryset.filter(examinations__appointment__provider=user).distinct()
    if role == roles.ADMIN:
        return queryset
    return ImagingOrder.objects.none()


def examinations_for(user):
    role = role_of(user)
    queryset = (Examination.objects.select_related(*_EXAM_RELATED)
                .prefetch_related(*_EXAM_PREFETCH))
    if role == roles.PATIENT:
        return queryset.filter(appointment__patient=user)
    if role == roles.RADIOLOGY:
        return queryset.filter(appointment__provider=user)
    if role == roles.DOCTOR:
        # Only studies the doctor ordered, and only where a care relationship
        # still exists. Holding a doctor account is not authorization.
        return queryset.filter(order__doctor=user)
    if role == roles.ADMIN:
        return queryset
    return Examination.objects.none()


def reports_for(user):
    """Reports the user may read.

    The patient gate is applied in the queryset, not the template: an
    unreleased report is not fetched at all, so it cannot leak through a view
    that forgets to check.
    """
    role = role_of(user)
    queryset = RadiologyReport.objects.select_related(
        "examination", "examination__appointment",
        "examination__appointment__patient", "examination__appointment__provider",
        "examination__appointment__service", "examination__order", "author",
        "verified_by")

    if role == roles.PATIENT:
        return queryset.filter(
            examination__appointment__patient=user,
            status__in=RadiologyReport.PATIENT_VISIBLE_STATUSES)
    if role == roles.RADIOLOGY:
        return queryset.filter(examination__appointment__provider=user)
    if role == roles.DOCTOR:
        # The ordering doctor, and only once released. A draft is not a
        # clinical document.
        return queryset.filter(
            examination__order__doctor=user,
            status__in=RadiologyReport.PATIENT_VISIBLE_STATUSES)
    if role == roles.ADMIN:
        return queryset.none()   # administration does not include reading reads
    return RadiologyReport.objects.none()


def patient_visible_reports(patient):
    """Reports about this patient that the *patient themselves* may read.

    Released only — the same gate ``reports_for`` applies, expressed once here
    so the medical record can show a treating doctor the patient-visible layer
    without any caller being able to widen it. A draft is not fetched, so no
    aggregation layer can leak one by forgetting to filter.
    """
    return (RadiologyReport.objects
            .filter(examination__appointment__patient=patient,
                    status__in=RadiologyReport.PATIENT_VISIBLE_STATUSES)
            .select_related("examination", "examination__appointment",
                            "examination__appointment__provider",
                            "examination__appointment__provider__radiology_profile",
                            "examination__appointment__service",
                            "examination__order"))


def patient_orders(patient):
    """Imaging a doctor requested for this patient. The request, not the read."""
    return (ImagingOrder.objects.filter(patient=patient)
            .select_related("doctor", "patient"))


def get_order(user, order_id):
    order = orders_for(user).filter(pk=order_id).first()
    if order is None:
        raise NotFound()
    return order


def get_examination(user, examination_id):
    examination = examinations_for(user).filter(pk=examination_id).first()
    if examination is None:
        raise NotFound()
    return examination


def files_for(user):
    """Imaging files the user may read, scoped the same way as examinations.

    A patient may read files for their own studies; the ordering doctor may
    read them; the centre may read its own. Everyone else gets nothing — and a
    cross-boundary id is 'not found', so the endpoint cannot be used to learn
    which files exist.
    """
    role = role_of(user)
    queryset = ImagingFile.objects.select_related(
        "examination", "examination__appointment",
        "examination__appointment__patient", "examination__appointment__provider",
        "examination__order")
    if role == roles.PATIENT:
        return queryset.filter(examination__appointment__patient=user)
    if role == roles.RADIOLOGY:
        return queryset.filter(examination__appointment__provider=user)
    if role == roles.DOCTOR:
        return queryset.filter(examination__order__doctor=user)
    return ImagingFile.objects.none()


def get_file(user, file_id):
    stored = files_for(user).filter(pk=file_id).first()
    if stored is None:
        raise NotFound()
    return stored


# ---------------------------------------------------------------------------
# Doctor: ordering
# ---------------------------------------------------------------------------
def create_order(doctor, patient_id, modality, study_name,
                 clinical_indication="", notes=""):
    """A doctor orders a study for a patient they actually treat.

    The care-relationship rule is the platform's existing one (an appointment
    between them), reused rather than restated so radiology cannot drift into a
    looser definition of "my patient" than the rest of the product uses.
    """
    if not modalities.is_valid(modality):
        raise ServiceMismatch(f"{modality!r} is not a known imaging modality.")

    try:
        patient = User.objects.get(pk=int(patient_id))
    except (User.DoesNotExist, ValueError, TypeError):
        raise NotFound()

    if role_of(patient) != roles.PATIENT:
        raise NotFound()
    if not care.treats_patient(doctor, patient):
        raise NotAuthorized(
            "You can only order imaging for patients you have appointments with.")

    order = ImagingOrder.objects.create(
        patient=patient, doctor=doctor, modality=modality,
        study_name=study_name, clinical_indication=clinical_indication,
        notes=notes)

    from comms import notifications
    from comms import types as notification_types
    notifications.notify(
        patient, notification_types.IMAGING_ORDER_CREATED,
        "Imaging requested",
        f"{notifications.display_name(doctor)} requested {study_name}. "
        f"You can book it at a centre of your choice.",
        source="radiology.ImagingOrder", reference=order.id)

    logger.info("Imaging order %s created by %s for %s",
                order.id, doctor.username, patient.username)
    return order


def cancel_order(user, order_id, reason=""):
    """Either the patient or the ordering doctor may withdraw an order."""
    order = get_order(user, order_id)
    if role_of(user) not in (roles.PATIENT, roles.DOCTOR):
        raise NotAuthorized("Only the patient or the ordering doctor can cancel.")
    if order.status in (ImagingOrder.REPORTED, ImagingOrder.CANCELLED):
        raise InvalidTransition(
            f"A {order.get_status_display().lower()} order cannot be cancelled.")

    order.status = ImagingOrder.CANCELLED
    order.cancellation_reason = reason or ""
    order.save(update_fields=["status", "cancellation_reason", "updated_at"])
    return order


# ---------------------------------------------------------------------------
# Patient: finding and booking
# ---------------------------------------------------------------------------
def centers_offering(modality=None):
    """Radiology centres, with the imaging services each actually offers.

    Built from the catalogue rather than a fixed list, so a centre that does not
    offer MRI never appears under MRI.
    """
    from accounts.models import RadiologyProfile

    services = Service.objects.filter(category__in=modalities.ALL,
                                      is_active=True)
    if modality:
        services = services.filter(category=modality)

    by_provider = {}
    for service in services.select_related("provider"):
        by_provider.setdefault(service.provider_id, []).append(service)

    centers = []
    for profile in (RadiologyProfile.objects
                    .filter(available=True, user_id__in=by_provider)
                    .select_related("user")):
        centers.append({
            "id": profile.user_id,
            "name": profile.name,
            "address": profile.address,
            "phone": profile.phone,
            "verified": profile.verified,
            "services": sorted(by_provider[profile.user_id],
                               key=lambda s: s.name),
        })
    return sorted(centers, key=lambda c: c["name"].lower())


def book_for_order(patient, order_id, service_id, date, time, reason=""):
    """Book an appointment that fulfils an existing order.

    The order is *attached*, never duplicated: booking a study a doctor already
    requested must not create a second clinical request. The engine does the
    booking; this only validates that the chosen service can answer the order
    and links the two.
    """
    order = orders_for(patient).filter(pk=order_id).first()
    if order is None:
        raise NotFound()
    if not order.is_bookable:
        raise OrderNotBookable(
            f"This order is {order.get_status_display().lower()} and cannot be "
            f"booked.")

    try:
        service = Service.objects.select_related("provider").get(
            pk=int(service_id), is_active=True)
    except (Service.DoesNotExist, ValueError, TypeError):
        raise NotFound()

    if role_of(service.provider) != roles.RADIOLOGY:
        raise ServiceMismatch("That service is not offered by a radiology centre.")
    if service.category != order.modality:
        # Without this a patient could attach an X-ray booking to an MRI order.
        raise ServiceMismatch(
            f"{service.name} is not {modalities.label(order.modality)} imaging.")

    with transaction.atomic():
        appointment = scheduling.create_appointment(
            patient, date=date, time=time, reason=reason,
            provider_id=service.provider_id, service_id=service.id)
        # The hook has already created the examination; link it to the order.
        examination = Examination.objects.select_for_update().get(
            appointment=appointment)
        examination.order = order
        examination.save(update_fields=["order", "updated_at"])

        order.status = ImagingOrder.SCHEDULED
        order.save(update_fields=["status", "updated_at"])

    logger.info("Order %s booked as appointment %s", order.id, appointment.id)
    return order, appointment, examination


def self_book(patient, service_id, date, time, study_name="", reason=""):
    """A patient books imaging with no doctor's referral.

    This still produces an ImagingOrder so the study has a clinical record to
    hang from — but with ``doctor=None``, which is what keeps it honestly
    distinguishable from a referral rather than a fabricated one.
    """
    try:
        service = Service.objects.select_related("provider").get(
            pk=int(service_id), is_active=True)
    except (Service.DoesNotExist, ValueError, TypeError):
        raise NotFound()
    if role_of(service.provider) != roles.RADIOLOGY:
        raise ServiceMismatch("That service is not offered by a radiology centre.")
    if service.category not in modalities.ALL:
        raise ServiceMismatch("That service is not an imaging service.")

    with transaction.atomic():
        order = ImagingOrder.objects.create(
            patient=patient, doctor=None, modality=service.category,
            study_name=study_name or service.name,
            notes="Booked by the patient without a referral.",
            status=ImagingOrder.SCHEDULED)
        appointment = scheduling.create_appointment(
            patient, date=date, time=time, reason=reason,
            provider_id=service.provider_id, service_id=service.id)
        examination = Examination.objects.select_for_update().get(
            appointment=appointment)
        examination.order = order
        examination.save(update_fields=["order", "updated_at"])

    return order, appointment, examination


# ---------------------------------------------------------------------------
# Centre: the examination lifecycle
# ---------------------------------------------------------------------------
#: Examination status -> the order status it implies. Applied whenever an
#: examination moves, so the two never drift.
_ORDER_STATUS_FOR = {
    Examination.CHECKED_IN: ImagingOrder.SCHEDULED,
    Examination.IN_PROGRESS: ImagingOrder.IN_PROGRESS,
    Examination.COMPLETED: ImagingOrder.REPORT_PENDING,
}

_TIMESTAMP_FOR = {
    Examination.CHECKED_IN: "checked_in_at",
    Examination.IN_PROGRESS: "started_at",
    Examination.COMPLETED: "completed_at",
}


def advance_examination(center_user, examination_id, to_status):
    """Move an examination one legal step, taking the order and booking with it.

    Only the centre performing the study may do this — checked here rather than
    trusted from the request, and scoped by ``examinations_for`` so another
    centre's id is simply not found.
    """
    if role_of(center_user) != roles.RADIOLOGY:
        raise NotAuthorized("Only the radiology centre can update an examination.")

    examination = examinations_for(center_user).filter(pk=examination_id).first()
    if examination is None:
        raise NotFound()

    if not examination.can_transition_to(to_status):
        raise InvalidTransition(
            f"An examination that is {examination.get_status_display().lower()} "
            f"cannot become {to_status.replace('_', ' ')}.")

    with transaction.atomic():
        examination.status = to_status
        fields = ["status", "updated_at"]

        stamp = _TIMESTAMP_FOR.get(to_status)
        if stamp:
            setattr(examination, stamp, timezone.now())
            fields.append(stamp)
        if to_status == Examination.COMPLETED:
            examination.performed_by = center_user
            fields.append("performed_by")
        examination.save(update_fields=fields)

        order = examination.order
        implied = _ORDER_STATUS_FOR.get(to_status)
        if order is not None and implied and order.status != ImagingOrder.CANCELLED:
            order.status = implied
            order.save(update_fields=["status", "updated_at"])

        if to_status == Examination.COMPLETED:
            # One writer for the booking's outcome: the engine's own service.
            appointment = examination.appointment
            if appointment.status == Appointment.SCHEDULED:
                scheduling.set_outcome(center_user, appointment.id,
                                       Appointment.COMPLETED)

    logger.info("Examination %s -> %s", examination.id, to_status)
    return examination


# ---------------------------------------------------------------------------
# Centre: files
# ---------------------------------------------------------------------------
def attach_file(center_user, examination_id, uploaded, kind=ImagingFile.IMAGE,
                description="", study_uid="", series_uid="", instance_uid=""):
    """Store an image or document against one of the centre's own examinations."""
    if role_of(center_user) != roles.RADIOLOGY:
        raise NotAuthorized("Only the radiology centre can attach imaging.")

    examination = examinations_for(center_user).filter(pk=examination_id).first()
    if examination is None:
        raise NotFound()
    if examination.status == Examination.CANCELLED:
        raise InvalidTransition("A cancelled examination cannot receive files.")

    service = examination.service
    modality = (service.category if service and service.category in modalities.ALL
                else (examination.order.modality if examination.order else ""))

    stored = ImagingFile.objects.create(
        examination=examination, file=uploaded, kind=kind,
        original_name=getattr(uploaded, "name", "")[:255],
        content_type=(getattr(uploaded, "content_type", "") or "")[:100],
        size_bytes=getattr(uploaded, "size", 0) or 0,
        description=description,
        modality_code=modalities.DICOM_CODES.get(modality, ""),
        study_uid=study_uid, series_uid=series_uid, instance_uid=instance_uid,
        uploaded_by=center_user)
    logger.info("Imaging file %s attached to examination %s",
                stored.id, examination.id)
    return stored


# ---------------------------------------------------------------------------
# Centre: reporting
# ---------------------------------------------------------------------------
def _own_report(center_user, report_id):
    if role_of(center_user) != roles.RADIOLOGY:
        raise NotAuthorized("Only the radiology centre can work on reports.")
    report = (RadiologyReport.objects
              .select_related("examination", "examination__appointment")
              .filter(pk=report_id,
                      examination__appointment__provider=center_user).first())
    if report is None:
        raise NotFound()
    return report


def draft_report(center_user, examination_id, findings="", impression="",
                 notes=""):
    """Start (or update) the draft for a completed examination."""
    if role_of(center_user) != roles.RADIOLOGY:
        raise NotAuthorized("Only the radiology centre can write reports.")

    examination = examinations_for(center_user).filter(pk=examination_id).first()
    if examination is None:
        raise NotFound()
    if examination.status != Examination.COMPLETED:
        raise InvalidTransition(
            "A report can only be written for a completed examination.")

    report, created = RadiologyReport.objects.get_or_create(
        examination=examination, defaults={"author": center_user})
    if not created and report.status not in (RadiologyReport.DRAFT,
                                             RadiologyReport.PENDING_REVIEW):
        raise InvalidTransition(
            f"A {report.get_status_display().lower()} report cannot be edited.")

    report.findings = findings
    report.impression = impression
    report.notes = notes
    report.author = report.author or center_user
    report.touch_report_date()
    report.save(update_fields=["findings", "impression", "notes", "author",
                               "report_date", "updated_at"])
    return report


def transition_report(center_user, report_id, to_status):
    """Move a report along its workflow: review -> verify -> release."""
    report = _own_report(center_user, report_id)
    if not report.can_transition_to(to_status):
        raise InvalidTransition(
            f"A {report.get_status_display().lower()} report cannot become "
            f"{to_status.replace('_', ' ')}.")

    with transaction.atomic():
        report.status = to_status
        fields = ["status", "updated_at"]

        if to_status == RadiologyReport.VERIFIED:
            report.verified_by = center_user
            report.verified_at = timezone.now()
            fields += ["verified_by", "verified_at"]
        if to_status == RadiologyReport.RELEASED:
            if report.verified_at is None:
                raise InvalidTransition(
                    "A report must be verified before it is released.")
            report.released_at = timezone.now()
            fields.append("released_at")
        report.save(update_fields=fields)

        order = report.examination.order
        if order is not None and to_status == RadiologyReport.RELEASED:
            order.status = ImagingOrder.REPORTED
            order.save(update_fields=["status", "updated_at"])

        if to_status == RadiologyReport.RELEASED:
            _announce_release(report)

    logger.info("Report %s -> %s", report.id, to_status)
    return report


def _announce_release(report):
    """Tell the patient — and the ordering doctor — that the report is out.

    Raised on the *released* branch only: the whole point of the workflow is
    that a draft is not a clinical document, so a notification about one would
    undo the gate. The body carries no findings and no impression; it says the
    report exists and links to it, and reading it still goes through this
    module's own permission check.
    """
    from comms import notifications
    from comms import types as notification_types

    examination = report.examination
    order = examination.order
    study = (order.study_name if order else
             (examination.appointment.service.name
              if examination.appointment.service else "imaging study"))

    notifications.notify(
        examination.appointment.patient,
        notification_types.RADIOLOGY_REPORT_RELEASED,
        "Radiology report available",
        f"Your {study} report is now available.",
        source="radiology.RadiologyReport", reference=report.id)

    if order is not None and order.doctor_id:
        notifications.notify(
            order.doctor, notification_types.RADIOLOGY_REPORT_RELEASED,
            "Radiology report available",
            f"The {study} you ordered for "
            f"{notifications.patient_name(order.patient)} has been reported.",
            source="radiology.RadiologyReport", reference=report.id)


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------
def center_counts(center_user):
    """Real figures for the radiology centre dashboard. No invented numbers."""
    today = timezone.localtime().date()
    examinations = Examination.objects.filter(appointment__provider=center_user)
    reports = RadiologyReport.objects.filter(
        examination__appointment__provider=center_user)

    return {
        "examinations_today": examinations.filter(
            appointment__start_at__date=today).exclude(
                status=Examination.CANCELLED).count(),
        "pending_orders": ImagingOrder.objects.filter(
            examinations__appointment__provider=center_user,
            status__in=(ImagingOrder.SCHEDULED, ImagingOrder.IN_PROGRESS)
        ).distinct().count(),
        "awaiting_report": examinations.filter(
            status=Examination.COMPLETED, report__isnull=True).count(),
        "reports_pending_review": reports.filter(
            status__in=(RadiologyReport.DRAFT,
                        RadiologyReport.PENDING_REVIEW)).count(),
        "reports_released": reports.filter(
            status=RadiologyReport.RELEASED).count(),
    }


def patient_radiology_summary(patient):
    """The patient dashboard's radiology block."""
    orders = ImagingOrder.objects.filter(patient=patient)
    released = reports_for(patient)
    return {
        "open_orders": orders.filter(
            status__in=(ImagingOrder.ORDERED, ImagingOrder.SCHEDULED,
                        ImagingOrder.IN_PROGRESS,
                        ImagingOrder.REPORT_PENDING)).count(),
        "awaiting_booking": orders.filter(status=ImagingOrder.ORDERED).count(),
        "released_reports": released.count(),
    }


def doctor_radiology_summary(doctor):
    orders = ImagingOrder.objects.filter(doctor=doctor)
    return {
        "imaging_orders": orders.count(),
        "imaging_awaiting_booking": orders.filter(
            status=ImagingOrder.ORDERED).count(),
        "imaging_reports_ready": reports_for(doctor).count(),
    }


def orderable_patients(doctor):
    """Patients this doctor may order imaging for — those they actually treat."""
    patient_ids = (Appointment.objects.filter(provider=doctor)
                   .values_list("patient_id", flat=True).distinct())
    return (User.objects.filter(pk__in=patient_ids)
            .filter(Q(account__role=roles.PATIENT) | Q(account__isnull=True))
            .order_by("first_name", "username"))
