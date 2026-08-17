"""Dashboard aggregation use-cases.

Both dashboards previously rendered hardcoded fixtures — invented appointment
counts, a fabricated "medication adherence" percentage, and a demo health-score
trend line. In a medical UI those read as real clinical figures.

Everything here is derived from the database. Where the product has no data
source for a tile (prescriptions, medication adherence), the summary reports
``None`` so the UI can show an honest empty state rather than a number.
"""
from django.db.models import Count
from django.utils import timezone

from accounts import roles
from accounts.models import LaboratoryProfile, PharmacyProfile, RadiologyProfile
from accounts.services import directory, role_of

from ..models import Appointment, Doctor
from ..serializers import provider_brief
from . import availability, scheduling

#: Tiles the UI must render as "not tracked yet" rather than invent a value.
#: Documentation of intent: the list the API actually returns is derived in
#: ``summary_for`` from which figures came back None, so it cannot go stale.
#: ``active_prescriptions`` left this list when the Pharmacy module gave it a
#: real source — which is the mechanism working as designed.
UNSUPPORTED_METRICS = ('medication_adherence',)

#: Facility tiles with no data source yet. A domain module that implements one
#: of them contributes a real number (see ``contribute`` below) and the tile
#: stops being unsupported — ``unsupported_metrics`` is derived from which
#: values actually came back None, so the two can never drift apart.
#:
#: None rather than 0 because the difference matters: 0 claims we counted and
#: found none, None says there is nothing to count. In a clinical product the
#: first is a lie.
FACILITY_UNSUPPORTED_METRICS = (
    'pending_orders', 'in_progress', 'results_ready',
)

# ---------------------------------------------------------------------------
# Domain contributions
#
# A clinical module adds its own figures without this module importing it — the
# dependency runs domain -> engine, the same way the booking callbacks do.
# A contributor is ``fn(user, role) -> dict``; a ``stats`` key merges into the
# summary's stats, everything else is added at the top level.
# ---------------------------------------------------------------------------
_CONTRIBUTORS = []


def contribute(callback):
    """Register a summary contributor. Returns it, so it can be used bare."""
    if callback not in _CONTRIBUTORS:
        _CONTRIBUTORS.append(callback)
    return callback


def _apply_contributions(summary, user, role):
    for callback in _CONTRIBUTORS:
        extra = callback(user, role) or {}
        summary.setdefault("stats", {}).update(extra.pop("stats", {}))
        summary.update(extra)
    return summary


def _serialise_appointment(appointment):
    """One appointment as the dashboards and notifications read it.

    ``provider``/``provider_role`` replace the old ``doctor`` key: the same
    summary now describes a lab visit and an imaging appointment, so naming the
    counterparty "doctor" would have been wrong two times out of three.
    """
    provider = provider_brief(appointment.provider)
    return {
        "id": appointment.id,
        "date": appointment.date.isoformat(),
        "time": appointment.time.strftime("%H:%M"),
        "end_time": appointment.end_time.strftime("%H:%M"),
        "status": appointment.status,
        "reason": appointment.reason,
        "provider": provider["name"],
        "provider_role": provider["role"],
        "specialization": provider["detail"],
        "service": appointment.service.name if appointment.service else None,
        "patient": appointment.patient.get_full_name() or appointment.patient.username,
        "patient_id": appointment.patient_id,
    }


def patient_summary(user):
    """Real figures for the patient dashboard."""
    upcoming = list(scheduling.upcoming_for_patient(user, limit=5))
    all_appointments = Appointment.objects.filter(patient=user)

    return {
        "role": "patient",
        "stats": {
            "upcoming_appointments": len(upcoming),
            "total_appointments": all_appointments.count(),
            "completed_appointments": all_appointments.filter(
                status=Appointment.COMPLETED).count(),
            # No data source exists for these yet — never fabricate a value.
            "active_prescriptions": None,
            "medication_adherence": None,
        },
        "upcoming_appointments": [_serialise_appointment(a) for a in upcoming],
    }


def doctor_summary(user):
    """Real figures for the doctor dashboard."""
    today = timezone.localtime().date()
    # The doctor is now addressed as a provider, like every other kind.
    mine = Appointment.objects.filter(provider=user).select_related(
        'provider', 'patient', 'service')

    today_appointments = list(scheduling.provider_appointments_on(user, today))
    upcoming = list(scheduling.upcoming_for_provider(user, limit=5))
    distinct_patients = mine.values_list('patient_id', flat=True).distinct().count()

    recent_activity = [
        {
            "text": f"{_serialise_appointment(a)['patient']} — {a.get_status_display().lower()}"
                    f" visit on {a.date.isoformat()}",
            "status": a.status,
            "at": a.updated_at.isoformat(),
        }
        for a in mine.order_by('-updated_at')[:5]
    ]

    return {
        "role": "doctor",
        "bookable": True,
        "stats": {
            # The scheduling block every provider dashboard shares, so a
            # doctor and a laboratory name the same figure the same way.
            **scheduling.counts_by_state(user),
            # Doctor-specific figures. Listed second so they win on the one
            # key both produce (appointments_today), which means the existing
            # dashboard reads exactly what it always did.
            "appointments_today": len(today_appointments),
            "upcoming_appointments": len(upcoming),
            "total_patients": distinct_patients,
            "completed_appointments": mine.filter(status=Appointment.COMPLETED).count(),
            "cancelled_appointments": mine.filter(status=Appointment.CANCELLED).count(),
            "medication_adherence": None,
        },
        "today": [_serialise_appointment(a) for a in today_appointments],
        "upcoming_appointments": [_serialise_appointment(a) for a in upcoming],
        "weekly_appointments": scheduling.daily_counts_for_provider(user, days=7),
        "recent_activity": recent_activity,
        "publishes_availability": availability.has_rules(user),
    }


