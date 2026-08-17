"""Availability rules, blocked time, and the slots that fall out of them.

Slots are **generated, never stored**. A stored slot table would need writing
for every provider for every future day, kept in step with every rule edit, and
garbage-collected — three jobs that exist only to cache an answer this module
computes from four small queries. Generation also means a rule change takes
effect immediately instead of at the next materialisation.

The pipeline for one provider on one date:

    rules for that date  ->  cut into slots  ->  drop past
                                             ->  drop blocked (TimeOff)
                                             ->  drop taken (scheduled appointments)

:func:`available_slots` and :func:`validate_slot` run the *same* pipeline, which
is what stops the list a patient sees from disagreeing with what booking will
accept.
"""
import datetime

from django.db.models import Q
from django.utils import timezone

from ..models import Appointment, AvailabilityRule, TimeOff

#: Slot length when nothing else decides — no service, and a rule that did not
#: set its own. Matches the length the pre-unification booking form assumed.
DEFAULT_SLOT_MINUTES = 30

#: How far ahead slots may be requested. Beyond this a date is almost certainly
#: a typo in the year, and generating a year of slots serves nobody.
MAX_DAYS_AHEAD = 365


class OutsideAvailability(Exception):
    """The requested period is not one this provider offers."""


class SlotTaken(OutsideAvailability):
    """The period is offered, but somebody already has it.

    A subclass because it *is* a kind of unavailability, and every caller that
    only cares "can I book this" keeps working. It is separate because the two
    mean different things to the client: "not offered" is a bad request, while
    "gone" is a conflict — retry with another time, the provider is fine.
    """


class ProviderUnavailable(Exception):
    """The provider publishes no availability at all."""


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def combine(date, time):
    """A timezone-aware instant from a local calendar date and wall-clock time.

    Every conversion between "what the calendar says" and "what is stored" goes
    through here, so there is exactly one place naive datetimes could leak in.
    """
    return timezone.make_aware(datetime.datetime.combine(date, time),
                               timezone.get_default_timezone())


def now():
    return timezone.now()


def _local_date(moment):
    return timezone.localtime(moment).date()


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def rules_on(provider, date, service=None):
    """The active rules that open ``date`` for ``service``.

    A rule written for a specific date **replaces** the weekly pattern for that
    date rather than adding to it. That is what makes an override an override:
    "this Saturday we open 10:00-14:00" must not also leave the usual Saturday
    hours bookable.
    """
    matching_service = Q(service__isnull=True)
    if service is not None:
        matching_service |= Q(service=service)

    base = (AvailabilityRule.objects
            .filter(provider=provider, is_active=True)
            .filter(matching_service))

    dated = list(base.filter(date=date))
    if dated:
        return dated
    return list(base.filter(weekday=date.weekday(), date__isnull=True))


def has_rules(provider):
    """Whether this provider publishes any availability at all.

    Used to keep pre-unification behaviour working: a doctor who has never
    opened hours is still bookable free-form, exactly as before. Once they
    publish a single rule, their availability is enforced.
    """
    return AvailabilityRule.objects.filter(provider=provider,
                                           is_active=True).exists()


def slot_length(rule, service=None):
    """Minutes per slot. The service wins when there is one.

    Only ever one of the two decides — a 60-minute MRI offered on a rule's
    30-minute grid would hand out slots that cannot hold the appointment.
    """
    if service is not None and service.duration_minutes:
        return service.duration_minutes
    return rule.slot_minutes or DEFAULT_SLOT_MINUTES


# ---------------------------------------------------------------------------
# Blocked time and existing bookings
# ---------------------------------------------------------------------------
def blocked_periods(provider, date):
    """TimeOff on ``date``, as aware ``(start, end)`` pairs.

    An all-day entry becomes the whole calendar day, so callers only ever deal
    with one shape.
    """
    periods = []
    for entry in TimeOff.objects.filter(provider=provider, date=date):
        if entry.is_all_day:
            start = combine(date, datetime.time.min)
            end = combine(date + datetime.timedelta(days=1), datetime.time.min)
        else:
            start = combine(date, entry.start_time)
            end = combine(date, entry.end_time)
        periods.append((start, end))
    return periods


def booked_periods(provider, date, exclude_appointment=None):
    """Periods already occupied by the provider's scheduled appointments.

    Filtered on the stored instants rather than a local date, so an appointment
    is matched by when it actually is.
    """
    day_start = combine(date, datetime.time.min)
    day_end = combine(date + datetime.timedelta(days=1), datetime.time.min)
    queryset = Appointment.objects.filter(
        provider=provider, status=Appointment.SCHEDULED,
        start_at__lt=day_end, end_at__gt=day_start)
    if exclude_appointment is not None:
        queryset = queryset.exclude(pk=exclude_appointment.pk)
    return list(queryset.values_list('start_at', 'end_at'))


