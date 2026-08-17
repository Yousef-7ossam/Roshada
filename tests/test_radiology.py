"""The Radiology module.

The workflow tests walk the real chain — order → booking → examination →
files → report → patient — because each step's guarantee depends on the one
before it, and testing them in isolation would miss exactly the disagreements
that matter (an examination whose order never advanced, a released report on a
cancelled study).

The security half is deliberately larger than the happy path. Imaging is
patient medical data, so what is *refused* is the part worth proving.
"""
import datetime
import io

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient

from accounts import roles
from accounts.services import register_account
from appointments.models import AvailabilityRule, Service
from appointments.services import availability
from radiology import modalities
from radiology.models import (
    Examination, ImagingFile, ImagingOrder, RadiologyReport,
)

pytestmark = pytest.mark.django_db

PW = "Str0ng!Passw0rd"


@pytest.fixture(autouse=True)
def _no_throttling(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK,
                               "DEFAULT_THROTTLE_CLASSES": []}


@pytest.fixture(autouse=True)
def _isolated_media(settings, tmp_path):
    """Uploads go to a per-test directory, never the real MEDIA_ROOT."""
    settings.MEDIA_ROOT = str(tmp_path / "media")


@pytest.fixture
def client():
    return APIClient()


def png_bytes(size=(16, 16)):
    from PIL import Image
    buffer = io.BytesIO()
    Image.new("RGB", size, "black").save(buffer, format="PNG")
    return buffer.getvalue()


def png_upload(name="scan.png"):
    from django.core.files.uploadedfile import SimpleUploadedFile
    return SimpleUploadedFile(name, png_bytes(), content_type="image/png")


# ---------------------------------------------------------------------------
# Cast
# ---------------------------------------------------------------------------
def make(role, username, **extra):
    defaults = {
        roles.PATIENT: {"age": 40},
        roles.DOCTOR: {"specialization": "Neurology"},
        roles.RADIOLOGY: {"services": "MRI, CT"},
        roles.LABORATORY: {"services": "CBC"},
        roles.PHARMACY: {"services": "Dispensing"},
    }[role]
    user, _account, _profile, token = register_account(
        role, username=username, password=PW, name=username.replace("_", " ").title(),
        **{**defaults, **extra})
    return user, token.key


def future_date(days=2):
    return (timezone.localtime() + datetime.timedelta(days=days)).date()


def open_center(center, date, service, start="10:00", end="16:00"):
    return AvailabilityRule.objects.create(
        provider=center, service=service, date=date,
        start_time=datetime.time.fromisoformat(start),
        end_time=datetime.time.fromisoformat(end),
        slot_minutes=service.duration_minutes)


def imaging_service(center, name="MRI Brain", modality=modalities.MRI,
                    minutes=60, preparation=""):
    return Service.objects.create(provider=center, name=name,
                                  category=modality, duration_minutes=minutes,
                                  preparation=preparation)


def care_relationship(doctor, patient, days=1):
    """The platform's existing definition of "my patient": an appointment."""
    from appointments.models import Appointment
    start = availability.combine(future_date(days), datetime.time(9, 0))
    return Appointment.objects.create(provider=doctor, patient=patient,
                                      start_at=start,
                                      end_at=start + datetime.timedelta(minutes=30))


@pytest.fixture
def cast(client):
    """A doctor, a patient they treat, and a centre offering MRI."""
    patient, patient_token = make(roles.PATIENT, "pat_one")
    doctor, doctor_token = make(roles.DOCTOR, "doc_one")
    center, center_token = make(roles.RADIOLOGY, "centre_one")
    care_relationship(doctor, patient)
    service = imaging_service(center, preparation="Remove all metal objects.")
    date = future_date()
    open_center(center, date, service)
    return {
        "patient": patient, "patient_token": patient_token,
        "doctor": doctor, "doctor_token": doctor_token,
        "center": center, "center_token": center_token,
        "service": service, "date": date,
    }


def as_user(client, token):
    client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
    return client


def create_order(client, cast, study="MRI Brain", modality=modalities.MRI):
    as_user(client, cast["doctor_token"])
    res = client.post("/api/radiology/orders/",
                      {"patient_id": cast["patient"].id, "modality": modality,
                       "study_name": study,
                       "clinical_indication": "Persistent headache"},
                      format="json")
    assert res.status_code == 201, res.data
    return res.data


def book_order(client, cast, order_id, hhmm="10:00", expect=201):
    as_user(client, cast["patient_token"])
    res = client.post(f"/api/radiology/orders/{order_id}/book/",
                      {"service_id": cast["service"].id,
                       "date": cast["date"].isoformat(), "time": f"{hhmm}:00"},
                      format="json")
    assert res.status_code == expect, res.data
    return res


