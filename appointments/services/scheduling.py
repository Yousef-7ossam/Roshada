"""Scheduling use-cases: providers, booking, and the appointment lifecycle.

ORM access (including query optimisation via select_related) lives here so the
views stay free of persistence details.

**One engine, three provider kinds.** A booking is always the same operation —
a patient occupies a period of one provider's time — whether that provider is a
doctor, a laboratory or a radiology centre. The only thing that varies is which
service is attached, so that is the only thing this module branches on.
"""
import datetime

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts import roles
from accounts.services import role_of

from ..models import Appointment, Doctor, Service
from . import availability
from .availability import DEFAULT_SLOT_MINUTES, combine

#: Re-exported so a view can catch every way a booking can fail from one module
#: instead of importing the availability internals as well.
OutsideAvailability = availability.OutsideAvailability
ProviderUnavailable = availability.ProviderUnavailable
SlotTaken = availability.SlotTaken

#: Re-exported from accounts.roles so the permission classes and this module
#: cannot disagree about who is bookable.
BOOKABLE_ROLES = roles.BOOKABLE_ROLES


# ---------------------------------------------------------------------------
# Domain callbacks
#
# The engine knows nothing about radiology, laboratories or anything else that
# might want to react to a booking. Domain modules register here at startup
# (see ``radiology.apps.RadiologyConfig.ready``), which keeps the dependency
# pointing one way — domain -> engine — and leaves the wiring greppable.
#
# Plain lists rather than Django signals: a test can read them, and the order
# callbacks run in is the order they were registered.
# ---------------------------------------------------------------------------
_ON_CREATED = []
_ON_CANCELLED = []
_ON_RESCHEDULED = []
_ON_OUTCOME = []


def on_appointment_created(callback):
    """Register a callable run with each newly booked appointment."""
    if callback not in _ON_CREATED:
        _ON_CREATED.append(callback)
    return callback


def on_appointment_cancelled(callback):
    if callback not in _ON_CANCELLED:
        _ON_CANCELLED.append(callback)
    return callback


def on_appointment_rescheduled(callback):
    """``fn(appointment, previous_start)``.

    The old instant is passed because a subscriber that only receives the
    appointment cannot say what changed — "your appointment was moved" is a
    worse message than one naming both times.
    """
    if callback not in _ON_RESCHEDULED:
        _ON_RESCHEDULED.append(callback)
    return callback


def on_appointment_outcome(callback):
    """``fn(appointment)`` — the provider closed the visit out."""
    if callback not in _ON_OUTCOME:
        _ON_OUTCOME.append(callback)
    return callback


def _notify(callbacks, appointment):
    """Run the registered callbacks.

    Deliberately *not* wrapped in a try/except: these run inside the booking
    transaction, and a domain that cannot record its side of a booking must
    roll the booking back rather than leave the two halves disagreeing.
    """
    for callback in callbacks:
        callback(appointment)


class DoctorNotFound(Exception):
    """Raised when the requested provider does not exist or is not bookable."""


class DoctorNotAvailable(Exception):
    """Raised when the requested provider is not accepting appointments."""


class SlotUnavailable(Exception):
    """Raised when the provider already has an appointment in that slot."""


class AppointmentNotFound(Exception):
    """Raised when the appointment does not exist or is not the caller's."""


class InvalidTransition(Exception):
    """Raised when the requested status change is not allowed."""


class ServiceNotFound(Exception):
    """Raised when the service does not exist, is inactive, or is another
    provider's."""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def list_available_doctors():
    """Available doctors, with the related user pre-fetched (avoids N+1)."""
    return Doctor.objects.filter(available=True).select_related('user')


def _facility_queryset(role):
    from accounts.models import (
        LaboratoryProfile, PharmacyProfile, RadiologyProfile,
    )
    model = {roles.LABORATORY: LaboratoryProfile,
             roles.RADIOLOGY: RadiologyProfile,
             roles.PHARMACY: PharmacyProfile}[role]
    return model.objects.select_related('user')