def overlaps(start, end, periods):
    """Half-open overlap: touching periods do not conflict.

    Same semantics as the database's ``[)`` exclusion constraint, so what this
    module offers and what PostgreSQL will accept cannot disagree.
    """
    return any(start < other_end and end > other_start
               for other_start, other_end in periods)


# ---------------------------------------------------------------------------
# Slot generation
# ---------------------------------------------------------------------------
def _cut(rule, date, minutes):
    """Whole slots of ``minutes`` inside one rule's window."""
    step = datetime.timedelta(minutes=minutes)
    window_start = combine(date, rule.start_time)
    window_end = combine(date, rule.end_time)

    slots = []
    cursor = window_start
    # Only slots that fit *entirely* inside the window: half a slot at the end
    # of a session is not something a provider can honour.
    while cursor + step <= window_end:
        slots.append((cursor, cursor + step))
        cursor += step
    return slots


def candidate_slots(provider, date, service=None):
    """Every slot the rules produce for ``date``, before availability filters."""
    slots = set()
    for rule in rules_on(provider, date, service):
        slots.update(_cut(rule, date, slot_length(rule, service)))
    return sorted(slots)


def available_slots(provider, date, service=None, exclude_appointment=None):
    """Bookable slots for one provider on one date, earliest first.

    ``exclude_appointment`` lets a reschedule see its *own* current slot as
    free; without it, moving an appointment 30 minutes later would collide with
    the copy of itself it is about to vacate.
    """
    slots = candidate_slots(provider, date, service)
    if not slots:
        return []

    moment = now()
    blocked = blocked_periods(provider, date)
    taken = booked_periods(provider, date, exclude_appointment)

    return [(start, end) for start, end in slots
            if start >= moment
            and not overlaps(start, end, blocked)
            and not overlaps(start, end, taken)]


def describe_slots(provider, date, service=None):
    """Slots as JSON-ready dicts, including the ones that are gone.

    The unavailable entries are deliberate: "08:00 is booked" tells a patient
    something that a silently shorter list does not, and it is what makes the
    grid look like a real schedule rather than an arbitrary set of times.
    """
    moment = now()
    blocked = blocked_periods(provider, date)
    taken = booked_periods(provider, date)

    described = []
    for start, end in candidate_slots(provider, date, service):
        if start < moment:
            state = "past"
        elif overlaps(start, end, taken):
            state = "booked"
        elif overlaps(start, end, blocked):
            state = "unavailable"
        else:
            state = "available"
        described.append({
            "start": start.isoformat(),
            "end": end.isoformat(),
            "start_time": timezone.localtime(start).strftime("%H:%M"),
            "end_time": timezone.localtime(end).strftime("%H:%M"),
            "state": state,
            "available": state == "available",
        })
    return described


# ---------------------------------------------------------------------------
# The single validation path
# ---------------------------------------------------------------------------
def validate_slot(provider, start_at, end_at, service=None,
                  exclude_appointment=None):
    """Raise unless this exact period is one the provider is offering.

    Called by booking *and* by rescheduling, inside their transaction — never
    trusting that the slot list a client saw is still true. Whatever was on
    screen, the answer is recomputed here.
    """
    if start_at < now():
        raise OutsideAvailability("That time is in the past.")

    date = _local_date(start_at)
    if (date - _local_date(now())).days > MAX_DAYS_AHEAD:
        raise OutsideAvailability(
            f"Appointments cannot be booked more than {MAX_DAYS_AHEAD} days ahead.")

    if not rules_on(provider, date, service):
        raise ProviderUnavailable(
            "This provider is not open for bookings on that date.")

    if (start_at, end_at) not in set(candidate_slots(provider, date, service)):
        raise OutsideAvailability(
            "That is not one of the times this provider offers on that date.")

    if overlaps(start_at, end_at, blocked_periods(provider, date)):
        raise OutsideAvailability("The provider is unavailable at that time.")

    if overlaps(start_at, end_at,
                booked_periods(provider, date, exclude_appointment)):
        # Detected here for a clear message; the database has the final word
        # when the insert runs, which is what makes two simultaneous bookings
        # safe even though both pass this check.
        raise SlotTaken("That slot has just been taken.")


def next_available(provider, service=None, days=14, limit=1, start_date=None):
    """Scan forward for the soonest bookable slots. Used by the booking UI."""
    first = start_date or _local_date(now())
    found = []
    for offset in range(days):
        date = first + datetime.timedelta(days=offset)
        for start, end in available_slots(provider, date, service):
            found.append((start, end))
            if len(found) >= limit:
                return found
    return found