def advance(client, cast, examination_id, to_status, expect=200):
    as_user(client, cast["center_token"])
    res = client.post(f"/api/radiology/examinations/{examination_id}/status/",
                      {"status": to_status}, format="json")
    assert res.status_code == expect, res.data
    return res


# ---------------------------------------------------------------------------
# No duplicated architecture
# ---------------------------------------------------------------------------
class TestBuiltOnTheExistingArchitecture:
    def test_there_is_no_second_appointment_or_slot_model(self):
        """Radiology must use the unified engine, not shadow it."""
        from django.apps import apps
        names = {m.__name__ for m in apps.get_app_config("radiology").get_models()}
        assert names == {"ImagingOrder", "Examination", "ImagingFile",
                         "RadiologyReport"}
        for forbidden in ("RadiologyAppointment", "RadiologySlot",
                          "RadiologyCenter", "ImagingCenter", "ImagingService"):
            assert forbidden not in names, (
                f"{forbidden} duplicates something the platform already has")

    def test_a_centre_is_an_account_not_a_new_entity(self, cast):
        """The centre is a user with the radiology role + RadiologyProfile."""
        from accounts.models import RadiologyProfile
        assert RadiologyProfile.objects.filter(user=cast["center"]).exists()
        assert cast["center"].account.role == roles.RADIOLOGY

    def test_an_imaging_service_is_an_appointment_service(self, cast):
        assert isinstance(cast["service"], Service)
        assert cast["service"].category == modalities.MRI

    def test_booking_produces_an_ordinary_appointment(self, client, cast):
        order = create_order(client, cast)
        res = book_order(client, cast, order["id"])
        from appointments.models import Appointment
        appointment = Appointment.objects.get(pk=res.data["appointment_id"])
        assert appointment.provider_id == cast["center"].id
        assert appointment.service_id == cast["service"].id
        assert appointment.status == Appointment.SCHEDULED

    def test_radiology_capabilities_are_no_longer_planned(self):
        assert roles.RADIOLOGY_ORDERS not in roles.PLANNED_CAPABILITIES
        assert roles.RADIOLOGY_REPORTS not in roles.PLANNED_CAPABILITIES
        # Laboratory is still unbuilt and must stay declared. (Pharmacy left
        # this set when the Pharmacy module landed.)
        assert roles.LAB_ORDERS in roles.PLANNED_CAPABILITIES


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
class TestImagingOrders:
    def test_a_doctor_orders_a_study_for_their_own_patient(self, client, cast):
        order = create_order(client, cast)
        assert order["status"] == "ordered"
        assert order["modality_label"] == "MRI"
        assert order["is_self_requested"] is False
        assert order["patient"]["username"] == "pat_one"

    def test_a_doctor_cannot_order_for_a_patient_they_do_not_treat(self, client, cast):
        stranger, _ = make(roles.PATIENT, "stranger")
        as_user(client, cast["doctor_token"])
        res = client.post("/api/radiology/orders/",
                          {"patient_id": stranger.id, "modality": modalities.CT,
                           "study_name": "CT Chest"}, format="json")
        assert res.status_code == 403
        assert not ImagingOrder.objects.filter(patient=stranger).exists()

    @pytest.mark.parametrize("role,username", [
        (roles.PATIENT, "p_order"), (roles.RADIOLOGY, "r_order"),
        (roles.LABORATORY, "l_order")])
    def test_only_doctors_create_orders(self, client, cast, role, username):
        _user, token = make(role, username)
        as_user(client, token)
        res = client.post("/api/radiology/orders/",
                          {"patient_id": cast["patient"].id,
                           "modality": modalities.CT, "study_name": "CT"},
                          format="json")
        assert res.status_code == 403

    def test_an_unknown_modality_is_refused(self, client, cast):
        as_user(client, cast["doctor_token"])
        res = client.post("/api/radiology/orders/",
                          {"patient_id": cast["patient"].id,
                           "modality": "telepathy", "study_name": "?"},
                          format="json")
        assert res.status_code == 400

    def test_the_patient_sees_the_order_the_doctor_wrote(self, client, cast):
        create_order(client, cast)
        as_user(client, cast["patient_token"])
        res = client.get("/api/radiology/orders/")
        assert res.status_code == 200
        assert len(res.data) == 1
        assert res.data[0]["is_bookable"] is True

    def test_a_doctor_sees_only_their_own_orders(self, client, cast):
        create_order(client, cast)
        other_doctor, other_token = make(roles.DOCTOR, "doc_two")
        care_relationship(other_doctor, cast["patient"], days=3)

        as_user(client, other_token)
        assert client.get("/api/radiology/orders/").data == [], (
            "a doctor read another doctor's referral")

    def test_a_patient_cannot_read_another_patients_order(self, client, cast):
        order = create_order(client, cast)
        _other, other_token = make(roles.PATIENT, "pat_two")
        as_user(client, other_token)
        # 404, not 403: a different answer would confirm the order exists.
        assert client.get(
            f"/api/radiology/orders/{order['id']}/").status_code == 404


