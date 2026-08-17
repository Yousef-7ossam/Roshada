"""Notifications raised by events the scheduling engine owns.

Appointments live in ``appointments``, which sits below every
clinical module and must not import one — so these subscribe to its registries
instead. The clinical modules (radiology, pharmacy) call
``notifications.notify`` directly from their own services: every module calls
the one centralized notifier rather than growing its own notification logic.

Nothing here can break the action that triggered it. ``notify`` runs each
write in its own savepoint and never raises, so a booking is never lost
because a notification could not be written.
"""
import logging

from appointments.models import Appointment
from appointments.serializers import provider_brief
from appointments.services import scheduling

from . import notifications, types

logger = logging.getLogger("appointments")

SOURCE = "appointments.Appointment"


def _when(appointment):
    from django.utils import timezone
    moment = timezone.localtime(appointment.start_at)
    return f"{moment:%Y-%m-%d} at {moment:%H:%M}"


def _service(appointment):
    return appointment.service.name if appointment.service else "appointment"


@scheduling.on_appointment_created
def announce_booking(appointment):
    """Both sides hear about a new booking, in their own words.

    The patient is told who they are seeing; the provider is told who is
    coming. Only these two — an appointment is nobody else's business, so
    there is no broadcast here.
    """
    provider = appointment.provider
    notifications.notify(
        appointment.patient, types.APPOINTMENT_CREATED,
        "Appointment booked",
        f"Your {_service(appointment)} with "
        f"{notifications.display_name(provider)} is on {_when(appointment)}.",
        source=SOURCE, reference=appointment.id)
    notifications.notify(
        provider, types.APPOINTMENT_CREATED,
        "New appointment",
        f"{notifications.patient_name(appointment.patient)} booked "
        f"{_when(appointment)}.",
        source=SOURCE, reference=appointment.id)


@scheduling.on_appointment_cancelled
def announce_cancellation(appointment):
    """Both parties are told, including whoever cancelled.

    The engine's callback signature carries no actor, and changing it would
    ripple into radiology, which already uses it. Notifying both is also the
    better behaviour: a confirmation that your own cancellation went through is
    worth having, and the alternative — guessing who acted — would sometimes
    tell the wrong person nothing.
    """
    notifications.notify_users(
        [appointment.patient, appointment.provider],
        types.APPOINTMENT_CANCELLED, "Appointment cancelled",
        f"The {_service(appointment)} on {_when(appointment)} was cancelled.",
        source=SOURCE, reference=appointment.id)


@scheduling.on_appointment_rescheduled
def announce_reschedule(appointment, previous_start):
    """Say what changed, not merely that something did."""
    from django.utils import timezone
    was = timezone.localtime(previous_start)
    notifications.notify_users(
        [appointment.patient, appointment.provider],
        types.APPOINTMENT_RESCHEDULED, "Appointment rescheduled",
        f"Moved from {was:%Y-%m-%d %H:%M} to {_when(appointment)}.",
        source=SOURCE, reference=appointment.id)


@scheduling.on_appointment_outcome
def announce_outcome(appointment):
    """Only the patient, and only for a completed visit.

    A no-show is not something to notify the patient about — they know, and a
    push about it would be an accusation rather than information. The provider
    set the outcome themselves, so telling them is noise.
    """
    if appointment.status != Appointment.COMPLETED:
        return
    notifications.notify(
        appointment.patient, types.APPOINTMENT_COMPLETED, "Visit completed",
        f"Your {_service(appointment)} with "
        f"{notifications.display_name(appointment.provider)} is complete.",
        source=SOURCE, reference=appointment.id)


def announce_provider_confirmation(appointment):
    """A provider explicitly confirming a booking.

    Kept as a plain function rather than a registry subscriber because Roshada
    has no separate confirmation step today — a booking is confirmed the
    moment the engine accepts it. Declared so the workflow has one obvious
    home if that step is ever added, and called by nothing in the meantime.
    """
    provider = provider_brief(appointment.provider)
    return notifications.notify(
        appointment.patient, types.APPOINTMENT_CONFIRMED,
        "Appointment confirmed",
        f"{provider['name']} confirmed your {_when(appointment)} appointment.",
        source=SOURCE, reference=appointment.id)