def facility_summary(user, role):
    """The laboratory / radiology / pharmacy dashboard.

    Scheduling figures are real for the two bookable facilities. Pharmacy is
    not bookable, so it sees no scheduling figures at all rather than zeroes
    that would read as "no bookings today".

    The order/sample/inventory tiles start ``None`` and are filled in by
    whichever domain module actually implements them — radiology and pharmacy
    do today, laboratory does not yet. Nothing here needs to know which: the
    contributions are applied in ``summary_for`` and ``unsupported_metrics``
    is derived from what is still ``None`` afterwards.
    """
    profile = getattr(user, f"{role}_profile", None)
    stats = {metric: None for metric in FACILITY_UNSUPPORTED_METRICS}

    bookable = role in roles.BOOKABLE_ROLES
    if bookable:
        stats.update(scheduling.counts_by_state(user))
    else:
        stats.update({"appointments_today": None, "completed_today": None,
                      "upcoming": None, "patients_served": None})

    today = [] if not bookable else [
        _serialise_appointment(a) for a in
        scheduling.provider_appointments_on(
            user, timezone.localtime().date())]

    return {
        "role": role,
        "role_label": roles.label(role),
        "bookable": bookable,
        "facility": {
            "name": profile.name if profile else user.get_full_name() or user.username,
            "verified": profile.verified if profile else False,
            "available": profile.available if profile else False,
            "services": profile.services if profile else "",
            "operating_hours": profile.operating_hours if profile else "",
        },
        "stats": stats,
        "today": today,
        "upcoming_appointments": ([] if not bookable else [
            _serialise_appointment(a)
            for a in scheduling.upcoming_for_provider(user, limit=5)]),
        "publishes_availability": (
            bookable and availability.has_rules(user)),
    }


def admin_summary(user):
    """Platform-wide figures for the administrator.

    Counts and status only. An administrator needs to see that the platform is
    healthy, not to read anybody's medical history, so no clinical content
    appears here.
    """
    # Resolved through accounts.services.directory() rather than counted
    # straight off UserAccount: a user created outside registration (a
    # superuser, typically) has no account row, and counting rows would leave
    # them out of every figure while still appearing in total_users. The
    # numbers have to add up.
    entries = directory()
    by_role = {role: 0 for role in roles.ALL_ROLES}
    by_status = {}
    for entry in entries:
        by_role[entry["role"]] = by_role.get(entry["role"], 0) + 1
        by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1

    appointments_by_status = {
        row["status"]: row["count"]
        for row in Appointment.objects.values("status").annotate(count=Count("id"))
    }

    return {
        "role": roles.ADMIN,
        "role_label": roles.label(roles.ADMIN),
        "stats": {
            # From the same resolved pass, so total_users always equals the sum
            # of users_by_role.
            "total_users": len(entries),
            "total_appointments": Appointment.objects.count(),
            "doctors_available": Doctor.objects.filter(available=True).count(),
            "facilities_pending_verification": sum(
                model.objects.filter(verified=False).count()
                for model in (LaboratoryProfile, RadiologyProfile, PharmacyProfile)
            ),
        },
        # Zero is a real answer here: every role was counted and found empty.
        "users_by_role": by_role,
        "users_by_status": by_status,
        "appointments_by_status": appointments_by_status,
        # The live permission matrix, read from accounts.roles rather than
        # restated — the admin screen shows what is actually enforced.
        "permissions": {role: sorted(roles.capabilities(role))
                        for role in roles.ALL_ROLES},
        "planned_capabilities": sorted(roles.PLANNED_CAPABILITIES),
    }


#: role -> the function that builds its dashboard.
SUMMARY_BUILDERS = {
    roles.PATIENT: lambda user, role: patient_summary(user),
    roles.DOCTOR: lambda user, role: doctor_summary(user),
    roles.LABORATORY: facility_summary,
    roles.RADIOLOGY: facility_summary,
    roles.PHARMACY: facility_summary,
    roles.ADMIN: lambda user, role: admin_summary(user),
}


def summary_for(user):
    """Role-appropriate dashboard payload.

    Dispatched on the user's *role*, not on which profile row happens to exist.
    An unknown role falls back to the patient view, which only ever reads the
    caller's own data — the safe direction to fail.
    """
    role = role_of(user)
    builder = SUMMARY_BUILDERS.get(role, SUMMARY_BUILDERS[roles.PATIENT])
    summary = _apply_contributions(builder(user, role), user, role)

    # Derived last, from what the figures actually are: a tile is "unsupported"
    # exactly when nothing was able to produce a number for it. A domain module
    # implementing one removes it from this list simply by returning a count.
    summary["unsupported_metrics"] = sorted(
        key for key, value in summary.get("stats", {}).items() if value is None)
    return summary