# ---------------------------------------------------------------------------
# Booking through the unified engine
# ---------------------------------------------------------------------------
class TestBooking:
    def test_booking_an_order_does_not_create_a_second_order(self, client, cast):
        order = create_order(client, cast)
        assert ImagingOrder.objects.count() == 1
        book_order(client, cast, order["id"])
        assert ImagingOrder.objects.count() == 1, "booking duplicated the order"
        assert ImagingOrder.objects.get().status == ImagingOrder.SCHEDULED

    def test_booking_creates_exactly_one_scheduled_examination(self, client, cast):
        order = create_order(client, cast)
        book_order(client, cast, order["id"])
        examination = Examination.objects.get()
        assert examination.status == Examination.SCHEDULED
        assert examination.order_id == order["id"]
        assert examination.patient == cast["patient"]
        assert examination.center == cast["center"]

    def test_a_service_of_the_wrong_modality_cannot_fulfil_the_order(
            self, client, cast):
        """An X-ray booking must not satisfy an MRI referral."""
        xray = imaging_service(cast["center"], "Chest X-Ray", modalities.XRAY, 15)
        open_center(cast["center"], cast["date"], xray, "10:00", "12:00")
        order = create_order(client, cast)

        as_user(client, cast["patient_token"])
        res = client.post(f"/api/radiology/orders/{order['id']}/book/",
                          {"service_id": xray.id,
                           "date": cast["date"].isoformat(), "time": "10:00:00"},
                          format="json")
        assert res.status_code == 400
        assert "X-Ray" in res.data["error"] or "MRI" in res.data["error"]

    def test_a_non_radiology_service_cannot_fulfil_an_imaging_order(
            self, client, cast):
        lab, _ = make(roles.LABORATORY, "lab_one")
        lab_service = Service.objects.create(provider=lab, name="MRI Brain",
                                             category=modalities.MRI)
        open_center(lab, cast["date"], lab_service)
        order = create_order(client, cast)

        as_user(client, cast["patient_token"])
        res = client.post(f"/api/radiology/orders/{order['id']}/book/",
                          {"service_id": lab_service.id,
                           "date": cast["date"].isoformat(), "time": "10:00:00"},
                          format="json")
        assert res.status_code == 400

    def test_a_patient_cannot_book_another_patients_order(self, client, cast):
        order = create_order(client, cast)
        _other, other_token = make(roles.PATIENT, "pat_two")
        as_user(client, other_token)
        res = client.post(f"/api/radiology/orders/{order['id']}/book/",
                          {"service_id": cast["service"].id,
                           "date": cast["date"].isoformat(), "time": "10:00:00"},
                          format="json")
        assert res.status_code == 404

    def test_an_order_cannot_be_booked_twice(self, client, cast):
        order = create_order(client, cast)
        book_order(client, cast, order["id"])
        book_order(client, cast, order["id"], hhmm="11:00", expect=400)

    def test_double_booking_is_still_prevented_by_the_engine(self, client, cast):
        """Radiology inherits the engine's guarantee rather than restating it."""
        first = create_order(client, cast)
        book_order(client, cast, first["id"], "10:00")

        other_patient, other_token = make(roles.PATIENT, "pat_two")
        care_relationship(cast["doctor"], other_patient, days=4)
        as_user(client, cast["doctor_token"])
        second = client.post("/api/radiology/orders/",
                             {"patient_id": other_patient.id,
                              "modality": modalities.MRI,
                              "study_name": "MRI Brain"}, format="json").data

        as_user(client, other_token)
        res = client.post(f"/api/radiology/orders/{second['id']}/book/",
                          {"service_id": cast["service"].id,
                           "date": cast["date"].isoformat(), "time": "10:00:00"},
                          format="json")
        assert res.status_code == 409

    def test_cancelling_the_appointment_frees_the_order_and_the_slot(
            self, client, cast):
        order = create_order(client, cast)
        booked = book_order(client, cast, order["id"])
        appointment_id = booked.data["appointment_id"]

        as_user(client, cast["patient_token"])
        assert client.post(f"/api/appointments/{appointment_id}/cancel/",
                           {"reason": "unwell"},
                           format="json").status_code == 200

        # The hook cascaded: study cancelled, order bookable again.
        assert Examination.objects.get().status == Examination.CANCELLED
        assert ImagingOrder.objects.get().status == ImagingOrder.ORDERED
        # And the slot really is free — re-book it.
        book_order(client, cast, order["id"], "10:00")

    def test_a_patient_may_book_imaging_without_a_referral(self, client, cast):
        """Section 10: self-service is allowed, but must not fake a doctor."""
        as_user(client, cast["patient_token"])
        res = client.post("/api/radiology/book/",
                          {"service_id": cast["service"].id,
                           "date": cast["date"].isoformat(), "time": "10:00:00",
                           "study_name": "MRI Brain (self-requested)"},
                          format="json")
        assert res.status_code == 201
        order = ImagingOrder.objects.get()
        assert order.doctor_id is None
        assert order.is_self_requested is True
        assert res.data["order"]["is_self_requested"] is True


