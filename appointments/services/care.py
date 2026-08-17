"""The care relationship — who is whose patient.

One rule, in one place: **a doctor may read a patient's medical data only when a
care relationship exists**, and an appointment between them is what establishes
one. Being a doctor is not by itself permission to read an arbitrary patient's
record.

This rule gates prescribing (:mod:`pharmacy.services`), imaging orders
(:mod:`radiology.services`), the unified medical record (:mod:`records.access`)
and doctor↔patient messaging (:mod:`comms.messaging`). It used to live in the
screening module because screening history was the first thing it protected;
when that feature was removed the rule had to come with it, not go with it — a
platform-wide authorization rule is not a detail of the feature that happened to
need it first.

It sits below every clinical module and imports none of them, which is what lets
all four consumers share one definition instead of four that drift.
"""
from django.contrib.auth.models import User

from ..models import Appointment


class PatientNotFound(Exception):
    """No such patient."""


class NotYourPatient(Exception):
    """The doctor has no care relationship with this patient."""


def treats_patient(doctor_user, patient):
    """True when the doctor has any appointment with this patient.

    Any appointment, in any state: a cancelled visit still means these two
    people were in a clinical relationship, and a doctor who saw a patient last
    year has a legitimate reason to read what they prescribed.
    """
    return Appointment.objects.filter(
        provider=doctor_user, patient=patient).exists()


def find_patient(patient_id):
    """Look up a patient by id, or raise :class:`PatientNotFound`."""
    try:
        return User.objects.get(pk=patient_id)
    except (User.DoesNotExist, ValueError, TypeError):
        raise PatientNotFound() from None


def require_patient_of(doctor_user, patient_id):
    """The patient, if this doctor treats them. Raises otherwise.

    The two failures stay distinct — "no such patient" and "not yours" — because
    the callers answer them with different status codes.
    """
    patient = find_patient(patient_id)
    if not treats_patient(doctor_user, patient):
        raise NotYourPatient()
    return patient


def patients_of(doctor_user):
    """Every patient this doctor has a care relationship with."""
    return User.objects.filter(
        id__in=Appointment.objects.filter(provider=doctor_user)
        .values_list("patient_id", flat=True).distinct())
