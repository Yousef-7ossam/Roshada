"""The Unified Medical Record.

The record is an aggregation layer over modules that already enforce their own
rules, so the tests that matter most are the ones proving it did not become a
way *around* those rules. Two in particular:

* **The release gate.** A draft radiology report and an unissued prescription
  must be absent from the timeline, and must appear the moment the owning
  module releases them — with no change on the records side. That is section
  21/22/23 made structural rather than promised.
* **No duplication.** The record stores no clinical data, so a source record
  edited after the fact is reflected immediately. A test edits an impression
  and reads the change back through the timeline.
"""
import datetime

import pytest
from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework.test import APIClient

from accounts import roles
from accounts.services import register_account
from appointments.models import Appointment, Service
from appointments.services import availability
from pharmacy import dosage_forms
from pharmacy.models import Medication, PharmacyInventory, Prescription
from radiology import modalities
from radiology.models import Examination, ImagingOrder, RadiologyReport
from records import timeline
from records.models import MedicalRecord

pytestmark = pytest.mark.django_db

PW = "Str0ng!Passw0rd"


@pytest.fixture(autouse=True)
def _no_throttling(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK,
                               "DEFAULT_THROTTLE_CLASSES": []}


@pytest.fixture
def client():
    return APIClient()


# ---------------------------------------------------------------------------
# Cast
# ---------------------------------------------------------------------------
def make(role, username, **extra):
    defaults = {
        roles.PATIENT: {"age": 44},
        roles.DOCTOR: {"specialization": "Internal Medicine"},
        roles.RADIOLOGY: {"services": "MRI"},
        roles.LABORATORY: {"services": "CBC"},
        roles.PHARMACY: {"services": "Dispensing"},
    }[role]
    user, _account, _profile, token = register_account(
        role, username=username, password=PW,
        name=username.replace("_", " ").title(), **{**defaults, **extra})
    return user, token.key


def as_user(client, token):
    client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
    return client


def appointment(provider, patient, days=-3, minutes=30,
                status=Appointment.COMPLETED, service=None):
    """A real booking through the engine's own model, at a real instant."""
    start = availability.combine(
        (timezone.localtime() + datetime.timedelta(days=days)).date(),
        datetime.time(9, 0))
    return Appointment.objects.create(
        provider=provider, patient=patient, start_at=start,
        end_at=start + datetime.timedelta(minutes=minutes),
        status=status, service=service, reason="Follow-up")


@pytest.fixture
def cast():
    """A patient, the doctor who treats them, a centre and a pharmacy."""
    patient, patient_token = make(roles.PATIENT, "mr_patient")
    doctor, doctor_token = make(roles.DOCTOR, "mr_doctor")
    center, center_token = make(roles.RADIOLOGY, "mr_centre")
    pharmacy, pharmacy_token = make(roles.PHARMACY, "mr_pharmacy")
    visit = appointment(doctor, patient)
    return {
        "patient": patient, "patient_token": patient_token,
        "doctor": doctor, "doctor_token": doctor_token,
        "center": center, "center_token": center_token,
        "pharmacy": pharmacy, "pharmacy_token": pharmacy_token,
        "visit": visit,
    }


def imaging_study(cast, released=False, impression="Normal study."):
    """A complete imaging chain, ending in a report at the requested state."""
    service = Service.objects.create(provider=cast["center"], name="MRI Brain",
                                     category=modalities.MRI,
                                     duration_minutes=60)
    booking = appointment(cast["center"], cast["patient"], days=-2,
                          minutes=60, service=service)
    order = ImagingOrder.objects.create(
        patient=cast["patient"], doctor=cast["doctor"], modality=modalities.MRI,
        study_name="MRI Brain", clinical_indication="Headache",
        status=ImagingOrder.REPORT_PENDING)
    examination = Examination.objects.create(
        appointment=booking, order=order, status=Examination.COMPLETED)
    report = RadiologyReport.objects.create(
        examination=examination, author=cast["center"],
        findings="No acute abnormality.", impression=impression,
        status=RadiologyReport.DRAFT)
    if released:
        release(report, cast["center"])
    return order, examination, report


def release(report, center):
    """Move a report through the module's own workflow to released."""
    from radiology import services as radiology_services
    for step in (RadiologyReport.PENDING_REVIEW, RadiologyReport.VERIFIED,
                 RadiologyReport.RELEASED):
        radiology_services.transition_report(center, report.id, step)
    report.refresh_from_db()
    return report