# ---------------------------------------------------------------------------
# Examination lifecycle
# ---------------------------------------------------------------------------
class TestExaminationWorkflow:
    def test_the_full_sequence_advances_the_order_with_it(self, client, cast):
        order = create_order(client, cast)
        book_order(client, cast, order["id"])
        examination = Examination.objects.get()

        advance(client, cast, examination.id, Examination.CHECKED_IN)
        assert Examination.objects.get().checked_in_at is not None

        advance(client, cast, examination.id, Examination.IN_PROGRESS)
        assert ImagingOrder.objects.get().status == ImagingOrder.IN_PROGRESS

        advance(client, cast, examination.id, Examination.COMPLETED)
        examination.refresh_from_db()
        assert examination.completed_at is not None
        assert examination.performed_by_id == cast["center"].id
        assert ImagingOrder.objects.get().status == ImagingOrder.REPORT_PENDING
        # Completing the study closes the booking through the engine's own
        # service — one writer for appointment outcomes.
        assert examination.appointment.status == "completed"

    @pytest.mark.parametrize("to_status", ["in_progress", "completed"])
    def test_steps_cannot_be_skipped(self, client, cast, to_status):
        order = create_order(client, cast)
        book_order(client, cast, order["id"])
        examination = Examination.objects.get()
        advance(client, cast, examination.id, to_status, expect=409)
        assert Examination.objects.get().status == Examination.SCHEDULED

    def test_a_completed_examination_is_final(self, client, cast):
        order = create_order(client, cast)
        book_order(client, cast, order["id"])
        examination = Examination.objects.get()
        for step in (Examination.CHECKED_IN, Examination.IN_PROGRESS,
                     Examination.COMPLETED):
            advance(client, cast, examination.id, step)
        advance(client, cast, examination.id, Examination.IN_PROGRESS, expect=409)

    def test_an_arbitrary_status_from_the_request_is_refused(self, client, cast):
        order = create_order(client, cast)
        book_order(client, cast, order["id"])
        examination = Examination.objects.get()
        advance(client, cast, examination.id, "reported", expect=409)
        advance(client, cast, examination.id, "definitely_done", expect=409)

    def test_another_centre_cannot_touch_this_examination(self, client, cast):
        order = create_order(client, cast)
        book_order(client, cast, order["id"])
        examination = Examination.objects.get()

        _other_centre, other_token = make(roles.RADIOLOGY, "centre_two")
        as_user(client, other_token)
        res = client.post(
            f"/api/radiology/examinations/{examination.id}/status/",
            {"status": Examination.CHECKED_IN}, format="json")
        assert res.status_code == 404
        assert Examination.objects.get().status == Examination.SCHEDULED

    @pytest.mark.parametrize("who", ["patient_token", "doctor_token"])
    def test_only_the_centre_advances_an_examination(self, client, cast, who):
        order = create_order(client, cast)
        book_order(client, cast, order["id"])
        examination = Examination.objects.get()
        as_user(client, cast[who])
        assert client.post(
            f"/api/radiology/examinations/{examination.id}/status/",
            {"status": Examination.CHECKED_IN},
            format="json").status_code == 403

    def test_a_centre_sees_only_its_own_examinations(self, client, cast):
        order = create_order(client, cast)
        book_order(client, cast, order["id"])
        _other_centre, other_token = make(roles.RADIOLOGY, "centre_two")

        as_user(client, other_token)
        assert client.get("/api/radiology/examinations/").data == []
        as_user(client, cast["center_token"])
        assert len(client.get("/api/radiology/examinations/").data) == 1


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def completed_examination(client, cast):
    order = create_order(client, cast)
    book_order(client, cast, order["id"])
    examination = Examination.objects.get()
    for step in (Examination.CHECKED_IN, Examination.IN_PROGRESS,
                 Examination.COMPLETED):
        advance(client, cast, examination.id, step)
    return examination


