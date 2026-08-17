"""The one notification service every module calls.

This is the whole public surface for raising a notification::

    from comms import notifications

    notifications.notify(patient, notifications.types.PRESCRIPTION_CREATED,
                         "New prescription",
                         f"Dr. {name} issued a prescription.",
                         source="pharmacy.Prescription", reference=rx.id)

**Raising a notification can never break the thing that raised it.** Every
write here runs inside its own savepoint and swallows its own failures. That
matters more than it looks: the callers are clinical actions running inside
open transactions — releasing a report, confirming a medication request — and
an unhandled database error inside such a transaction poisons it, so *every
later query fails too* and the clinical action is lost. A nested ``atomic``
block rolls back only the notification.

It also gives the right semantics for free: a notification written inside a
source transaction only survives if that transaction commits. A patient is
never told about a booking that was rolled back.
"""
import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts import roles
from accounts.services import role_of

from . import channels, types
from .models import Notification

logger = logging.getLogger("appointments")


def notify(recipient, notification_type, title, body="", source="",
           reference=None, dedupe_unread=False):
    """Create one notification. Returns it, or None if it was not created.

    ``dedupe_unread`` skips creation when the recipient already has an unread
    notification of the same type about the same record — which is what keeps
    a ten-message exchange from producing ten identical unread badges.

    Never raises. See the module docstring for why that is a deliberate
    guarantee rather than defensive habit.
    """
    if recipient is None:
        return None
    if not types.is_valid(notification_type):
        # A typo'd type would be unfilterable and invisible in the UI. Log it
        # loudly rather than storing a row nobody can find.
        logger.error("Unknown notification type %r (not sent to %s)",
                     notification_type, getattr(recipient, "username", "?"))
        return None

    try:
        with transaction.atomic():
            if dedupe_unread and _has_unread(recipient, notification_type,
                                             source, reference):
                return None
            notification = Notification.objects.create(
                recipient=recipient, type=notification_type,
                title=title[:160], body=(body or "")[:300],
                source=source or "", reference=reference)
    except Exception:                                       # noqa: BLE001
        logger.exception("Could not raise %s for %s", notification_type,
                         getattr(recipient, "username", "?"))
        return None

    channels.deliver(notification)
    return notification


def notify_users(recipients, notification_type, title, body="", source="",
                 reference=None, dedupe_unread=False):
    """Same, for several people. Skips ``None`` and duplicate recipients."""
    sent, seen = [], set()
    for recipient in recipients:
        if recipient is None or recipient.pk in seen:
            continue
        seen.add(recipient.pk)
        created = notify(recipient, notification_type, title, body,
                         source=source, reference=reference,
                         dedupe_unread=dedupe_unread)
        if created is not None:
            sent.append(created)
    return sent


def _has_unread(recipient, notification_type, source, reference):
    return Notification.objects.filter(
        recipient=recipient, type=notification_type, source=source or "",
        reference=reference, read_at__isnull=True).exists()


# ---------------------------------------------------------------------------
# Reading — scoped to the caller, always
# ---------------------------------------------------------------------------
def for_user(user, category=None, notification_type=None, unread_only=False):
    """This user's own notifications. There is no cross-user queryset here.

    Every read path in this module starts from ``recipient=user``, so there is
    no code path — not even an internal one — that returns somebody else's
    notifications.
    """
    queryset = Notification.objects.filter(recipient=user)
    if unread_only:
        queryset = queryset.filter(read_at__isnull=True)
    if notification_type:
        queryset = queryset.filter(type=notification_type)
    elif category:
        queryset = queryset.filter(type__in=types.types_in(category))
    return queryset


def unread_count(user):
    """One indexed COUNT, not a fetch-and-len.

    The badge renders on every page, so this deliberately never loads rows —
    the ``(recipient, read_at)`` index answers it directly.
    """
    return Notification.objects.filter(recipient=user,
                                       read_at__isnull=True).count()