def list_providers(role=None):
    """Bookable providers, optionally of one kind.

    Returns plain dicts rather than model instances because the three kinds
    live in three tables; a common shape here is what lets the patient's
    "choose a provider" step be one list instead of three.
    """
    wanted = (role,) if role else BOOKABLE_ROLES
    providers = []

    if roles.DOCTOR in wanted:
        for doctor in list_available_doctors():
            if doctor.user_id is None:
                # A seeded doctor with no login cannot own appointments.
                continue
            providers.append({
                "id": doctor.user_id,
                "role": roles.DOCTOR,
                "role_label": roles.label(roles.DOCTOR),
                "name": doctor.name,
                "detail": doctor.specialization,
                "location": doctor.clinic,
                "available": doctor.available,
                "verified": True,      # doctors are vetted at registration
                "doctor_id": doctor.id,
            })

    for facility_role in (roles.LABORATORY, roles.RADIOLOGY):
        if facility_role not in wanted:
            continue
        for facility in _facility_queryset(facility_role).filter(available=True):
            providers.append({
                "id": facility.user_id,
                "role": facility_role,
                "role_label": roles.label(facility_role),
                "name": facility.name,
                "detail": facility.services,
                "location": facility.address,
                "available": facility.available,
                "verified": facility.verified,
            })

    return sorted(providers, key=lambda p: (p["role"], p["name"].lower()))


def resolve_provider(provider_id=None, doctor_id=None):
    """The provider's ``User``, from either identifier.

    ``doctor_id`` is the pre-unification identifier — a ``Doctor`` row's pk, not
    a user's. It is still accepted so existing clients keep working, and mapped
    here, at the one place that knows both id spaces.
    """
    if provider_id is not None:
        try:
            user = User.objects.get(pk=int(provider_id))
        except (User.DoesNotExist, ValueError, TypeError):
            raise DoctorNotFound()
        if role_of(user) not in BOOKABLE_ROLES:
            raise DoctorNotFound()
        return user

    try:
        doctor = Doctor.objects.select_related('user').get(pk=int(doctor_id))
    except (Doctor.DoesNotExist, ValueError, TypeError):
        raise DoctorNotFound()
    if doctor.user_id is None:
        raise DoctorNotFound()
    if not doctor.available:
        raise DoctorNotAvailable()
    return doctor.user


def provider_is_available(user):
    """Whether the provider is accepting bookings at all (their own switch)."""
    role = role_of(user)
    if role == roles.DOCTOR:
        profile = getattr(user, 'doctor_profile', None)
        return bool(profile and profile.available)
    profile = getattr(user, f"{role}_profile", None)
    return bool(profile and profile.available)


def resolve_service(provider, service_id):
    """A service belonging to ``provider``. Another provider's id is a 404.

    Checking ownership here rather than trusting the id is what stops a patient
    from attaching Lab B's cheap test to an appointment with Lab A.
    """
    if service_id in (None, ""):
        return None
    try:
        return Service.objects.get(pk=int(service_id), provider=provider,
                                   is_active=True)
    except (Service.DoesNotExist, ValueError, TypeError):
        raise ServiceNotFound()


def list_services(provider, include_inactive=False):
    queryset = Service.objects.filter(provider=provider)
    if not include_inactive:
        queryset = queryset.filter(is_active=True)
    return queryset


# ---------------------------------------------------------------------------
# Reading appointments
# ---------------------------------------------------------------------------
#: Wide on purpose. Rendering an appointment needs the provider's *display*
#: name, which lives on whichever profile matches their role, so all three are
#: joined in the one query rather than fetched per row while serializing.
_RELATED = ('provider', 'patient', 'service', 'provider__account',
            'provider__doctor_profile', 'provider__laboratory_profile',
            'provider__radiology_profile')


def list_patient_appointments(patient):
    """A patient's appointments, with provider/service pre-fetched."""
    return (Appointment.objects.filter(patient=patient)
            .select_related(*_RELATED))


def list_provider_appointments(provider_user):
    """Appointments booked with the given provider, whatever their kind."""
    return (Appointment.objects.filter(provider=provider_user)
            .select_related(*_RELATED))


#: Kept under the old name; doctors are one kind of provider.
list_doctor_appointments = list_provider_appointments


# ---------------------------------------------------------------------------
# Booking
# ---------------------------------------------------------------------------
def _duration_for(provider, service, start_at):
    """How long the appointment occupies.

    Service duration wins. Failing that, the rule covering the slot decides;
    failing that (a provider with no published hours), the legacy default.
    """
    if service is not None and service.duration_minutes:
        return service.duration_minutes
    date = availability._local_date(start_at)
    for rule in availability.rules_on(provider, date, service):
        window_start = combine(date, rule.start_time)
        window_end = combine(date, rule.end_time)
        if window_start <= start_at < window_end:
            return availability.slot_length(rule, service)
    return DEFAULT_SLOT_MINUTES