def prescription(cast, issue=True, medication_name="Amoxicillin"):
    from pharmacy import services as pharmacy_services
    medication, _ = Medication.objects.get_or_create(
        name=medication_name, strength="500 mg", form=dosage_forms.CAPSULE,
        defaults={"generic_name": medication_name})
    return pharmacy_services.create_prescription(
        cast["doctor"], cast["patient"].id,
        [{"medication_id": medication.id, "dosage": "1 capsule",
          "frequency": "3 times/day", "duration": "7 days", "quantity": 21}],
        diagnosis="Throat infection", issue=issue)


def types_in(payload):
    return [entry["type"] for entry in payload["results"]]


def titles_in(payload):
    return [entry["title"] for entry in payload["results"]]


# ---------------------------------------------------------------------------
# The record aggregates; it does not duplicate
# ---------------------------------------------------------------------------
class TestAggregationNotDuplication:
    def test_the_app_owns_exactly_one_model(self):
        """Everything else is a reference into a module that already exists."""
        from django.apps import apps
        names = {m.__name__ for m in apps.get_app_config("records").get_models()}
        assert names == {"MedicalRecord"}
        for forbidden in ("MedicalRecordLabResult", "MedicalRecordAppointment",
                          "MedicalRecordPrescription", "Consultation",
                          "LabResult", "RadiologyReport", "Prescription",
                          "Appointment", "Patient", "Doctor"):
            assert forbidden not in names, (
                f"{forbidden} duplicates something the platform already owns")

    def test_the_record_stores_no_clinical_columns(self):
        """A clinical field here would be a second copy free to disagree."""
        columns = {f.name for f in MedicalRecord._meta.get_fields()}
        assert columns == {"id", "patient", "status", "notes", "created_at",
                           "updated_at"}

    def test_editing_the_source_changes_the_timeline_with_no_sync(
            self, client, cast):
        _order, _exam, report = imaging_study(cast, released=True,
                                              impression="First impression.")
        as_user(client, cast["patient_token"])
        first = client.get("/api/records/me/timeline/?type=radiology_report")
        assert "First impression." in str(first.data)

        # Edit the source record directly. Nothing tells the records app.
        report.impression = "Corrected impression."
        report.save(update_fields=["impression"])

        second = client.get("/api/records/me/timeline/?type=radiology_report")
        assert "Corrected impression." in str(second.data)
        assert "First impression." not in str(second.data)

    def test_a_record_is_created_on_first_access_not_by_backfill(
            self, client, cast):
        assert not MedicalRecord.objects.filter(patient=cast["patient"]).exists()
        as_user(client, cast["patient_token"])
        assert client.get("/api/records/me/").status_code == 200
        assert MedicalRecord.objects.filter(patient=cast["patient"]).count() == 1
        # And opening it again does not make a second one.
        client.get("/api/records/me/")
        assert MedicalRecord.objects.filter(patient=cast["patient"]).count() == 1

    def test_the_reference_layer_is_a_registry_not_a_table(self):
        """Sources register themselves; records imports no clinical module."""
        names = {getattr(source, "__name__", "") for source in timeline.registered()}
        assert {"appointment_entries",
                "radiology_timeline_entries",
                "pharmacy_timeline_entries"} <= names

        import records.services
        import records.sources
        import records.timeline
        for module in (records.timeline, records.sources, records.services):
            text = open(module.__file__, encoding="utf-8").read()
            assert "import radiology" not in text
            assert "import pharmacy" not in text