def unread_by_category(user):
    """Unread totals per filter tab, in one grouped query."""
    from django.db.models import Count

    counts = {category: 0 for category in types.CATEGORIES}
    rows = (Notification.objects
            .filter(recipient=user, read_at__isnull=True)
            .values("type").annotate(total=Count("id")))
    for row in rows:
        counts[types.category_of(row["type"])] = (
            counts.get(types.category_of(row["type"]), 0) + row["total"])
    return counts


def mark_read(user, notification_id, read=True):
    """Mark one of the caller's own notifications read or unread.

    Scoped by recipient, so another user's id is simply not found — the id in
    the URL selects among *this user's* rows and can never reach anyone else's.
    """
    notification = Notification.objects.filter(
        pk=notification_id, recipient=user).first()
    if notification is None:
        return None
    return notification.mark_read() if read else notification.mark_unread()


def mark_all_read(user, category=None):
    """One UPDATE, not a row-by-row loop."""
    queryset = Notification.objects.filter(recipient=user,
                                           read_at__isnull=True)
    if category:
        queryset = queryset.filter(type__in=types.types_in(category))
    return queryset.update(read_at=timezone.now())


# ---------------------------------------------------------------------------
# Appointment reminders
# ---------------------------------------------------------------------------
def create_due_reminders(within_hours=24):
    """Raise a reminder for each scheduled appointment starting soon.

    Deliberately **not** run automatically. Roshada has no scheduler, no
    Celery and no cron integration, and this is not the place to invent
    background jobs — so this is a function a management command calls, and an
    operator schedules if they want it.

    Safe to run repeatedly: the partial unique constraint on
    ``(recipient, type, source, reference)`` means a second run cannot notify
    the same patient about the same appointment twice.
    """
    import datetime

    from appointments.models import Appointment
    from appointments.serializers import provider_brief

    now = timezone.now()
    horizon = now + datetime.timedelta(hours=within_hours)
    due = (Appointment.objects
           .filter(status=Appointment.SCHEDULED, start_at__gte=now,
                   start_at__lte=horizon)
           .select_related("patient", "provider", "provider__account",
                           "provider__doctor_profile",
                           "provider__laboratory_profile",
                           "provider__radiology_profile", "service"))

    raised = []
    for appointment in due:
        provider = provider_brief(appointment.provider)
        prefix = "Dr. " if provider["role"] == roles.DOCTOR else ""
        when = timezone.localtime(appointment.start_at)
        created = notify(
            appointment.patient, types.APPOINTMENT_REMINDER,
            "Upcoming appointment",
            f"{prefix}{provider['name']} on {when:%Y-%m-%d} at {when:%H:%M}.",
            source="appointments.Appointment", reference=appointment.id)
        if created is not None:
            raised.append(created)
    return raised


# ---------------------------------------------------------------------------
# Helpers shared by the event hooks
# ---------------------------------------------------------------------------
def display_name(user):
    """How a person is named inside a notification body.

    Doctors are titled; a facility takes its profile name, because "your
    appointment with roshada_lab_1" is not something to show a patient.
    """
    if user is None:
        return "your provider"
    role = role_of(user)
    for attribute in ("doctor_profile", "laboratory_profile",
                      "radiology_profile", "pharmacy_profile"):
        profile = getattr(user, attribute, None)
        if profile is not None and getattr(profile, "name", ""):
            name = profile.name
            break
    else:
        name = user.get_full_name() or user.username
    return f"Dr. {name}" if role == roles.DOCTOR else name


def patient_name(user):
    if user is None:
        return "A patient"
    return user.get_full_name() or user.username


def purge_for(user):
    """Delete a user's notifications. Only used by the account owner.

    Roshada has no retention policy and does not expire notifications; this exists so a person can clear their own centre, which is
    the one deletion the product actually needs.
    """
    return Notification.objects.filter(Q(recipient=user)).delete()