def create_appointment(patient, date, time, reason="", provider_id=None,
                       doctor_id=None, service_id=None):
    """Book an appointment, validating the provider, service and slot.

    Availability is enforced **when the provider publishes any** — and always
    when a service is booked, since a service without opening hours produces no
    slots at all. A doctor who has never opened hours stays bookable free-form,
    which is how this change avoids invalidating every appointment made before
    the availability model existed.
    """
    provider = resolve_provider(provider_id=provider_id, doctor_id=doctor_id)
    if not provider_is_available(provider):
        raise DoctorNotAvailable()

    service = resolve_service(provider, service_id)
    start_at = combine(date, time)
    end_at = start_at + datetime.timedelta(
        minutes=_duration_for(provider, service, start_at))

    enforce = service is not None or availability.has_rules(provider)

    try:
        with transaction.atomic():
            if enforce:
                # Re-checked inside the transaction: whatever the client was
                # shown may already be stale.
                availability.validate_slot(provider, start_at, end_at, service)
            appointment = Appointment.objects.create(
                provider=provider, patient=patient, service=service,
                start_at=start_at, end_at=end_at, reason=reason)
            # Inside the transaction: if a domain cannot record its side of the
            # booking, the booking itself must not survive.
            _notify(_ON_CREATED, appointment)
            return appointment
    except IntegrityError:
        # The exclusion constraint refused an overlap. This is the authoritative
        # answer — two simultaneous bookings both pass validate_slot and only
        # one survives here.
        raise SlotUnavailable()


# ---------------------------------------------------------------------------
# Lifecycle: cancel / reschedule / close out
# ---------------------------------------------------------------------------
def _visible_to(user, appointment_id):
    """Fetch an appointment the user is a party to (patient or provider)."""
    try:
        appointment = (Appointment.objects.select_related(*_RELATED)
                       .get(pk=int(appointment_id)))
    except (Appointment.DoesNotExist, ValueError, TypeError):
        raise AppointmentNotFound()

    is_patient = appointment.patient_id == user.id
    is_provider = appointment.provider_id == user.id
    if not (is_patient or is_provider):
        # Same error as "missing" so the endpoint cannot be used to probe which
        # appointment ids exist.
        raise AppointmentNotFound()
    return appointment


def cancel_appointment(user, appointment_id, reason=""):
    """Cancel a scheduled appointment. Either party may cancel.

    The row stays; only the status changes. That both preserves history and
    releases the slot, because the exclusion constraint and every availability
    query are scoped to ``scheduled``.
    """
    appointment = _visible_to(user, appointment_id)

    if appointment.status == Appointment.CANCELLED:
        raise InvalidTransition("This appointment is already cancelled.")
    if appointment.status != Appointment.SCHEDULED:
        raise InvalidTransition(
            f"A {appointment.get_status_display().lower()} appointment cannot be cancelled.")

    with transaction.atomic():
        appointment.status = Appointment.CANCELLED
        appointment.cancellation_reason = reason or ""
        appointment.save(
            update_fields=['status', 'cancellation_reason', 'updated_at'])
        # Lets a domain release whatever it attached — a radiology examination
        # is cancelled and its order becomes bookable again.
        _notify(_ON_CANCELLED, appointment)
    return appointment


def reschedule_appointment(user, appointment_id, date, time):
    """Move a scheduled appointment to another slot with the same provider."""
    appointment = _visible_to(user, appointment_id)

    if appointment.status != Appointment.SCHEDULED:
        raise InvalidTransition(
            f"A {appointment.get_status_display().lower()} appointment cannot be rescheduled.")

    provider = appointment.provider
    service = appointment.service
    start_at = combine(date, time)
    if start_at == appointment.start_at:
        return appointment  # nothing to do

    duration = (appointment.duration_minutes
                if service is None else service.duration_minutes)
    end_at = start_at + datetime.timedelta(minutes=duration)

    enforce = service is not None or availability.has_rules(provider)
    try:
        with transaction.atomic():
            if enforce:
                # Excluding itself: the appointment's current slot must not
                # count as taken when it is the one being moved.
                availability.validate_slot(provider, start_at, end_at, service,
                                           exclude_appointment=appointment)
            previous_start = appointment.start_at
            appointment.start_at = start_at
            appointment.end_at = end_at
            appointment.save(update_fields=['start_at', 'end_at', 'updated_at'])
            _notify_rescheduled(appointment, previous_start)
    except IntegrityError:
        raise SlotUnavailable()
    return appointment


def _notify_rescheduled(appointment, previous_start):
    for callback in _ON_RESCHEDULED:
        callback(appointment, previous_start)


