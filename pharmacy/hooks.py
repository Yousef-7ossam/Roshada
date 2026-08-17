"""How pharmacy attaches to the platform's shared services.

One registration, and it is a *read*: the pharmacy domain contributes figures to
the dashboard summary that every portal already fetches. Nothing here touches
the appointment engine — a pharmacy is not bookable, so there is no booking
callback to register and none is invented.

Registered from ``apps.ready()`` so the dependency runs pharmacy -> engine. The
dashboard module has no import of this one.
"""
from accounts import roles
from appointments.services import dashboard
from records import access
from records import timeline as records_timeline


@records_timeline.source
def pharmacy_timeline_entries(viewer, patient, limit):
    """Prescriptions, and — for the patient only — their dispensing history.

    Prescriptions come from ``patient_visible_prescriptions``, so a doctor's
    unissued draft is never fetched and therefore cannot surface here.

    Medication requests are shown to the patient and to nobody else. The
    pharmacy module does not route dispensing back to the prescriber, and an
    aggregation layer must not become the second route around that decision.
    """
    from . import services

    if not access.may_view(viewer, patient):
        return []

    entries = []
    for prescription in services.patient_visible_prescriptions(patient)[:limit]:
        items = list(prescription.items.all())
        first = items[0].medication.label if items else "Prescription"
        more = f" +{len(items) - 1} more" if len(items) > 1 else ""
        entries.append(records_timeline.Entry(
            type=records_timeline.PRESCRIPTION,
            # Issued, not created: a prescription enters the patient's history
            # when the doctor hands it over. Cancelled drafts that were never
            # issued fall back to their own timestamp rather than to "now".
            at=prescription.issued_at or prescription.updated_at,
            title=f"{first}{more}",
            source="pharmacy.Prescription",
            reference=prescription.id,
            status=prescription.status,
            status_label=prescription.get_status_display(),
            provider=("Dr. " + (prescription.doctor.get_full_name()
                                or prescription.doctor.username)
                      if prescription.doctor else ""),
            detail=prescription.diagnosis or "",
            extra={"medications": len(items)},
        ))

    if access.is_subject(viewer, patient):
        for request in services.patient_medication_requests(patient)[:limit]:
            items = list(request.items.all())
            first = items[0].medication.label if items else "Medication"
            more = f" +{len(items) - 1} more" if len(items) > 1 else ""
            entries.append(records_timeline.Entry(
                type=records_timeline.MEDICATION_ORDER,
                at=request.completed_at or request.created_at,
                title=f"{first}{more}",
                source="pharmacy.MedicationRequest",
                reference=request.id,
                status=request.status,
                status_label=request.get_status_display(),
                provider=_pharmacy_name(request.pharmacy),
                detail="",
            ))
    return entries


def _pharmacy_name(user):
    profile = getattr(user, "pharmacy_profile", None)
    if profile is not None and profile.name:
        return profile.name
    return user.get_full_name() or user.username


@dashboard.contribute
def pharmacy_dashboard_block(user, role):
    """The pharmacy figures each role's dashboard shows.

    For the pharmacy itself these replace placeholder tiles with real counts —
    which also means ``active_prescriptions``, a metric the patient dashboard
    has been honestly reporting as "not tracked" since it was written, finally
    becomes a number. It stops being listed as unsupported automatically,
    because that list is derived from which figures came back None.
    """
    from . import services

    if role == roles.PHARMACY:
        return {"stats": services.pharmacy_counts(user)}
    if role == roles.PATIENT:
        summary = services.patient_pharmacy_summary(user)
        return {
            # Promoted into stats so the existing tile finds it; the block is
            # also returned whole for the pharmacy panel on the dashboard.
            "stats": {"active_prescriptions": summary["active_prescriptions"]},
            "pharmacy": summary,
        }
    if role == roles.DOCTOR:
        return {"pharmacy": services.doctor_pharmacy_summary(user)}
    return {}