# ---------------------------------------------------------------------------
# The timeline
# ---------------------------------------------------------------------------
class TestTimeline:
    def test_it_combines_every_module(self, client, cast):
        imaging_study(cast, released=True)
        prescription(cast)
        as_user(client, cast["patient_token"])
        payload = client.get("/api/records/me/timeline/?limit=50").data
        found = set(types_in(payload))
        assert {"consultation", "radiology_report", "radiology_order",
                "prescription"} <= found

    def test_events_are_sorted_newest_first_across_modules(self, client, cast):
        imaging_study(cast, released=True)
        prescription(cast)
        as_user(client, cast["patient_token"])
        payload = client.get("/api/records/me/timeline/?limit=50").data
        dates = [entry["date"] for entry in payload["results"]]
        assert dates == sorted(dates, reverse=True)

    def test_each_event_uses_its_own_real_timestamp(self, client, cast):
        _order, _exam, report = imaging_study(cast, released=True)
        issued = prescription(cast)
        as_user(client, cast["patient_token"])
        payload = client.get("/api/records/me/timeline/?limit=50").data
        by_type = {entry["type"]: entry for entry in payload["results"]}

        report.refresh_from_db()
        assert by_type["radiology_report"]["date"][:19] == \
            report.released_at.isoformat()[:19]
        assert by_type["prescription"]["date"][:19] == \
            issued.issued_at.isoformat()[:19]
        assert by_type["consultation"]["date"][:19] == \
            cast["visit"].start_at.isoformat()[:19]

    def test_a_completed_doctor_visit_is_a_consultation(self, client, cast):
        as_user(client, cast["patient_token"])
        payload = client.get("/api/records/me/timeline/?limit=50").data
        assert "consultation" in types_in(payload)

    def test_a_future_booking_is_an_appointment_not_a_consultation(
            self, client, cast):
        appointment(cast["doctor"], cast["patient"], days=5,
                    status=Appointment.SCHEDULED)
        as_user(client, cast["patient_token"])
        payload = client.get("/api/records/me/timeline/?limit=50").data
        kinds = types_in(payload)
        assert kinds.count("consultation") == 1
        assert "appointment" in kinds

    def test_lab_and_radiology_appointments_both_appear(self, client, cast):
        lab, _token = make(roles.LABORATORY, "mr_lab")
        appointment(lab, cast["patient"], days=-4)
        imaging_study(cast, released=True)
        as_user(client, cast["patient_token"])
        payload = client.get("/api/records/me/timeline/?limit=50").data
        providers = {entry["provider"] for entry in payload["results"]}
        assert "Mr Lab" in providers
        assert "Mr Centre" in providers

    def test_entries_reference_the_source_and_carry_no_internal_ids(
            self, client, cast):
        prescription(cast)
        as_user(client, cast["patient_token"])
        payload = client.get(
            "/api/records/me/timeline/?type=prescription").data
        entry = payload["results"][0]
        assert entry["source"] == "pharmacy.Prescription"
        assert isinstance(entry["reference"], int)
        # A timeline row is a reference, not a record dump.
        for leaked in ("patient", "patient_id", "doctor", "items", "notes"):
            assert leaked not in entry

    def test_the_vocabulary_is_served_not_hardcoded(self, client, cast):
        as_user(client, cast["patient_token"])
        res = client.get("/api/records/types/")
        assert res.status_code == 200
        assert {t["value"] for t in res.data} == set(timeline.ALL_TYPES)

    def test_kinds_with_no_module_are_reported_as_unavailable(self, client, cast):
        """Roshada has no Laboratory module, so lab results cannot exist yet."""
        as_user(client, cast["patient_token"])
        types = {t["value"]: t for t in client.get("/api/records/types/").data}
        assert types["lab_result"]["available"] is False
        assert types["radiology_report"]["available"] is True
        overview = client.get("/api/records/me/").data
        assert "lab_result" in overview["unavailable_types"]


# ---------------------------------------------------------------------------
# Filtering, search and pagination
# ---------------------------------------------------------------------------
class TestFilteringAndPagination:
    def test_filtering_by_type(self, client, cast):
        imaging_study(cast, released=True)
        prescription(cast)
        as_user(client, cast["patient_token"])
        payload = client.get("/api/records/me/timeline/?type=prescription").data
        assert set(types_in(payload)) == {"prescription"}

    def test_filtering_by_several_types(self, client, cast):
        imaging_study(cast, released=True)
        prescription(cast)
        as_user(client, cast["patient_token"])
        payload = client.get(
            "/api/records/me/timeline/?type=prescription,consultation").data
        assert set(types_in(payload)) <= {"prescription", "consultation"}

    def test_an_unknown_type_is_an_error_not_an_empty_list(self, client, cast):
        """Silently returning nothing would read as 'you have no lab results'."""
        as_user(client, cast["patient_token"])
        res = client.get("/api/records/me/timeline/?type=horoscope")
        assert res.status_code == 400
        assert "horoscope" in res.data["error"]

    def test_date_range_filtering(self, client, cast):
        imaging_study(cast, released=True)
        today = timezone.localtime().date()
        as_user(client, cast["patient_token"])
        # The consultation is 3 days back; a window starting yesterday excludes it.
        recent = client.get(
            f"/api/records/me/timeline/?from={today - datetime.timedelta(days=1)}").data
        assert "consultation" not in types_in(recent)
        everything = client.get(
            f"/api/records/me/timeline/?from={today - datetime.timedelta(days=30)}").data
        assert "consultation" in types_in(everything)

    def test_a_malformed_date_is_rejected(self, client, cast):
        as_user(client, cast["patient_token"])
        res = client.get("/api/records/me/timeline/?from=last-tuesday")
        assert res.status_code == 400

    def test_search_matches_title_and_provider(self, client, cast):
        imaging_study(cast, released=True)
        prescription(cast)
        as_user(client, cast["patient_token"])
        assert titles_in(client.get(
            "/api/records/me/timeline/?q=amoxicillin").data) == ["Amoxicillin 500 mg"]
        assert client.get(
            "/api/records/me/timeline/?q=mri").data["results"]

    def test_pagination(self, client, cast):
        for index in range(8):
            appointment(cast["doctor"], cast["patient"], days=-(10 + index))
        as_user(client, cast["patient_token"])
        first = client.get("/api/records/me/timeline/?limit=3").data
        assert first["count"] == 3 and first["has_more"] is True
        second = client.get("/api/records/me/timeline/?limit=3&offset=3").data
        assert second["count"] == 3
        # No overlap between pages.
        assert not ({e["reference"] for e in first["results"]}
                    & {e["reference"] for e in second["results"]})

    def test_the_page_size_is_capped(self, client, cast):
        """An unbounded limit would let one request ask for a whole history."""
        as_user(client, cast["patient_token"])
        payload = client.get("/api/records/me/timeline/?limit=100000").data
        assert payload["limit"] <= 100