def write_report(client, cast, examination_id, findings="No acute finding.",
                 impression="Normal study."):
    as_user(client, cast["center_token"])
    res = client.post(f"/api/radiology/examinations/{examination_id}/report/",
                      {"findings": findings, "impression": impression},
                      format="json")
    assert res.status_code == 200, res.data
    return res.data


def move_report(client, cast, report_id, to_status, expect=200, token=None):
    as_user(client, token or cast["center_token"])
    res = client.post(f"/api/radiology/reports/{report_id}/status/",
                      {"status": to_status}, format="json")
    assert res.status_code == expect, res.data
    return res


class TestReportWorkflow:
    def test_a_report_needs_a_completed_examination(self, client, cast):
        order = create_order(client, cast)
        book_order(client, cast, order["id"])
        examination = Examination.objects.get()
        as_user(client, cast["center_token"])
        res = client.post(
            f"/api/radiology/examinations/{examination.id}/report/",
            {"findings": "premature"}, format="json")
        assert res.status_code == 409

    def test_draft_to_review_to_verified_to_released(self, client, cast):
        examination = completed_examination(client, cast)
        report = write_report(client, cast, examination.id)
        assert report["status"] == "draft"

        move_report(client, cast, report["id"], RadiologyReport.PENDING_REVIEW)
        verified = move_report(client, cast, report["id"],
                               RadiologyReport.VERIFIED)
        assert verified.data["verified_at"] is not None
        assert verified.data["verified_by"]["username"] == "centre_one"

        released = move_report(client, cast, report["id"],
                               RadiologyReport.RELEASED)
        assert released.data["released_at"] is not None
        assert ImagingOrder.objects.get().status == ImagingOrder.REPORTED

    def test_a_report_cannot_be_released_without_verification(self, client, cast):
        examination = completed_examination(client, cast)
        report = write_report(client, cast, examination.id)
        move_report(client, cast, report["id"], RadiologyReport.RELEASED,
                    expect=409)

    def test_a_released_report_is_final(self, client, cast):
        examination = completed_examination(client, cast)
        report = write_report(client, cast, examination.id)
        for step in (RadiologyReport.PENDING_REVIEW, RadiologyReport.VERIFIED,
                     RadiologyReport.RELEASED):
            move_report(client, cast, report["id"], step)
        move_report(client, cast, report["id"], RadiologyReport.DRAFT, expect=409)

    def test_a_verified_report_can_no_longer_be_edited(self, client, cast):
        examination = completed_examination(client, cast)
        report = write_report(client, cast, examination.id)
        move_report(client, cast, report["id"], RadiologyReport.PENDING_REVIEW)
        move_report(client, cast, report["id"], RadiologyReport.VERIFIED)

        as_user(client, cast["center_token"])
        res = client.post(
            f"/api/radiology/examinations/{examination.id}/report/",
            {"findings": "rewritten after verification"}, format="json")
        assert res.status_code == 409

    # ---- Who may read a report ----
    def test_a_patient_cannot_see_a_report_before_release(self, client, cast):
        examination = completed_examination(client, cast)
        report = write_report(client, cast, examination.id,
                              findings="SUSPICIOUS MASS")

        as_user(client, cast["patient_token"])
        assert client.get("/api/radiology/reports/").data == []
        payload = client.get(
            f"/api/radiology/examinations/{examination.id}/").data
        # The patient learns a report is being prepared, not what it says.
        assert payload["report"]["status"] == "pending"
        assert "SUSPICIOUS" not in str(payload)

        for step in (RadiologyReport.PENDING_REVIEW, RadiologyReport.VERIFIED):
            move_report(client, cast, report["id"], step)
        as_user(client, cast["patient_token"])
        assert client.get("/api/radiology/reports/").data == [], (
            "a verified-but-unreleased report reached the patient")

    def test_the_patient_reads_it_once_released(self, client, cast):
        examination = completed_examination(client, cast)
        report = write_report(client, cast, examination.id)
        for step in (RadiologyReport.PENDING_REVIEW, RadiologyReport.VERIFIED,
                     RadiologyReport.RELEASED):
            move_report(client, cast, report["id"], step)

        as_user(client, cast["patient_token"])
        reports = client.get("/api/radiology/reports/").data
        assert len(reports) == 1
        assert reports[0]["impression"] == "Normal study."

    def test_the_ordering_doctor_reads_a_released_report(self, client, cast):
        examination = completed_examination(client, cast)
        report = write_report(client, cast, examination.id)
        as_user(client, cast["doctor_token"])
        assert client.get("/api/radiology/reports/").data == []

        for step in (RadiologyReport.PENDING_REVIEW, RadiologyReport.VERIFIED,
                     RadiologyReport.RELEASED):
            move_report(client, cast, report["id"], step)
        as_user(client, cast["doctor_token"])
        assert len(client.get("/api/radiology/reports/").data) == 1

    def test_an_unrelated_doctor_reads_nothing(self, client, cast):
        examination = completed_examination(client, cast)
        report = write_report(client, cast, examination.id)
        for step in (RadiologyReport.PENDING_REVIEW, RadiologyReport.VERIFIED,
                     RadiologyReport.RELEASED):
            move_report(client, cast, report["id"], step)

        _other, other_token = make(roles.DOCTOR, "doc_three")
        care_relationship(_other, cast["patient"], days=5)
        as_user(client, other_token)
        assert client.get("/api/radiology/reports/").data == [], (
            "a doctor who did not order the study read its report")

    def test_another_patient_reads_nothing(self, client, cast):
        examination = completed_examination(client, cast)
        report = write_report(client, cast, examination.id)
        for step in (RadiologyReport.PENDING_REVIEW, RadiologyReport.VERIFIED,
                     RadiologyReport.RELEASED):
            move_report(client, cast, report["id"], step)

        _other, other_token = make(roles.PATIENT, "pat_two")
        as_user(client, other_token)
        assert client.get("/api/radiology/reports/").data == []

    @pytest.mark.parametrize("who", ["patient_token", "doctor_token"])
    def test_nobody_outside_the_centre_may_verify(self, client, cast, who):
        examination = completed_examination(client, cast)
        report = write_report(client, cast, examination.id)
        move_report(client, cast, report["id"], RadiologyReport.PENDING_REVIEW)
        move_report(client, cast, report["id"], RadiologyReport.VERIFIED,
                    expect=403, token=cast[who])
        assert RadiologyReport.objects.get().status == "pending_review"

    def test_another_centre_may_not_verify_or_edit(self, client, cast):
        examination = completed_examination(client, cast)
        report = write_report(client, cast, examination.id)
        _other_centre, other_token = make(roles.RADIOLOGY, "centre_two")

        move_report(client, cast, report["id"], RadiologyReport.PENDING_REVIEW)
        move_report(client, cast, report["id"], RadiologyReport.VERIFIED,
                    expect=404, token=other_token)
        as_user(client, other_token)
        assert client.post(
            f"/api/radiology/examinations/{examination.id}/report/",
            {"findings": "hijacked"}, format="json").status_code == 404
        assert RadiologyReport.objects.get().findings == "No acute finding."


