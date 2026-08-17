"""Tools a patient may call.

Every one of these is scoped to the caller by construction: the handler takes
``user`` from the request and passes it to the same service the patient's own
pages use. There is no argument for "whose", so a patient cannot ask about
another patient — not because a check refuses it, but because the question
cannot be expressed.

Provider *directory* data (which doctors exist, when they are free) is public to
any signed-in patient, which is why a provider id is the one identifier these
schemas accept.
"""
import datetime

from django.utils import timezone

from accounts import roles
from ... import availability, scheduling
from .base import ToolError, tool

PATIENT = (roles.PATIENT,)

#: Keeps a tool result small enough to read and cheap enough to send.
LIMIT = 10


def _date(value, field="date"):
    """Parse an ISO date from the model, or explain why it could not be."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value).strip())
    except (ValueError, TypeError):
        raise ToolError(f"'{value}' is not a date I can read for {field}. "
                        f"Use YYYY-MM-DD.") from None


def _appointment_dict(appointment):
    from ....serializers import provider_brief

    provider = provider_brief(appointment.provider)
    return {
        "id": appointment.id,
        "date": appointment.date.isoformat(),
        "time": appointment.time.strftime("%H:%M"),
        "status": appointment.get_status_display(),
        "provider": provider["name"],
        "provider_role": provider["role"],
        "provider_detail": provider["detail"],
        "service": appointment.service.name if appointment.service else None,
        "reason": appointment.reason or "",
    }


# ---------------------------------------------------------------------------
# Finding care
# ---------------------------------------------------------------------------
@tool(name="search_doctors",
      description="Find doctors registered in Roshada, optionally filtered by "
                  "specialisation or name. Returns real doctors only.",
      parameters={
          "type": "object",
          "properties": {
              "specialization": {
                  "type": "string",
                  "description": "Specialisation to match, e.g. cardiology."},
              "name": {"type": "string",
                       "description": "Part of the doctor's name."},
          },
      },
      roles=PATIENT)
def search_doctors(user, specialization="", name=""):
    doctors = []
    for provider in scheduling.list_providers(role=roles.DOCTOR):
        if specialization and specialization.strip().lower() not in (
                provider["detail"] or "").lower():
            continue
        if name and name.strip().lower() not in provider["name"].lower():
            continue
        doctors.append({
            "doctor_user_id": provider["id"],
            "name": provider["name"],
            "specialization": provider["detail"],
            "clinic": provider["location"],
        })
        if len(doctors) >= LIMIT:
            break

    return {"ok": True, "count": len(doctors), "doctors": doctors,
            "note": ("No doctor in Roshada matches that." if not doctors
                     else "Use doctor_user_id with get_doctor_availability.")}


@tool(name="get_doctor_availability",
      description="The bookable slots a specific provider has. Use the "
                  "doctor_user_id returned by search_doctors.",
      parameters={
          "type": "object",
          "properties": {
              "doctor_user_id": {
                  "type": "integer",
                  "description": "The provider's user id, from search_doctors."},
              "date": {"type": "string",
                       "description": "Day to check, YYYY-MM-DD. Omit for the "
                                      "next available slots."},
          },
          "required": ["doctor_user_id"],
      },
      roles=PATIENT)
def get_doctor_availability(user, doctor_user_id, date=""):
    try:
        provider = scheduling.resolve_provider(provider_id=doctor_user_id)
    except scheduling.DoctorNotFound:
        raise ToolError("There is no such provider in Roshada.") from None

    day = _date(date)
    if day is not None:
        slots = [s for s in availability.describe_slots(provider, day)
                 if s["available"]]
        return {"ok": True, "provider": provider.get_full_name() or provider.username,
                "date": day.isoformat(),
                "available_times": [s["start_time"] for s in slots][:LIMIT],
                "note": ("Nothing free that day." if not slots else "")}

    upcoming = availability.next_available(provider, days=14, limit=LIMIT)
    return {
        "ok": True,
        "provider": provider.get_full_name() or provider.username,
        "next_available": [
            {"date": timezone.localtime(start).date().isoformat(),
             "time": timezone.localtime(start).strftime("%H:%M")}
            for start, _end in upcoming],
        "note": ("This provider has no published hours in the next two weeks."
                 if not upcoming else ""),
    }


@tool(name="search_availability",
      description="Providers of a given kind with free slots on a date. Use "
                  "for laboratory or radiology availability as well as doctors.",
      parameters={
          "type": "object",
          "properties": {
              "kind": {"type": "string", "enum": ["doctor", "laboratory",
                                                  "radiology"],
                       "description": "Which kind of provider."},
              "date": {"type": "string", "description": "YYYY-MM-DD."},
          },
      },
      roles=PATIENT)
def search_availability(user, kind="", date=""):
    role = {"doctor": roles.DOCTOR, "laboratory": roles.LABORATORY,
            "radiology": roles.RADIOLOGY}.get((kind or "").strip().lower())
    day = _date(date)
    found = scheduling.search_availability(role=role, date=day, limit=LIMIT)
    return {
        "ok": True,
        "date": (day or timezone.localdate()).isoformat(),
        "providers": [{"provider_user_id": r["id"], "name": r["name"],
                       "kind": r["role_label"], "detail": r["detail"],
                       "times": [s["start_time"] for s in r["slots"]][:6]}
                      for r in found],
        # "No free slots today" is not "no doctors". Point at the directory so
        # the model answers the question the person actually asked instead of
        # reporting an empty schedule as an empty platform.
        "note": ("Nobody has published free slots for that day. This does NOT "
                 "mean Roshada has no doctors — call search_doctors to list "
                 "them before telling the person there is nobody available."
                 if not found else ""),
    }


@tool(name="search_pharmacy_availability",
      description="Which pharmacies stock a medication, and at what price.",
      parameters={
          "type": "object",
          "properties": {
              "medication": {"type": "string",
                             "description": "Medication name to look for."},
          },
          "required": ["medication"],
      },
      roles=PATIENT)
def search_pharmacy_availability(user, medication):
    from pharmacy import services as pharmacy

    matches = pharmacy.search_medications(medication, limit=3)
    if not matches:
        return {"ok": True, "medication": medication, "pharmacies": [],
                "note": "Roshada does not have that medication on file."}

    found = []
    for match in matches:
        for entry in pharmacy.pharmacies_with(match.id, quantity=1,
                                              include_out_of_stock=False):
            found.append({"medication": match.label,
                          "pharmacy": entry.get("name"),
                          "available": entry.get("available"),
                          "price": str(entry.get("price"))
                          if entry.get("price") is not None else None})
            if len(found) >= LIMIT:
                break

    return {"ok": True, "medication": medication, "pharmacies": found,
            "note": ("No pharmacy currently has it in stock." if not found
                     else "")}


# ---------------------------------------------------------------------------
# The patient's own record
# ---------------------------------------------------------------------------
@tool(name="get_patient_appointments",
      description="The signed-in patient's own appointments.",
      parameters={
          "type": "object",
          "properties": {
              "upcoming_only": {
                  "type": "boolean",
                  "description": "True for future appointments only."},
          },
      },
      roles=PATIENT)
def get_patient_appointments(user, upcoming_only=False):
    if upcoming_only:
        found = list(scheduling.upcoming_for_patient(user, limit=LIMIT))
    else:
        found = list(scheduling.list_patient_appointments(user)[:LIMIT])
    return {"ok": True, "count": len(found),
            "appointments": [_appointment_dict(a) for a in found],
            "note": "You have no appointments booked." if not found else ""}


@tool(name="get_patient_prescriptions",
      description="Prescriptions issued to the signed-in patient by their "
                  "doctors, with the medicines on each one.",
      roles=PATIENT)
def get_patient_prescriptions(user):
    from pharmacy import services as pharmacy

    found = list(pharmacy.patient_visible_prescriptions(user)[:LIMIT])
    prescriptions = []
    for prescription in found:
        prescriptions.append({
            "id": prescription.id,
            "issued": prescription.created_at.date().isoformat(),
            "doctor": (prescription.doctor.get_full_name()
                       or prescription.doctor.username),
            "status": prescription.get_status_display(),
            "diagnosis": prescription.diagnosis or "",
            "medicines": [
                {"name": item.medication.label if item.medication else item.name,
                 "dosage": item.dosage or "",
                 "instructions": item.instructions or ""}
                for item in prescription.items.all()],
        })
    return {"ok": True, "count": len(prescriptions),
            "prescriptions": prescriptions,
            "note": ("No doctor has issued you a prescription yet."
                     if not prescriptions else "")}


@tool(name="get_patient_radiology_reports",
      description="Radiology reports released to the signed-in patient. "
                  "Draft and unreleased reports are never included.",
      roles=PATIENT)
def get_patient_radiology_reports(user):
    from radiology import services as radiology

    found = list(radiology.patient_visible_reports(user)[:LIMIT])
    return {
        "ok": True, "count": len(found),
        "reports": [{
            "id": report.id,
            "released": (report.released_at or report.updated_at).date().isoformat(),
            "study": getattr(report.examination.appointment.service, "name", "")
                     or "Imaging",
            "impression": report.impression or "",
        } for report in found],
        "note": ("You have no released radiology reports." if not found else
                 "Discuss these with the doctor who ordered them."),
    }


@tool(name="get_patient_lab_results",
      description="Laboratory results for the signed-in patient.",
      roles=PATIENT)
def get_patient_lab_results(user):
    """Honest about a module Roshada does not have.

    Laboratory *appointments* exist; laboratory *results* have no producer in
    this platform yet. Returning an empty list without saying so would let the
    model tell a patient their results are clear.
    """
    return {
        "ok": True, "count": 0, "results": [],
        "note": "Roshada does not have a laboratory results module yet, so no "
                "lab results are stored here for anyone. This is not the same "
                "as the patient having no results — tell them Roshada cannot "
                "show lab results and they should ask the laboratory directly.",
    }


# ---------------------------------------------------------------------------
# Writes — previewed, confirmed, then executed
# ---------------------------------------------------------------------------
def _book_preview(provider_user_id=None, date="", time="", reason=""):
    try:
        provider = scheduling.resolve_provider(provider_id=provider_user_id)
        who = provider.get_full_name() or provider.username
    except Exception:                                       # noqa: BLE001
        who = "the provider"
    return f"Book an appointment with {who} on {date} at {time}."


@tool(name="book_appointment",
      description="Book an appointment for the signed-in patient. The first "
                  "call only proposes it — nothing is booked until you show "
                  "the person what you are about to do, they agree in their "
                  "next message, and you call it again with the same "
                  "arguments and confirm=true.",
      parameters={
          "type": "object",
          "properties": {
              "provider_user_id": {
                  "type": "integer",
                  "description": "Provider user id from search_doctors."},
              "date": {"type": "string", "description": "YYYY-MM-DD."},
              "time": {"type": "string", "description": "HH:MM, 24-hour."},
              "reason": {"type": "string",
                         "description": "Why they are coming, in their words."},
              "confirm": {"type": "boolean",
                          "description": "Only true after the person has "
                                         "agreed, in a later message."},
          },
          "required": ["provider_user_id", "date", "time"],
      },
      roles=PATIENT, writes=True)
def book_appointment(user, provider_user_id, date, time, reason=""):
    day = _date(date)
    # The model sends "10:00"; the engine takes a real time object, because it
    # combines the two into an aware period. Parsing here keeps that contract
    # intact instead of letting a string reach the scheduler.
    try:
        at = datetime.time.fromisoformat(str(time).strip())
    except (ValueError, TypeError):
        raise ToolError(f"'{time}' is not a time I can read. "
                        f"Use HH:MM on a 24-hour clock.") from None

    try:
        appointment = scheduling.create_appointment(
            user, day, at, reason=reason, provider_id=provider_user_id)
    except scheduling.DoctorNotFound:
        raise ToolError("There is no such provider in Roshada.") from None
    except scheduling.DoctorNotAvailable:
        raise ToolError("That provider is not accepting bookings.") from None
    except (scheduling.SlotUnavailable, availability.OutsideAvailability) as exc:
        raise ToolError(f"That time cannot be booked: {exc}") from None
    except ValueError as exc:
        raise ToolError(str(exc)) from None

    return {"ok": True, "executed": True,
            "appointment": _appointment_dict(appointment),
            "note": "The appointment is booked."}


book_appointment.preview = _book_preview


def _cancel_preview(appointment_id=None, reason=""):
    return f"Cancel appointment #{appointment_id}."


@tool(name="cancel_appointment",
      description="Cancel one of the signed-in patient's own appointments. "
                  "The first call only proposes it — nothing is cancelled "
                  "until the person agrees in their next message and you "
                  "call it again with the same arguments and confirm=true.",
      parameters={
          "type": "object",
          "properties": {
              "appointment_id": {
                  "type": "integer",
                  "description": "Id from get_patient_appointments."},
              "reason": {"type": "string", "description": "Why, if they said."},
              "confirm": {"type": "boolean",
                          "description": "Only true after the person has "
                                         "agreed, in a later message."},
          },
          "required": ["appointment_id"],
      },
      roles=PATIENT, writes=True)
def cancel_appointment(user, appointment_id, reason=""):
    try:
        appointment = scheduling.cancel_appointment(user, appointment_id,
                                                    reason=reason)
    except scheduling.AppointmentNotFound:
        # The service already refuses appointments that are not the caller's,
        # and reports them as "not found" so an id cannot be probed.
        raise ToolError("You have no appointment with that id.") from None
    except scheduling.InvalidTransition as exc:
        raise ToolError(str(exc)) from None

    return {"ok": True, "executed": True,
            "appointment": _appointment_dict(appointment),
            "note": "The appointment is cancelled."}


cancel_appointment.preview = _cancel_preview