# ---------------------------------------------------------------------------
# The release gate — the aggregation layer must not bypass a source's rules
# ---------------------------------------------------------------------------
class TestSourcePermissionsArePreserved:
    def test_a_draft_report_is_absent_and_appears_only_once_released(
            self, client, cast):
        _order, _exam, report = imaging_study(cast, released=False,
                                              impression="Suspicious mass.")
        as_user(client, cast["patient_token"])

        before = client.get("/api/records/me/timeline/?limit=50").data
        assert "radiology_report" not in types_in(before)
        assert "Suspicious mass." not in str(before)

        release(report, cast["center"])

        after = client.get("/api/records/me/timeline/?limit=50").data
        assert "radiology_report" in types_in(after)
        assert "Suspicious mass." in str(after)

    def test_a_verified_but_unreleased_report_stays_hidden(self, client, cast):
        from radiology import services as radiology_services
        _order, _exam, report = imaging_study(cast, released=False)
        for step in (RadiologyReport.PENDING_REVIEW, RadiologyReport.VERIFIED):
            radiology_services.transition_report(cast["center"], report.id, step)
        as_user(client, cast["patient_token"])
        payload = client.get("/api/records/me/timeline/?limit=50").data
        assert "radiology_report" not in types_in(payload)

    def test_an_unissued_prescription_is_absent_until_issued(self, client, cast):
        from pharmacy import services as pharmacy_services
        draft = prescription(cast, issue=False)
        as_user(client, cast["patient_token"])
        assert "prescription" not in types_in(
            client.get("/api/records/me/timeline/?limit=50").data)

        pharmacy_services.transition_prescription(
            cast["doctor"], draft.id, Prescription.ISSUED)
        assert "prescription" in types_in(
            client.get("/api/records/me/timeline/?limit=50").data)

    def test_the_timeline_carries_no_imaging_file_bytes_or_urls(
            self, client, cast):
        """Section 26: never load imaging when metadata is what is wanted."""
        imaging_study(cast, released=True)
        as_user(client, cast["patient_token"])
        body = str(client.get("/api/records/me/timeline/?limit=50").data)
        assert "/media/" not in body
        assert "download" not in body