# ---------------------------------------------------------------------------
# Imaging files
# ---------------------------------------------------------------------------
class TestImagingFiles:
    def test_the_centre_attaches_a_file_and_metadata_is_recorded(
            self, client, cast):
        examination = completed_examination(client, cast)
        as_user(client, cast["center_token"])
        res = client.post(
            f"/api/radiology/examinations/{examination.id}/files/",
            {"file": png_upload("brain.png"), "description": "Axial T1"},
            format="multipart")
        assert res.status_code == 201, res.data
        stored = ImagingFile.objects.get()
        assert stored.original_name == "brain.png"
        assert stored.size_bytes > 0
        # DICOM readiness: the modality code is filled from the service.
        assert stored.modality_code == "MR"

    def test_the_bytes_are_on_disk_not_in_the_database(self, client, cast,
                                                       settings):
        import os
        examination = completed_examination(client, cast)
        as_user(client, cast["center_token"])
        client.post(f"/api/radiology/examinations/{examination.id}/files/",
                    {"file": png_upload()}, format="multipart")
        stored = ImagingFile.objects.get()
        assert stored.file.name.startswith("imaging/examination_")
        assert os.path.exists(os.path.join(settings.MEDIA_ROOT,
                                           stored.file.name))

    def test_the_metadata_payload_carries_no_url(self, client, cast):
        """A URL in the payload would be a way around the download check."""
        examination = completed_examination(client, cast)
        as_user(client, cast["center_token"])
        res = client.post(
            f"/api/radiology/examinations/{examination.id}/files/",
            {"file": png_upload()}, format="multipart")
        assert "url" not in res.data and "file" not in res.data

    def test_media_is_not_served_statically(self):
        """The only route to the bytes must be the authorization-checking view."""
        from django.urls import resolve, Resolver404
        with pytest.raises(Resolver404):
            resolve("/media/imaging/examination_1/scan.png")

    def test_the_owner_downloads_their_own_image(self, client, cast):
        examination = completed_examination(client, cast)
        as_user(client, cast["center_token"])
        created = client.post(
            f"/api/radiology/examinations/{examination.id}/files/",
            {"file": png_upload()}, format="multipart").data

        as_user(client, cast["patient_token"])
        res = client.get(f"/api/radiology/files/{created['id']}/download/")
        assert res.status_code == 200
        assert res["Content-Disposition"].startswith("attachment")

    def test_the_ordering_doctor_downloads_it(self, client, cast):
        examination = completed_examination(client, cast)
        as_user(client, cast["center_token"])
        created = client.post(
            f"/api/radiology/examinations/{examination.id}/files/",
            {"file": png_upload()}, format="multipart").data
        as_user(client, cast["doctor_token"])
        assert client.get(
            f"/api/radiology/files/{created['id']}/download/").status_code == 200

    def test_another_patient_cannot_download_it(self, client, cast):
        examination = completed_examination(client, cast)
        as_user(client, cast["center_token"])
        created = client.post(
            f"/api/radiology/examinations/{examination.id}/files/",
            {"file": png_upload()}, format="multipart").data

        _other, other_token = make(roles.PATIENT, "pat_two")
        as_user(client, other_token)
        assert client.get(
            f"/api/radiology/files/{created['id']}/download/").status_code == 404

    def test_another_centre_cannot_download_it(self, client, cast):
        examination = completed_examination(client, cast)
        as_user(client, cast["center_token"])
        created = client.post(
            f"/api/radiology/examinations/{examination.id}/files/",
            {"file": png_upload()}, format="multipart").data

        _other_centre, other_token = make(roles.RADIOLOGY, "centre_two")
        as_user(client, other_token)
        assert client.get(
            f"/api/radiology/files/{created['id']}/download/").status_code == 404

    def test_an_unrelated_doctor_cannot_download_it(self, client, cast):
        examination = completed_examination(client, cast)
        as_user(client, cast["center_token"])
        created = client.post(
            f"/api/radiology/examinations/{examination.id}/files/",
            {"file": png_upload()}, format="multipart").data

        _other, other_token = make(roles.DOCTOR, "doc_three")
        care_relationship(_other, cast["patient"], days=5)
        as_user(client, other_token)
        assert client.get(
            f"/api/radiology/files/{created['id']}/download/").status_code == 404

    def test_an_anonymous_caller_cannot_download_it(self, client, cast):
        examination = completed_examination(client, cast)
        as_user(client, cast["center_token"])
        created = client.post(
            f"/api/radiology/examinations/{examination.id}/files/",
            {"file": png_upload()}, format="multipart").data
        client.credentials()
        assert client.get(
            f"/api/radiology/files/{created['id']}/download/").status_code == 401

    def test_another_centre_cannot_attach_to_this_examination(self, client, cast):
        examination = completed_examination(client, cast)
        _other_centre, other_token = make(roles.RADIOLOGY, "centre_two")
        as_user(client, other_token)
        res = client.post(
            f"/api/radiology/examinations/{examination.id}/files/",
            {"file": png_upload()}, format="multipart")
        assert res.status_code == 404
        assert ImagingFile.objects.count() == 0

    def test_a_patient_cannot_attach_files(self, client, cast):
        examination = completed_examination(client, cast)
        as_user(client, cast["patient_token"])
        assert client.post(
            f"/api/radiology/examinations/{examination.id}/files/",
            {"file": png_upload()}, format="multipart").status_code == 403

    def test_a_non_image_upload_is_refused(self, client, cast):
        """Reuses the platform's existing upload validation."""
        from django.core.files.uploadedfile import SimpleUploadedFile
        examination = completed_examination(client, cast)
        as_user(client, cast["center_token"])
        res = client.post(
            f"/api/radiology/examinations/{examination.id}/files/",
            {"file": SimpleUploadedFile("payload.php", b"<?php ?>",
                                        content_type="image/png")},
            format="multipart")
        assert res.status_code == 400
        assert ImagingFile.objects.count() == 0


