"""Tools a doctor may call.

Scoped to the doctor's own practice by construction. The interesting one is
:func:`search_doctor_patient_appointments`: the doctor asks about a patient *by
name*, and the search runs over the patients they already have a care
relationship with — :mod:`appointments.services.care`, the same rule that gates
prescribing and record access.

A doctor therefore cannot reach an unrelated patient through the assistant, and
not because a filter drops them afterwards: a name that belongs to somebody
else's patient simply finds nothing, and the tool says so in the same words it
would use for a name nobody has. Existence is not leaked either way.
"""
import datetime

from django.db.models import Q
from django.utils import timezone

from accounts import roles
from ... import care, scheduling
from .base import ToolError, tool

DOCTOR = (roles.DOCTOR,)
LIMIT = 15


def _day(value, field="date"):
    if not value:
        return None
    text = str(value).strip().lower()
    today = timezone.localdate()
    if text in {"today", "اليوم"}:
        return today
    if text in {"tomorrow", "بكرة", "بكره", "غدا", "غدًا"}:
        return today + datetime.timedelta(days=1)
    try:
        return datetime.date.fromisoformat(text)
    except (ValueError, TypeError):
        raise ToolError(f"'{value}' is not a date I can read for {field}. "
                        f"Use YYYY-MM-DD, 'today' or 'tomorrow'.") from None


def _patient_name(user):
    return user.get_full_name() or user.username


def _entry(appointment):
    return {
        "id": appointment.id,
        "date": appointment.date.isoformat(),
        "time": appointment.time.strftime("%H:%M"),
        "status": appointment.get_status_display(),
        "patient": _patient_name(appointment.patient),
        "service": appointment.service.name if appointment.service else None,
        "reason": appointment.reason or "",
    }


@tool(name="get_doctor_appointments",
      description="The signed-in doctor's own appointments, optionally for one "
                  "day. Accepts 'today' or 'tomorrow'.",
      parameters={
          "type": "object",
          "properties": {
              "date": {"type": "string",
                       "description": "YYYY-MM-DD, 'today' or 'tomorrow'. "
                                      "Omit for everything upcoming."},
          },
      },
      roles=DOCTOR)
def get_doctor_appointments(user, date=""):
    day = _day(date)
    if day is not None:
        found = list(scheduling.provider_appointments_on(user, day))
        scope = day.isoformat()
    else:
        found = list(scheduling.upcoming_for_provider(user, limit=LIMIT))
        scope = "upcoming"

    return {"ok": True, "scope": scope, "count": len(found),
            "appointments": [_entry(a) for a in found],
            "note": ("Nothing booked for that day." if not found else "")}


@tool(name="get_doctor_schedule",
      description="How many appointments the signed-in doctor has each day "
                  "over the coming week.",
      parameters={
          "type": "object",
          "properties": {
              "days": {"type": "integer",
                       "description": "How many days ahead, up to 30."},
          },
      },
      roles=DOCTOR)
def get_doctor_schedule(user, days=7):
    try:
        days = max(1, min(int(days), 30))
    except (TypeError, ValueError):
        days = 7
    counts = scheduling.daily_counts_for_provider(user, days=days)
    return {"ok": True, "days": days, "schedule": counts}


@tool(name="search_doctor_patient_appointments",
      description="Whether a named patient has an appointment with the "
                  "signed-in doctor, and when. Only searches patients this "
                  "doctor already treats.",
      parameters={
          "type": "object",
          "properties": {
              "patient_name": {
                  "type": "string",
                  "description": "The patient's name as the doctor said it."},
          },
          "required": ["patient_name"],
      },
      roles=DOCTOR)
def search_doctor_patient_appointments(user, patient_name):
    needle = (patient_name or "").strip()
    if not needle:
        raise ToolError("Tell me which patient to look for.")

    # Only this doctor's own patients are searchable at all.
    candidates = care.patients_of(user).filter(
        Q(first_name__icontains=needle) | Q(last_name__icontains=needle)
        | Q(username__icontains=needle))[:LIMIT]

    matches = []
    for patient in candidates:
        appointments = list(
            scheduling.list_provider_appointments(user)
            .filter(patient=patient)[:LIMIT])
        if not appointments:
            continue
        matches.append({"patient": _patient_name(patient),
                        "appointments": [_entry(a) for a in appointments]})

    if not matches:
        return {
            "ok": True, "found": False, "matches": [],
            # The same answer for "no such person" and "not your patient".
            # Distinguishing them would confirm that a patient exists.
            "note": f"No patient called '{needle}' has an appointment with "
                    f"you. Say exactly that — do not speculate about whether "
                    f"they exist or booked with someone else.",
        }
    return {"ok": True, "found": True, "matches": matches}


@tool(name="get_doctor_patients",
      description="The patients the signed-in doctor treats, by name.",
      roles=DOCTOR)
def get_doctor_patients(user):
    patients = [_patient_name(p) for p in care.patients_of(user)[:LIMIT]]
    return {"ok": True, "count": len(patients), "patients": patients,
            "note": "Nobody has booked with you yet." if not patients else ""}