# ---------------------------------------------------------------------------
# Who may open a record
# ---------------------------------------------------------------------------
class TestAccess:
    def test_a_patient_opens_their_own_record(self, client, cast):
        as_user(client, cast["patient_token"])
        res = client.get("/api/records/me/")
        assert res.status_code == 200
        assert res.data["is_own_record"] is True
        assert res.data["patient"]["username"] == "mr_patient"

    def test_a_patient_cannot_open_another_patients_record(self, client, cast):
        other, other_token = make(roles.PATIENT, "mr_other_patient")
        prescription(cast)
        as_user(client, other_token)
        # There is no path that takes a patient id from a patient at all, and
        # the doctor-scoped one refuses them outright.
        assert client.get(
            f"/api/records/patients/{cast['patient'].id}/").status_code == 403
        # Their own record is their own, and contains nothing of the first
        # patient's.
        mine = client.get("/api/records/me/").data
        assert mine["patient"]["id"] == other.id
        assert client.get(
            "/api/records/me/timeline/?limit=50").data["results"] == []

    def test_a_treating_doctor_opens_the_record(self, client, cast):
        imaging_study(cast, released=True)
        prescription(cast)
        as_user(client, cast["doctor_token"])
        res = client.get(f"/api/records/patients/{cast['patient'].id}/")
        assert res.status_code == 200
        assert res.data["is_own_record"] is False
        payload = client.get(
            f"/api/records/patients/{cast['patient'].id}/timeline/?limit=50").data
        assert {"consultation", "radiology_report", "prescription"} <= \
            set(types_in(payload))

    def test_a_doctor_without_a_care_relationship_is_refused(self, client, cast):
        _stranger, stranger_token = make(roles.DOCTOR, "mr_stranger_doctor")
        as_user(client, stranger_token)
        # 404, not 403: a 403 would confirm this patient exists.
        assert client.get(
            f"/api/records/patients/{cast['patient'].id}/").status_code == 404
        assert client.get(
            f"/api/records/patients/{cast['patient'].id}/timeline/"
        ).status_code == 404

    def test_a_doctors_patient_list_is_only_their_own(self, client, cast):
        make(roles.PATIENT, "mr_someone_elses")
        as_user(client, cast["doctor_token"])
        res = client.get("/api/records/patients/")
        assert [p["id"] for p in res.data] == [cast["patient"].id]

    def test_a_doctor_is_not_shown_dispensing_activity(self, client, cast):
        """Carried over from the pharmacy module, not re-decided here."""
        from pharmacy import services as pharmacy_services
        issued = prescription(cast)
        medication = issued.items.first().medication
        PharmacyInventory.objects.create(pharmacy=cast["pharmacy"],
                                         medication=medication, quantity=50,
                                         price="100.00")
        pharmacy_services.create_request(
            cast["patient"], cast["pharmacy"].id,
            [{"prescription_item_id": issued.items.first().id}],
            prescription_id=issued.id)

        as_user(client, cast["patient_token"])
        assert "medication_order" in types_in(
            client.get("/api/records/me/timeline/?limit=50").data)

        as_user(client, cast["doctor_token"])
        payload = client.get(
            f"/api/records/patients/{cast['patient'].id}/timeline/?limit=50").data
        assert "medication_order" not in types_in(payload)

    def test_a_pharmacy_gets_no_medical_record(self, client, cast):
        """Section 17: a pharmacy must not receive the whole record."""
        prescription(cast)
        as_user(client, cast["pharmacy_token"])
        assert client.get(
            f"/api/records/patients/{cast['patient'].id}/").status_code == 403
        # A pharmacy account is not a patient, so it has no record of its own
        # either — 404 rather than an empty timeline, which would imply one
        # exists and simply happens to be blank.
        assert client.get("/api/records/me/").status_code == 404
        assert client.get(
            "/api/records/me/timeline/?limit=50").status_code == 404

    def test_a_radiology_centre_gets_no_medical_record(self, client, cast):
        imaging_study(cast, released=True)
        as_user(client, cast["center_token"])
        assert client.get(
            f"/api/records/patients/{cast['patient'].id}/").status_code == 403
        assert client.get(
            "/api/records/me/timeline/?limit=50").status_code == 404

    def test_a_laboratory_gets_no_medical_record(self, client, cast):
        _lab, lab_token = make(roles.LABORATORY, "mr_lab_two")
        as_user(client, lab_token)
        assert client.get(
            f"/api/records/patients/{cast['patient'].id}/").status_code == 403
        assert client.get(
            "/api/records/me/timeline/?limit=50").status_code == 404

    def test_an_admin_is_not_given_clinical_history(self, client, cast):
        """Administering the platform is not a reason to read a history."""
        from rest_framework.authtoken.models import Token
        from accounts.models import UserAccount
        imaging_study(cast, released=True)
        admin = User.objects.create_user("mr_admin", password=PW)
        UserAccount.objects.create(user=admin, role=roles.ADMIN)
        as_user(client, Token.objects.create(user=admin).key)
        assert client.get(
            f"/api/records/patients/{cast['patient'].id}/").status_code == 403
        assert client.get(
            "/api/records/me/timeline/?limit=50").status_code == 404

    def test_an_anonymous_caller_can_do_nothing(self, client, cast):
        client.credentials()
        for path in ("/api/records/me/", "/api/records/me/timeline/",
                     "/api/records/types/", "/api/records/patients/",
                     f"/api/records/patients/{cast['patient'].id}/"):
            assert client.get(path).status_code == 401, path

    def test_the_record_is_read_only(self, client, cast):
        """Section 33: the aggregation layer never writes to a source."""
        as_user(client, cast["patient_token"])
        for method in ("post", "put", "patch", "delete"):
            res = getattr(client, method)("/api/records/me/", {}, format="json")
            assert res.status_code == 405, method