# ---------------------------------------------------------------------------
# Discovery and dashboards
# ---------------------------------------------------------------------------
class TestDiscoveryAndDashboards:
    def test_centres_are_listed_only_for_modalities_they_offer(self, client, cast):
        ultrasound_centre, _ = make(roles.RADIOLOGY, "us_centre")
        imaging_service(ultrasound_centre, "Abdominal Ultrasound",
                        modalities.ULTRASOUND, 30)

        as_user(client, cast["patient_token"])
        mri = client.get("/api/radiology/centers/",
                         {"modality": modalities.MRI}).data
        assert {c["id"] for c in mri} == {cast["center"].id}

        us = client.get("/api/radiology/centers/",
                        {"modality": modalities.ULTRASOUND}).data
        assert {c["id"] for c in us} == {ultrasound_centre.id}

    def test_preparation_instructions_come_from_the_centre(self, client, cast):
        """Not hardcoded: whatever this centre configured is what shows."""
        as_user(client, cast["patient_token"])
        centers = client.get("/api/radiology/centers/",
                             {"modality": modalities.MRI}).data
        service = centers[0]["services"][0]
        assert service["preparation"] == "Remove all metal objects."

    def test_an_unknown_modality_filter_is_refused(self, client, cast):
        as_user(client, cast["patient_token"])
        assert client.get("/api/radiology/centers/",
                          {"modality": "vibes"}).status_code == 400

    def test_the_centre_dashboard_reports_real_counts(self, client, cast):
        examination = completed_examination(client, cast)
        write_report(client, cast, examination.id)

        as_user(client, cast["center_token"])
        summary = client.get("/api/dashboard/summary/").data
        stats = summary["stats"]
        assert stats["reports_pending_review"] == 1
        assert stats["reports_released"] == 0
        assert stats["awaiting_report"] == 0     # a draft exists for it

    def test_the_patient_dashboard_counts_open_orders(self, client, cast):
        create_order(client, cast)
        as_user(client, cast["patient_token"])
        summary = client.get("/api/dashboard/summary/").data
        assert summary["radiology"]["awaiting_booking"] == 1
        assert summary["radiology"]["released_reports"] == 0

    def test_the_doctor_dashboard_counts_their_orders(self, client, cast):
        create_order(client, cast)
        as_user(client, cast["doctor_token"])
        summary = client.get("/api/dashboard/summary/").data
        assert summary["radiology"]["imaging_orders"] == 1
        assert summary["radiology"]["imaging_reports_ready"] == 0

    def test_listing_examinations_does_not_scale_with_their_number(
            self, client, cast, django_assert_max_num_queries):
        """Serialising an examination reaches the provider's profile, the order,
        the report and the files. Without the joins that was five queries per
        row; the count must stay flat as studies accumulate."""
        for hour in ("10:00", "11:00", "12:00", "13:00"):
            order = create_order(client, cast)
            book_order(client, cast, order["id"], hhmm=hour)
        assert Examination.objects.count() == 4

        as_user(client, cast["center_token"])
        with django_assert_max_num_queries(12):
            res = client.get("/api/radiology/examinations/")
        assert len(res.data) == 4

    def test_a_lab_gets_no_radiology_block(self, client, cast):
        """Radiology must not leak into a laboratory's dashboard."""
        _lab, lab_token = make(roles.LABORATORY, "lab_dash")
        as_user(client, lab_token)
        summary = client.get("/api/dashboard/summary/").data
        assert "reports_pending_review" not in summary["stats"]