def set_outcome(provider_user, appointment_id, status):
    """Provider closes out a visit as completed or no-show."""
    if status not in (Appointment.COMPLETED, Appointment.NO_SHOW):
        raise InvalidTransition("Outcome must be 'completed' or 'no_show'.")

    appointment = _visible_to(provider_user, appointment_id)
    if appointment.provider_id != provider_user.id:
        raise AppointmentNotFound()

    if appointment.status != Appointment.SCHEDULED:
        raise InvalidTransition(
            f"A {appointment.get_status_display().lower()} appointment cannot be closed out.")

    with transaction.atomic():
        appointment.status = status
        appointment.save(update_fields=['status', 'updated_at'])
        _notify(_ON_OUTCOME, appointment)
    return appointment


# ---------------------------------------------------------------------------
# Queries used by the dashboards
# ---------------------------------------------------------------------------
def _now():
    return availability.now()


def _day_bounds(day):
    start = combine(day, datetime.time.min)
    return start, start + datetime.timedelta(days=1)


def upcoming_for_patient(patient, limit=None):
    """Scheduled, still-in-the-future appointments, soonest first."""
    qs = (Appointment.objects
          .filter(patient=patient, status=Appointment.SCHEDULED,
                  start_at__gte=_now())
          .select_related(*_RELATED)
          .order_by('start_at'))
    return qs[:limit] if limit else qs


def upcoming_for_provider(provider_user, limit=None):
    """The provider's own upcoming scheduled appointments, soonest first."""
    qs = (Appointment.objects
          .filter(provider=provider_user, status=Appointment.SCHEDULED,
                  start_at__gte=_now())
          .select_related(*_RELATED)
          .order_by('start_at'))
    return qs[:limit] if limit else qs


upcoming_for_doctor = upcoming_for_provider


def provider_appointments_on(provider_user, day):
    start, end = _day_bounds(day)
    return (Appointment.objects
            .filter(provider=provider_user, status=Appointment.SCHEDULED,
                    start_at__gte=start, start_at__lt=end)
            .select_related(*_RELATED)
            .order_by('start_at'))


doctor_appointments_on = provider_appointments_on


def daily_counts_for_provider(provider_user, days=7):
    """Appointments per day for the last `days` days (oldest first)."""
    today = availability._local_date(_now())
    first = today - datetime.timedelta(days=days - 1)
    window_start = combine(first, datetime.time.min)
    window_end = combine(today + datetime.timedelta(days=1), datetime.time.min)

    rows = (Appointment.objects
            .filter(provider=provider_user,
                    start_at__gte=window_start, start_at__lt=window_end)
            .exclude(status=Appointment.CANCELLED)
            .values_list('start_at', flat=True))

    counts = {first + datetime.timedelta(days=i): 0 for i in range(days)}
    for start_at in rows:
        day = availability._local_date(start_at)
        if day in counts:
            counts[day] += 1
    return [{"date": d.isoformat(), "count": c} for d, c in sorted(counts.items())]


daily_counts_for_doctor = daily_counts_for_provider


def counts_by_state(provider_user, day=None):
    """Today's headline figures for a provider dashboard."""
    day = day or availability._local_date(_now())
    start, end = _day_bounds(day)
    today = Appointment.objects.filter(provider=provider_user,
                                       start_at__gte=start, start_at__lt=end)
    return {
        "appointments_today": today.filter(
            status=Appointment.SCHEDULED).count(),
        "completed_today": today.filter(status=Appointment.COMPLETED).count(),
        "upcoming": Appointment.objects.filter(
            provider=provider_user, status=Appointment.SCHEDULED,
            start_at__gte=_now()).count(),
        "patients_served": (Appointment.objects
                            .filter(provider=provider_user,
                                    status=Appointment.COMPLETED)
                            .values('patient_id').distinct().count()),
    }


def search_availability(role=None, service_name=None, date=None, limit=25):
    """Providers with bookable slots on ``date`` — the patient's search.

    Returns each provider together with its slots, so the caller renders one
    result per provider rather than a flat list of times it has to regroup.
    """
    date = date or availability._local_date(_now())
    results = []
    for provider in list_providers(role):
        user = User.objects.filter(pk=provider["id"]).first()
        if user is None:
            continue
        services = list(list_services(user))
        if service_name:
            needle = service_name.lower()
            services = [s for s in services if needle in s.name.lower()]
            if not services:
                continue

        for service in (services or [None]):
            slots = availability.available_slots(user, date, service)
            if not slots:
                continue
            results.append({
                **provider,
                "service": ({"id": service.id, "name": service.name,
                             "duration_minutes": service.duration_minutes}
                            if service else None),
                "slots": [{"start": s.isoformat(),
                           "start_time": timezone.localtime(s).strftime("%H:%M"),
                           "end": e.isoformat()} for s, e in slots],
            })
            if len(results) >= limit:
                return results
    return results