# ---------------------------------------------------------------------------
# Overview and performance
# ---------------------------------------------------------------------------
class TestOverviewAndPerformance:
    def test_the_overview_groups_real_events(self, client, cast):
        imaging_study(cast, released=True)
        prescription(cast)
        as_user(client, cast["patient_token"])
        data = client.get("/api/records/me/").data
        assert data["counts"]["prescription"] == 1
        assert data["counts"]["radiology_report"] == 1
        assert data["recent_prescriptions"][0]["type"] == "prescription"
        assert data["recent_activity"]

    def test_the_overview_hides_exactly_what_the_timeline_hides(
            self, client, cast):
        """One code path, so the landing view cannot leak what the list won't."""
        imaging_study(cast, released=False, impression="Not for the patient.")
        as_user(client, cast["patient_token"])
        data = client.get("/api/records/me/").data
        assert data["recent_radiology"] == []
        assert "Not for the patient." not in str(data)

    def test_upcoming_appointments_are_separated_from_history(self, client, cast):
        appointment(cast["doctor"], cast["patient"], days=6,
                    status=Appointment.SCHEDULED)
        as_user(client, cast["patient_token"])
        data = client.get("/api/records/me/").data
        assert len(data["upcoming_appointments"]) == 1

    def test_a_new_patient_sees_an_empty_record_not_an_error(self, client):
        _fresh, token = make(roles.PATIENT, "mr_fresh")
        as_user(client, token)
        data = client.get("/api/records/me/").data
        assert data["recent_activity"] == []
        assert client.get(
            "/api/records/me/timeline/").data["results"] == []

    def test_the_timeline_does_not_scale_with_event_count(
            self, client, cast, django_assert_max_num_queries):
        """Four sources, each bounded — not one query per event."""
        imaging_study(cast, released=True)
        for index in range(6):
            appointment(cast["doctor"], cast["patient"], days=-(20 + index))
            prescription(cast, medication_name=f"Drug {index}")

        as_user(client, cast["patient_token"])
        with django_assert_max_num_queries(15):
            payload = client.get("/api/records/me/timeline/?limit=25").data
        assert len(payload["results"]) > 10

    def test_the_overview_does_not_scale_with_event_count(
            self, client, cast, django_assert_max_num_queries):
        imaging_study(cast, released=True)
        for index in range(5):
            prescription(cast, medication_name=f"Other {index}")
        as_user(client, cast["patient_token"])
        with django_assert_max_num_queries(20):
            assert client.get("/api/records/me/").status_code == 200

    def test_one_broken_source_does_not_blank_the_history(self, client, cast):
        """An empty medical record reads as 'nothing ever happened'."""
        prescription(cast)

        def exploding_source(viewer, patient, limit):
            raise RuntimeError("this module is having a bad day")

        timeline.source(exploding_source)
        try:
            as_user(client, cast["patient_token"])
            payload = client.get("/api/records/me/timeline/?limit=50").data
            assert "prescription" in types_in(payload)
        finally:
            timeline._SOURCES.remove(exploding_source)


# ---------------------------------------------------------------------------
# Nothing else broke
# ---------------------------------------------------------------------------
class TestExistingModulesStillWork:
    def test_radiology_report_scoping_is_unchanged(self, client, cast):
        from radiology import services as radiology_services
        _order, _exam, report = imaging_study(cast, released=False)
        assert radiology_services.reports_for(cast["patient"]).count() == 0
        release(report, cast["center"])
        assert radiology_services.reports_for(cast["patient"]).count() == 1

    def test_pharmacy_prescription_scoping_is_unchanged(self, cast):
        from pharmacy import services as pharmacy_services
        prescription(cast, issue=False)
        assert pharmacy_services.prescriptions_for(cast["patient"]).count() == 0
        assert pharmacy_services.prescriptions_for(cast["doctor"]).count() == 1

    def test_appointments_still_answer(self, client, cast):
        as_user(client, cast["patient_token"])
        assert client.get("/api/appointments/mine/").status_code == 200

    def test_the_dashboard_still_answers(self, client, cast):
        as_user(client, cast["patient_token"])
        assert client.get("/api/dashboard/summary/").status_code == 200