# ---------------------------------------------------------------------------
# Endpoint-level role guards
# ---------------------------------------------------------------------------
class TestEndpointGuards:
    @pytest.mark.parametrize("path", [
        "/api/radiology/orders/", "/api/radiology/examinations/",
        "/api/radiology/reports/", "/api/radiology/centers/",
        "/api/radiology/modalities/"])
    def test_every_endpoint_requires_authentication(self, client, path):
        assert client.get(path).status_code == 401

    def test_a_pharmacy_sees_nothing_and_can_do_nothing(self, client, cast):
        _pharmacy, token = make(roles.PHARMACY, "pharm_rad", services="")
        as_user(client, token)
        assert client.get("/api/radiology/orders/").data == []
        assert client.get("/api/radiology/examinations/").data == []
        assert client.get("/api/radiology/reports/").data == []

    def test_an_admin_does_not_read_clinical_reports(self, client, cast):
        """Administering the platform is not a reason to read a patient's
        radiology report."""
        examination = completed_examination(client, cast)
        report = write_report(client, cast, examination.id)
        for step in (RadiologyReport.PENDING_REVIEW, RadiologyReport.VERIFIED,
                     RadiologyReport.RELEASED):
            move_report(client, cast, report["id"], step)

        admin = User.objects.create_superuser("rad_admin", "a@example.com", PW)
        from accounts.models import UserAccount
        UserAccount.objects.create(user=admin, role=roles.ADMIN)
        from rest_framework.authtoken.models import Token
        as_user(client, Token.objects.create(user=admin).key)
        assert client.get("/api/radiology/reports/").data == []
