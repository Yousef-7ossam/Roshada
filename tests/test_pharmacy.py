"""The Pharmacy module.

The workflow tests walk the real chain — prescribe → find a pharmacy → request
→ confirm → ready → complete — because each step's guarantee depends on the one
before it, and testing them in isolation would miss exactly the disagreements
that matter (stock reserved for a request that was already cancelled, a request
confirmed against a shelf that has since emptied).

Three sections are deliberately larger than the happy path, because they are
the ones a mistake would actually cost something:

* **Overselling.** Proven against PostgreSQL, not against the service layer —
  the check constraint is written to directly, so the guarantee survives a bug
  in the code that is supposed to respect it.
* **Concurrency.** Two real threads confirming against one shelf.
* **Disclosure.** What a pharmacy is *not* told about a prescription.
"""
import datetime
import threading

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from accounts import roles
from accounts.services import register_account
from pharmacy import dosage_forms
from pharmacy.models import (
    Medication, MedicationRequest, MedicationRequestItem, PharmacyInventory,
    Prescription, PrescriptionItem,
)

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
        roles.PATIENT: {"age": 35},
        roles.DOCTOR: {"specialization": "General Practice"},
        roles.RADIOLOGY: {"services": "MRI"},
        roles.LABORATORY: {"services": "CBC"},
        roles.PHARMACY: {"services": "Dispensing"},
    }[role]
    user, _account, _profile, token = register_account(
        role, username=username, password=PW,
        name=username.replace("_", " ").title(), **{**defaults, **extra})
    return user, token.key


def care_relationship(doctor, patient, days=1):
    """The platform's existing definition of "my patient": an appointment."""
    from appointments.models import Appointment
    from appointments.services import availability
    start = availability.combine(
        (timezone.localtime() + datetime.timedelta(days=days)).date(),
        datetime.time(9, 0))
    return Appointment.objects.create(
        provider=doctor, patient=patient, start_at=start,
        end_at=start + datetime.timedelta(minutes=30))


def medication(name="Amoxicillin", strength="500 mg",
               form=dosage_forms.CAPSULE, generic="Amoxicillin"):
    return Medication.objects.create(name=name, strength=strength, form=form,
                                     generic_name=generic)


def stock(pharmacy, med, quantity=10, price="100.00", **extra):
    return PharmacyInventory.objects.create(
        pharmacy=pharmacy, medication=med, quantity=quantity, price=price,
        **extra)


@pytest.fixture
def cast():
    """A doctor, a patient they treat, two pharmacies and one medication."""
    patient, patient_token = make(roles.PATIENT, "rx_patient")
    doctor, doctor_token = make(roles.DOCTOR, "rx_doctor")
    pharmacy_a, pharmacy_a_token = make(roles.PHARMACY, "pharmacy_a")
    pharmacy_b, pharmacy_b_token = make(roles.PHARMACY, "pharmacy_b")
    care_relationship(doctor, patient)
    med = medication()
    return {
        "patient": patient, "patient_token": patient_token,
        "doctor": doctor, "doctor_token": doctor_token,
        "pharmacy_a": pharmacy_a, "pharmacy_a_token": pharmacy_a_token,
        "pharmacy_b": pharmacy_b, "pharmacy_b_token": pharmacy_b_token,
        "medication": med,
    }


def as_user(client, token):
    client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
    return client


def write_prescription(client, cast, quantity=21, issue=True):
    as_user(client, cast["doctor_token"])
    res = client.post("/api/pharmacy/prescriptions/", {
        "patient_id": cast["patient"].id,
        "diagnosis": "Bacterial throat infection",
        "issue": issue,
        "items": [{"medication_id": cast["medication"].id, "dosage": "1 capsule",
                   "frequency": "3 times/day", "duration": "7 days",
                   "quantity": quantity,
                   "instructions": "Take after food."}],
    }, format="json")
    assert res.status_code == 201, res.data
    return res.data


def submit_request(client, cast, prescription, pharmacy=None, expect=201,
                   quantity=None):
    pharmacy = pharmacy or cast["pharmacy_a"]
    item = {"prescription_item_id": prescription["items"][0]["id"]}
    if quantity is not None:
        item["quantity"] = quantity
    as_user(client, cast["patient_token"])
    res = client.post("/api/pharmacy/requests/", {
        "pharmacy_id": pharmacy.id,
        "prescription_id": prescription["id"],
        "items": [item],
    }, format="json")
    assert res.status_code == expect, res.data
    return res


def move(client, token, request_id, to_status, expect=200):
    as_user(client, token)
    res = client.post(f"/api/pharmacy/requests/{request_id}/status/",
                      {"status": to_status}, format="json")
    assert res.status_code == expect, res.data
    return res


# ---------------------------------------------------------------------------
# Built on the existing architecture, not beside it
# ---------------------------------------------------------------------------
class TestBuiltOnTheExistingArchitecture:
    def test_no_duplicate_user_pharmacy_or_appointment_models(self):
        from django.apps import apps
        names = {m.__name__ for m in apps.get_app_config("pharmacy").get_models()}
        assert names == {"Medication", "Prescription", "PrescriptionItem",
                         "PharmacyInventory", "MedicationRequest",
                         "MedicationRequestItem"}
        for forbidden in ("Pharmacy", "PharmacyUser", "Patient", "Doctor",
                          "PharmacyAppointment", "PharmacySlot",
                          "Notification", "Order"):
            assert forbidden not in names, (
                f"{forbidden} duplicates something the platform already has")

    def test_a_pharmacy_is_an_account_not_a_new_entity(self, cast):
        from accounts.models import PharmacyProfile
        assert PharmacyProfile.objects.filter(user=cast["pharmacy_a"]).exists()
        assert cast["pharmacy_a"].account.role == roles.PHARMACY

    def test_pharmacy_capabilities_are_no_longer_planned(self):
        assert roles.PHARMACY_PRESCRIPTIONS not in roles.PLANNED_CAPABILITIES
        assert roles.PHARMACY_INVENTORY not in roles.PLANNED_CAPABILITIES
        # Laboratory is genuinely still unbuilt and must stay declared.
        assert roles.LAB_ORDERS in roles.PLANNED_CAPABILITIES

    def test_pharmacy_did_not_become_bookable(self):
        """Dispensing is not an appointment. Nothing here may change that."""
        assert roles.PHARMACY not in roles.BOOKABLE_ROLES


# ---------------------------------------------------------------------------
# The medication catalogue
# ---------------------------------------------------------------------------
class TestMedicationCatalogue:
    def test_identity_is_case_insensitive(self, cast):
        """The whole module rests on one product having one row."""
        med = cast["medication"]
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Medication.objects.create(name="amoxicillin",
                                          strength="500 MG",
                                          form=med.form)

    def test_a_different_strength_is_a_different_product(self, cast):
        other = Medication.objects.create(name="Amoxicillin", strength="250 mg",
                                          form=dosage_forms.CAPSULE)
        assert other.pk != cast["medication"].pk

    def test_search_matches_generic_name(self, client, cast):
        Medication.objects.create(name="Panadol", strength="500 mg",
                                  generic_name="Paracetamol",
                                  form=dosage_forms.TABLET)
        as_user(client, cast["patient_token"])
        res = client.get("/api/pharmacy/medications/?q=paracetamol")
        assert res.status_code == 200
        assert [m["name"] for m in res.data] == ["Panadol"]

    def test_adding_an_existing_product_reuses_it(self, client, cast):
        as_user(client, cast["doctor_token"])
        res = client.post("/api/pharmacy/medications/",
                          {"name": "AMOXICILLIN", "strength": "500 mg",
                           "form": dosage_forms.CAPSULE}, format="json")
        # 200, not 201: nothing was created.
        assert res.status_code == 200, res.data
        assert res.data["id"] == cast["medication"].id

    def test_a_patient_cannot_add_to_the_catalogue(self, client, cast):
        as_user(client, cast["patient_token"])
        res = client.post("/api/pharmacy/medications/",
                          {"name": "Something", "strength": "1 mg"},
                          format="json")
        assert res.status_code == 403

    def test_dosage_forms_are_served_not_hardcoded(self, client, cast):
        as_user(client, cast["patient_token"])
        res = client.get("/api/pharmacy/dosage-forms/")
        assert res.status_code == 200
        assert {f["value"] for f in res.data} == set(dosage_forms.ALL)


# ---------------------------------------------------------------------------
# Doctor → Prescription → Items → Medication
# ---------------------------------------------------------------------------
class TestPrescribing:
    def test_a_prescription_is_structured_not_a_text_blob(self, client, cast):
        data = write_prescription(client, cast)
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["medication"]["id"] == cast["medication"].id
        assert item["frequency"] == "3 times/day"
        assert item["quantity"] == 21
        # The medication is a relation, not a copy of its name.
        row = PrescriptionItem.objects.get(pk=item["id"])
        assert row.medication_id == cast["medication"].id

    def test_a_doctor_cannot_prescribe_for_a_stranger(self, client, cast):
        stranger, _token = make(roles.PATIENT, "not_my_patient")
        as_user(client, cast["doctor_token"])
        res = client.post("/api/pharmacy/prescriptions/", {
            "patient_id": stranger.id,
            "items": [{"medication_id": cast["medication"].id}],
        }, format="json")
        assert res.status_code == 403

    def test_a_patient_cannot_write_a_prescription(self, client, cast):
        as_user(client, cast["patient_token"])
        res = client.post("/api/pharmacy/prescriptions/", {
            "patient_id": cast["patient"].id,
            "items": [{"medication_id": cast["medication"].id}],
        }, format="json")
        assert res.status_code == 403

    def test_a_pharmacy_cannot_write_a_prescription(self, client, cast):
        as_user(client, cast["pharmacy_a_token"])
        res = client.post("/api/pharmacy/prescriptions/", {
            "patient_id": cast["patient"].id,
            "items": [{"medication_id": cast["medication"].id}],
        }, format="json")
        assert res.status_code == 403

    def test_a_prescription_needs_at_least_one_medication(self, client, cast):
        as_user(client, cast["doctor_token"])
        res = client.post("/api/pharmacy/prescriptions/",
                          {"patient_id": cast["patient"].id, "items": []},
                          format="json")
        assert res.status_code == 400

    def test_a_draft_is_invisible_to_the_patient(self, client, cast):
        draft = write_prescription(client, cast, issue=False)
        assert draft["status"] == Prescription.DRAFT

        as_user(client, cast["patient_token"])
        assert client.get("/api/pharmacy/prescriptions/").data == []
        assert client.get(
            f"/api/pharmacy/prescriptions/{draft['id']}/").status_code == 404

        # Issued, the same prescription becomes readable.
        as_user(client, cast["doctor_token"])
        assert client.post(f"/api/pharmacy/prescriptions/{draft['id']}/status/",
                           {"status": Prescription.ISSUED},
                           format="json").status_code == 200
        as_user(client, cast["patient_token"])
        assert len(client.get("/api/pharmacy/prescriptions/").data) == 1

    def test_a_patient_sees_their_own_prescription_in_full(self, client, cast):
        write_prescription(client, cast)
        as_user(client, cast["patient_token"])
        res = client.get("/api/pharmacy/prescriptions/")
        assert res.status_code == 200
        assert res.data[0]["doctor_name"]
        assert res.data[0]["items"][0]["instructions"] == "Take after food."

    def test_one_doctor_cannot_read_anothers_prescribing(self, client, cast):
        written = write_prescription(client, cast)
        other, other_token = make(roles.DOCTOR, "other_doctor")
        care_relationship(other, cast["patient"], days=3)
        as_user(client, other_token)
        assert client.get("/api/pharmacy/prescriptions/").data == []
        assert client.get(
            f"/api/pharmacy/prescriptions/{written['id']}/").status_code == 404

    def test_cancelling_is_the_prescribing_doctors_call(self, client, cast):
        written = write_prescription(client, cast)
        as_user(client, cast["patient_token"])
        assert client.post(
            f"/api/pharmacy/prescriptions/{written['id']}/status/",
            {"status": Prescription.CANCELLED}, format="json"
        ).status_code == 403

        as_user(client, cast["doctor_token"])
        res = client.post(f"/api/pharmacy/prescriptions/{written['id']}/status/",
                          {"status": Prescription.CANCELLED,
                           "reason": "Allergy reported"}, format="json")
        assert res.status_code == 200
        assert res.data["status"] == Prescription.CANCELLED

    def test_a_cancelled_prescription_cannot_be_reissued(self, client, cast):
        written = write_prescription(client, cast)
        as_user(client, cast["doctor_token"])
        client.post(f"/api/pharmacy/prescriptions/{written['id']}/status/",
                    {"status": Prescription.CANCELLED}, format="json")
        res = client.post(f"/api/pharmacy/prescriptions/{written['id']}/status/",
                          {"status": Prescription.ISSUED}, format="json")
        assert res.status_code == 409

    def test_prescribable_patients_are_only_the_doctors_own(self, client, cast):
        make(roles.PATIENT, "someone_elses_patient")
        as_user(client, cast["doctor_token"])
        res = client.get("/api/pharmacy/prescribable-patients/")
        assert res.status_code == 200
        assert [p["id"] for p in res.data] == [cast["patient"].id]


# ---------------------------------------------------------------------------
# Pharmacy → Medication → Stock
# ---------------------------------------------------------------------------
class TestInventory:
    def test_a_pharmacy_stocks_a_medication(self, client, cast):
        as_user(client, cast["pharmacy_a_token"])
        res = client.post("/api/pharmacy/inventory/",
                          {"medication_id": cast["medication"].id,
                           "quantity": 10, "price": "100.00"}, format="json")
        assert res.status_code == 201, res.data
        assert res.data["quantity"] == 10
        assert res.data["available_quantity"] == 10
        assert res.data["in_stock"] is True

    def test_stocking_the_same_product_twice_updates_one_line(self, client, cast):
        as_user(client, cast["pharmacy_a_token"])
        first = client.post("/api/pharmacy/inventory/",
                            {"medication_id": cast["medication"].id,
                             "quantity": 10}, format="json")
        second = client.post("/api/pharmacy/inventory/",
                             {"medication_id": cast["medication"].id,
                              "quantity": 25}, format="json")
        assert second.status_code == 200
        assert second.data["id"] == first.data["id"]
        assert PharmacyInventory.objects.filter(
            pharmacy=cast["pharmacy_a"], medication=cast["medication"]).count() == 1

    def test_the_database_refuses_a_duplicate_stock_line(self, cast):
        """Not merely the service layer: the constraint is the guarantee."""
        stock(cast["pharmacy_a"], cast["medication"])
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                PharmacyInventory.objects.create(
                    pharmacy=cast["pharmacy_a"], medication=cast["medication"],
                    quantity=5)

    def test_a_pharmacy_cannot_touch_another_pharmacys_stock(self, client, cast):
        line = stock(cast["pharmacy_b"], cast["medication"])
        as_user(client, cast["pharmacy_a_token"])
        # Not 403: telling A that B's line id exists is itself a disclosure.
        assert client.get(
            f"/api/pharmacy/inventory/{line.id}/").status_code == 404
        assert client.patch(f"/api/pharmacy/inventory/{line.id}/",
                            {"quantity": 0}, format="json").status_code == 404
        line.refresh_from_db()
        assert line.quantity == 10

    def test_a_pharmacy_only_lists_its_own_stock(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"], quantity=4)
        other = medication(name="Ibuprofen", strength="400 mg",
                           form=dosage_forms.TABLET, generic="Ibuprofen")
        stock(cast["pharmacy_b"], other, quantity=9)
        as_user(client, cast["pharmacy_a_token"])
        res = client.get("/api/pharmacy/inventory/")
        assert [line["medication"]["name"] for line in res.data] == ["Amoxicillin"]

    def test_a_doctor_cannot_manage_inventory(self, client, cast):
        as_user(client, cast["doctor_token"])
        assert client.get("/api/pharmacy/inventory/").status_code == 403
        assert client.post("/api/pharmacy/inventory/",
                           {"medication_id": cast["medication"].id,
                            "quantity": 5}, format="json").status_code == 403

    def test_a_patient_cannot_manage_inventory(self, client, cast):
        as_user(client, cast["patient_token"])
        assert client.get("/api/pharmacy/inventory/").status_code == 403

    def test_inventory_can_be_searched_and_filtered(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"], quantity=5)
        empty = medication(name="Ibuprofen", strength="400 mg",
                           form=dosage_forms.TABLET, generic="Ibuprofen")
        stock(cast["pharmacy_a"], empty, quantity=0)

        as_user(client, cast["pharmacy_a_token"])
        assert len(client.get("/api/pharmacy/inventory/?q=ibup").data) == 1
        in_stock = client.get("/api/pharmacy/inventory/?availability=in_stock")
        assert [line["medication"]["name"] for line in in_stock.data] == \
            ["Amoxicillin"]
        out = client.get("/api/pharmacy/inventory/?availability=out_of_stock")
        assert [line["medication"]["name"] for line in out.data] == ["Ibuprofen"]

    def test_a_line_can_be_deactivated_without_losing_its_stock(self, client, cast):
        line = stock(cast["pharmacy_a"], cast["medication"], quantity=7)
        as_user(client, cast["pharmacy_a_token"])
        res = client.patch(f"/api/pharmacy/inventory/{line.id}/",
                           {"is_active": False}, format="json")
        assert res.status_code == 200
        assert res.data["is_active"] is False
        assert res.data["quantity"] == 7
        assert res.data["in_stock"] is False


# ---------------------------------------------------------------------------
# Real availability, read from inventory
# ---------------------------------------------------------------------------
class TestAvailability:
    def test_availability_comes_from_stock_not_from_a_hardcoded_list(
            self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"], quantity=10, price="100.00")
        stock(cast["pharmacy_b"], cast["medication"], quantity=0, price="95.00")

        as_user(client, cast["patient_token"])
        res = client.get(
            f"/api/pharmacy/availability/?medication={cast['medication'].id}")
        assert res.status_code == 200
        by_name = {entry["name"]: entry for entry in res.data}
        assert by_name["Pharmacy A"]["available"] is True
        assert by_name["Pharmacy A"]["quantity_available"] == 10
        assert by_name["Pharmacy B"]["available"] is False
        assert by_name["Pharmacy B"]["quantity_available"] == 0

    def test_a_pharmacy_that_never_stocked_it_is_not_listed(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"])
        as_user(client, cast["patient_token"])
        res = client.get(
            f"/api/pharmacy/availability/?medication={cast['medication'].id}")
        assert [e["name"] for e in res.data] == ["Pharmacy A"]

    def test_reserved_units_are_not_offered_to_the_next_patient(self, client, cast):
        """Availability is stock minus what is already promised."""
        stock(cast["pharmacy_a"], cast["medication"], quantity=5)
        prescription = write_prescription(client, cast, quantity=4)
        created = submit_request(client, cast, prescription, quantity=4)
        move(client, cast["pharmacy_a_token"], created.data["id"],
             MedicationRequest.CONFIRMED)

        as_user(client, cast["patient_token"])
        res = client.get(
            f"/api/pharmacy/availability/?medication={cast['medication'].id}")
        assert res.data[0]["quantity_available"] == 1

    def test_internal_stock_figures_are_not_exposed_to_patients(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"], quantity=5)
        as_user(client, cast["patient_token"])
        res = client.get(
            f"/api/pharmacy/availability/?medication={cast['medication'].id}")
        # The patient learns what can be supplied, not the shelf's internals.
        assert "reserved" not in res.data[0]
        assert "quantity" not in res.data[0]

    def test_an_unavailable_pharmacy_is_not_offered(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"])
        profile = cast["pharmacy_a"].pharmacy_profile
        profile.available = False
        profile.save(update_fields=["available"])
        as_user(client, cast["patient_token"])
        res = client.get(
            f"/api/pharmacy/availability/?medication={cast['medication'].id}")
        assert res.data == []

    def test_prescription_availability_answers_each_line_separately(
            self, client, cast):
        """Section 16: one prescription may need two pharmacies."""
        second = medication(name="Vitamin D", strength="1000 IU",
                            form=dosage_forms.TABLET, generic="Cholecalciferol")
        stock(cast["pharmacy_a"], cast["medication"], quantity=30)
        stock(cast["pharmacy_b"], second, quantity=12)

        as_user(client, cast["doctor_token"])
        written = client.post("/api/pharmacy/prescriptions/", {
            "patient_id": cast["patient"].id,
            "items": [{"medication_id": cast["medication"].id, "quantity": 21},
                      {"medication_id": second.id, "quantity": 10}],
        }, format="json")
        assert written.status_code == 201

        as_user(client, cast["patient_token"])
        res = client.get(
            f"/api/pharmacy/prescriptions/{written.data['id']}/pharmacies/")
        assert res.status_code == 200
        assert len(res.data) == 2
        first_line = {p["name"] for p in res.data[0]["pharmacies"]}
        second_line = {p["name"] for p in res.data[1]["pharmacies"]}
        assert first_line == {"Pharmacy A"}
        assert second_line == {"Pharmacy B"}

    def test_searching_for_a_medication_creates_no_prescription(self, client, cast):
        """Section 26: search and prescribing are separate concepts."""
        stock(cast["pharmacy_a"], cast["medication"])
        before = Prescription.objects.count()
        as_user(client, cast["patient_token"])
        client.get(f"/api/pharmacy/availability/?medication={cast['medication'].id}")
        assert Prescription.objects.count() == before

    def test_availability_needs_a_medication(self, client, cast):
        as_user(client, cast["patient_token"])
        assert client.get("/api/pharmacy/availability/").status_code == 400


# ---------------------------------------------------------------------------
# The request lifecycle
# ---------------------------------------------------------------------------
class TestRequestWorkflow:
    def test_the_whole_chain(self, client, cast):
        line = stock(cast["pharmacy_a"], cast["medication"], quantity=30)
        prescription = write_prescription(client, cast, quantity=21)

        created = submit_request(client, cast, prescription)
        request_id = created.data["id"]
        assert created.data["status"] == MedicationRequest.PENDING
        # Submitting reserves nothing: asking is not a promise.
        line.refresh_from_db()
        assert line.reserved == 0
        # The price was snapshotted from the shelf at request time.
        assert created.data["items"][0]["unit_price"] == "100.00"
        assert created.data["total_price"] == "2100.00"

        move(client, cast["pharmacy_a_token"], request_id,
             MedicationRequest.CONFIRMED)
        line.refresh_from_db()
        assert (line.reserved, line.quantity) == (21, 30)
        assert line.available_quantity == 9

        move(client, cast["pharmacy_a_token"], request_id,
             MedicationRequest.PREPARING)
        move(client, cast["pharmacy_a_token"], request_id,
             MedicationRequest.READY)
        line.refresh_from_db()
        assert line.reserved == 21   # still held, not yet handed over

        final = move(client, cast["pharmacy_a_token"], request_id,
                     MedicationRequest.COMPLETED)
        assert final.data["status"] == MedicationRequest.COMPLETED
        line.refresh_from_db()
        # Dispensed: stock down, reservation released, together.
        assert (line.quantity, line.reserved) == (9, 0)

    def test_a_request_is_not_auto_confirmed(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"])
        prescription = write_prescription(client, cast, quantity=5)
        created = submit_request(client, cast, prescription)
        assert created.data["status"] == MedicationRequest.PENDING

    def test_a_pharmacy_can_reject(self, client, cast):
        line = stock(cast["pharmacy_a"], cast["medication"])
        prescription = write_prescription(client, cast, quantity=5)
        created = submit_request(client, cast, prescription)
        res = move(client, cast["pharmacy_a_token"], created.data["id"],
                   MedicationRequest.REJECTED)
        assert res.data["status"] == MedicationRequest.REJECTED
        line.refresh_from_db()
        assert line.reserved == 0

    def test_cancelling_a_confirmed_request_returns_the_stock(self, client, cast):
        line = stock(cast["pharmacy_a"], cast["medication"], quantity=10)
        prescription = write_prescription(client, cast, quantity=6)
        created = submit_request(client, cast, prescription)
        move(client, cast["pharmacy_a_token"], created.data["id"],
             MedicationRequest.CONFIRMED)
        line.refresh_from_db()
        assert line.reserved == 6

        move(client, cast["patient_token"], created.data["id"],
             MedicationRequest.CANCELLED)
        line.refresh_from_db()
        assert (line.reserved, line.quantity) == (0, 10)

    def test_stock_cannot_be_released_twice(self, client, cast):
        """A second cancellation must not hand the units back again."""
        line = stock(cast["pharmacy_a"], cast["medication"], quantity=10)
        prescription = write_prescription(client, cast, quantity=6)
        created = submit_request(client, cast, prescription)
        move(client, cast["pharmacy_a_token"], created.data["id"],
             MedicationRequest.CONFIRMED)
        move(client, cast["patient_token"], created.data["id"],
             MedicationRequest.CANCELLED)
        # The second attempt is refused as a transition, not silently applied.
        move(client, cast["patient_token"], created.data["id"],
             MedicationRequest.CANCELLED, expect=409)
        line.refresh_from_db()
        assert (line.reserved, line.quantity) == (0, 10)

    def test_illegal_transitions_are_refused(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"])
        prescription = write_prescription(client, cast, quantity=2)
        created = submit_request(client, cast, prescription)
        request_id = created.data["id"]

        # Cannot skip straight to completed from pending.
        move(client, cast["pharmacy_a_token"], request_id,
             MedicationRequest.COMPLETED, expect=409)
        # Cannot go backwards.
        move(client, cast["pharmacy_a_token"], request_id,
             MedicationRequest.CONFIRMED)
        move(client, cast["pharmacy_a_token"], request_id,
             MedicationRequest.PENDING, expect=403)
        # A terminal request stays terminal.
        move(client, cast["pharmacy_a_token"], request_id,
             MedicationRequest.CANCELLED)
        move(client, cast["pharmacy_a_token"], request_id,
             MedicationRequest.CONFIRMED, expect=409)

    def test_an_invented_status_is_rejected(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"])
        prescription = write_prescription(client, cast, quantity=2)
        created = submit_request(client, cast, prescription)
        as_user(client, cast["pharmacy_a_token"])
        res = client.post(
            f"/api/pharmacy/requests/{created.data['id']}/status/",
            {"status": "dispatched_by_drone"}, format="json")
        assert res.status_code == 400

    def test_a_patient_cannot_confirm_their_own_request(self, client, cast):
        """Otherwise a patient could reserve a pharmacy's stock at will."""
        line = stock(cast["pharmacy_a"], cast["medication"], quantity=10)
        prescription = write_prescription(client, cast, quantity=5)
        created = submit_request(client, cast, prescription)
        move(client, cast["patient_token"], created.data["id"],
             MedicationRequest.CONFIRMED, expect=403)
        line.refresh_from_db()
        assert line.reserved == 0

    def test_a_patient_cannot_mark_their_request_ready(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"])
        prescription = write_prescription(client, cast, quantity=5)
        created = submit_request(client, cast, prescription)
        move(client, cast["pharmacy_a_token"], created.data["id"],
             MedicationRequest.CONFIRMED)
        move(client, cast["patient_token"], created.data["id"],
             MedicationRequest.READY, expect=403)

    def test_the_same_prescription_line_cannot_be_requested_twice(
            self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"], quantity=50)
        stock(cast["pharmacy_b"], cast["medication"], quantity=50)
        prescription = write_prescription(client, cast, quantity=5)
        submit_request(client, cast, prescription)
        # Not even at a different pharmacy: it is one prescribed course.
        submit_request(client, cast, prescription, pharmacy=cast["pharmacy_b"],
                       expect=409)

    def test_a_line_can_be_requested_again_after_a_cancellation(
            self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"], quantity=50)
        prescription = write_prescription(client, cast, quantity=5)
        first = submit_request(client, cast, prescription)
        move(client, cast["patient_token"], first.data["id"],
             MedicationRequest.CANCELLED)
        submit_request(client, cast, prescription)

    def test_a_draft_prescription_cannot_be_taken_to_a_pharmacy(
            self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"])
        draft = write_prescription(client, cast, issue=False)
        as_user(client, cast["patient_token"])
        res = client.post("/api/pharmacy/requests/", {
            "pharmacy_id": cast["pharmacy_a"].id,
            "prescription_id": draft["id"],
            "items": [{"prescription_item_id": draft["items"][0]["id"]}],
        }, format="json")
        # The draft is not visible to this patient at all.
        assert res.status_code == 404

    def test_a_request_for_an_unstocked_medication_is_refused(self, client, cast):
        prescription = write_prescription(client, cast, quantity=5)
        as_user(client, cast["patient_token"])
        res = client.post("/api/pharmacy/requests/", {
            "pharmacy_id": cast["pharmacy_a"].id,
            "prescription_id": prescription["id"],
            "items": [{"prescription_item_id": prescription["items"][0]["id"]}],
        }, format="json")
        assert res.status_code == 400
        assert "does not stock" in res.data["error"]

    def test_an_over_the_counter_request_needs_no_prescription(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"], quantity=10)
        as_user(client, cast["patient_token"])
        res = client.post("/api/pharmacy/requests/", {
            "pharmacy_id": cast["pharmacy_a"].id,
            "items": [{"medication_id": cast["medication"].id, "quantity": 2}],
        }, format="json")
        assert res.status_code == 201, res.data
        # Recorded honestly as having no prescription behind it.
        assert res.data["prescription"] is None

    def test_a_pharmacy_cannot_create_a_request(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"])
        as_user(client, cast["pharmacy_a_token"])
        res = client.post("/api/pharmacy/requests/", {
            "pharmacy_id": cast["pharmacy_a"].id,
            "items": [{"medication_id": cast["medication"].id}],
        }, format="json")
        assert res.status_code == 403

    def test_an_anonymous_caller_can_do_nothing(self, client, cast):
        client.credentials()
        assert client.get("/api/pharmacy/requests/").status_code == 401
        assert client.get("/api/pharmacy/inventory/").status_code == 401
        assert client.get("/api/pharmacy/prescriptions/").status_code == 401
        assert client.get(
            f"/api/pharmacy/availability/?medication={cast['medication'].id}"
        ).status_code == 401


# ---------------------------------------------------------------------------
# Overselling — the guarantee, proved against PostgreSQL
# ---------------------------------------------------------------------------
class TestNoOverselling:
    def test_the_database_refuses_more_reserved_than_stocked(self, cast):
        """Written directly through the ORM, bypassing every service check.

        This is the point of the check constraint: if the reservation logic is
        ever wrong, the shelf still cannot go negative.
        """
        line = stock(cast["pharmacy_a"], cast["medication"], quantity=5)
        line.reserved = 6
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                line.save(update_fields=["reserved"])

    def test_reserving_exactly_the_shelf_is_allowed(self, cast):
        line = stock(cast["pharmacy_a"], cast["medication"], quantity=5)
        line.reserved = 5
        line.save(update_fields=["reserved"])
        line.refresh_from_db()
        assert line.available_quantity == 0

    def test_two_requests_cannot_both_reserve_the_same_units(self, client, cast):
        """Stock 5; requests for 4 and 3. Only one can be confirmed."""
        line = stock(cast["pharmacy_a"], cast["medication"], quantity=5)

        # Both submit — asking is free, and neither holds stock yet.
        as_user(client, cast["patient_token"])
        first = client.post("/api/pharmacy/requests/", {
            "pharmacy_id": cast["pharmacy_a"].id,
            "items": [{"medication_id": cast["medication"].id, "quantity": 4}],
        }, format="json")
        assert first.status_code == 201, first.data

        other, other_token = make(roles.PATIENT, "second_patient")
        as_user(client, other_token)
        second = client.post("/api/pharmacy/requests/", {
            "pharmacy_id": cast["pharmacy_a"].id,
            "items": [{"medication_id": cast["medication"].id, "quantity": 3}],
        }, format="json")
        assert second.status_code == 201, second.data

        # The pharmacy confirms the first. The second no longer fits.
        move(client, cast["pharmacy_a_token"], first.data["id"],
             MedicationRequest.CONFIRMED)
        refused = move(client, cast["pharmacy_a_token"], second.data["id"],
                       MedicationRequest.CONFIRMED, expect=409)
        assert refused.data["details"]["available"] == 1
        assert refused.data["details"]["requested"] == 3

        line.refresh_from_db()
        assert line.reserved == 4
        assert MedicationRequest.objects.get(
            pk=second.data["id"]).status == MedicationRequest.PENDING

    def test_stock_cannot_be_set_below_what_is_reserved(self, client, cast):
        line = stock(cast["pharmacy_a"], cast["medication"], quantity=10)
        prescription = write_prescription(client, cast, quantity=8)
        created = submit_request(client, cast, prescription)
        move(client, cast["pharmacy_a_token"], created.data["id"],
             MedicationRequest.CONFIRMED)

        as_user(client, cast["pharmacy_a_token"])
        res = client.patch(f"/api/pharmacy/inventory/{line.id}/",
                           {"quantity": 3}, format="json")
        assert res.status_code == 409
        line.refresh_from_db()
        assert line.quantity == 10

    def test_a_request_larger_than_the_shelf_is_refused_at_submission(
            self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"], quantity=2)
        as_user(client, cast["patient_token"])
        res = client.post("/api/pharmacy/requests/", {
            "pharmacy_id": cast["pharmacy_a"].id,
            "items": [{"medication_id": cast["medication"].id, "quantity": 5}],
        }, format="json")
        assert res.status_code == 409
        assert res.data["details"]["available"] == 2


# ---------------------------------------------------------------------------
# Concurrency — two real threads, one shelf
# ---------------------------------------------------------------------------
@pytest.mark.django_db(transaction=True)
def test_concurrent_confirmations_cannot_oversell():
    """Section 47, run for real rather than argued.

    Two threads confirm two requests against a shelf of 5 — one for 4 units,
    one for 3. Exactly one may succeed. ``transaction=True`` is required: the
    threads need to see each other's committed work, which they cannot inside
    the wrapping transaction the normal fixture uses.
    """
    from pharmacy import services

    pharmacy, _token = make(roles.PHARMACY, "concurrent_pharmacy")
    patient_one, _t1 = make(roles.PATIENT, "concurrent_one")
    patient_two, _t2 = make(roles.PATIENT, "concurrent_two")
    med = medication(name="Metformin", strength="850 mg",
                     form=dosage_forms.TABLET, generic="Metformin")
    line = stock(pharmacy, med, quantity=5)

    first = services.create_request(
        patient_one, pharmacy.id,
        [{"medication_id": med.id, "quantity": 4}])
    second = services.create_request(
        patient_two, pharmacy.id,
        [{"medication_id": med.id, "quantity": 3}])

    outcomes = {}
    barrier = threading.Barrier(2)

    def confirm(label, request_id):
        try:
            barrier.wait(timeout=10)
            services.transition_request(pharmacy, request_id,
                                        MedicationRequest.CONFIRMED)
            outcomes[label] = "ok"
        except Exception as exc:                      # noqa: BLE001 - recorded
            outcomes[label] = type(exc).__name__
        finally:
            # Each thread owns its own connection; leaving it open hangs the
            # test database teardown.
            connection.close()

    threads = [threading.Thread(target=confirm, args=("first", first.id)),
               threading.Thread(target=confirm, args=("second", second.id))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    succeeded = [label for label, result in outcomes.items() if result == "ok"]
    assert len(succeeded) == 1, outcomes

    line.refresh_from_db()
    # Whichever won, the shelf holds exactly that reservation and no more.
    assert line.reserved in (3, 4)
    assert line.reserved <= line.quantity
    statuses = set(MedicationRequest.objects.filter(
        pk__in=[first.id, second.id]).values_list("status", flat=True))
    assert statuses == {MedicationRequest.CONFIRMED, MedicationRequest.PENDING}

    # Clean up: transaction=True leaves real rows behind for the next test.
    MedicationRequestItem.objects.all().delete()
    MedicationRequest.objects.all().delete()
    PharmacyInventory.objects.all().delete()
    Medication.objects.all().delete()
    from django.contrib.auth.models import User
    User.objects.filter(username__startswith="concurrent_").delete()


# ---------------------------------------------------------------------------
# Isolation between patients, and between pharmacies
# ---------------------------------------------------------------------------
class TestIsolation:
    def test_a_patient_cannot_read_another_patients_prescription(
            self, client, cast):
        written = write_prescription(client, cast)
        _other, other_token = make(roles.PATIENT, "nosy_patient")
        as_user(client, other_token)
        assert client.get("/api/pharmacy/prescriptions/").data == []
        assert client.get(
            f"/api/pharmacy/prescriptions/{written['id']}/").status_code == 404
        assert client.get(
            f"/api/pharmacy/prescriptions/{written['id']}/pharmacies/"
        ).status_code == 404

    def test_a_patient_cannot_read_another_patients_request(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"])
        prescription = write_prescription(client, cast, quantity=3)
        created = submit_request(client, cast, prescription)
        _other, other_token = make(roles.PATIENT, "nosy_patient_two")
        as_user(client, other_token)
        assert client.get("/api/pharmacy/requests/").data == []
        assert client.get(
            f"/api/pharmacy/requests/{created.data['id']}/").status_code == 404

    def test_a_patient_cannot_move_another_patients_request(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"])
        prescription = write_prescription(client, cast, quantity=3)
        created = submit_request(client, cast, prescription)
        _other, other_token = make(roles.PATIENT, "nosy_patient_three")
        move(client, other_token, created.data["id"],
             MedicationRequest.CANCELLED, expect=404)

    def test_a_pharmacy_cannot_see_another_pharmacys_requests(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"])
        prescription = write_prescription(client, cast, quantity=3)
        created = submit_request(client, cast, prescription)

        as_user(client, cast["pharmacy_b_token"])
        assert client.get("/api/pharmacy/requests/").data == []
        assert client.get(
            f"/api/pharmacy/requests/{created.data['id']}/").status_code == 404
        move(client, cast["pharmacy_b_token"], created.data["id"],
             MedicationRequest.CONFIRMED, expect=404)

    def test_a_pharmacy_is_never_given_a_prescription_queryset(self, client, cast):
        """Section 27: filling a request is not access to the medical record."""
        write_prescription(client, cast)
        as_user(client, cast["pharmacy_a_token"])
        res = client.get("/api/pharmacy/prescriptions/")
        assert res.status_code == 200
        assert res.data == []

    def test_a_pharmacy_sees_the_requested_lines_and_a_reference_only(
            self, client, cast):
        """The pharmacy learns what to dispense — not the rest of the record."""
        second = medication(name="Vitamin D", strength="1000 IU",
                            form=dosage_forms.TABLET, generic="Cholecalciferol")
        stock(cast["pharmacy_a"], cast["medication"], quantity=50)

        as_user(client, cast["doctor_token"])
        written = client.post("/api/pharmacy/prescriptions/", {
            "patient_id": cast["patient"].id,
            "diagnosis": "Confidential clinical detail",
            "notes": "Private prescriber note",
            "items": [{"medication_id": cast["medication"].id, "quantity": 21,
                       "dosage": "1 capsule", "frequency": "3 times/day"},
                      {"medication_id": second.id, "quantity": 10}],
        }, format="json")
        assert written.status_code == 201

        # The patient fills only the first line here.
        as_user(client, cast["patient_token"])
        created = client.post("/api/pharmacy/requests/", {
            "pharmacy_id": cast["pharmacy_a"].id,
            "prescription_id": written.data["id"],
            "items": [{"prescription_item_id": written.data["items"][0]["id"]}],
        }, format="json")
        assert created.status_code == 201, created.data

        as_user(client, cast["pharmacy_a_token"])
        res = client.get(f"/api/pharmacy/requests/{created.data['id']}/")
        assert res.status_code == 200
        body = str(res.data)

        # What it needs: the line, the dosing to label the pack, the patient,
        # and enough of a reference to check the prescription is real.
        assert res.data["items"][0]["medication"]["name"] == "Amoxicillin"
        assert res.data["items"][0]["dosage"] == "1 capsule"
        assert res.data["prescription_reference"]["id"] == written.data["id"]
        assert res.data["prescription_reference"]["prescribed_by"]

        # What it must not get: the prescription's other medications, the
        # diagnosis, or the prescriber's private notes.
        assert "Vitamin D" not in body
        assert "Confidential clinical detail" not in body
        assert "Private prescriber note" not in body

    def test_a_doctor_is_not_shown_dispensing_activity(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"])
        prescription = write_prescription(client, cast, quantity=3)
        submit_request(client, cast, prescription)
        as_user(client, cast["doctor_token"])
        assert client.get("/api/pharmacy/requests/").data == []

    def test_an_admin_is_not_given_clinical_content(self, client):
        """Administering the platform is not a reason to read prescriptions."""
        from django.contrib.auth.models import User
        from rest_framework.authtoken.models import Token
        from accounts.models import UserAccount

        admin = User.objects.create_user("rx_admin", password=PW)
        UserAccount.objects.create(user=admin, role=roles.ADMIN)
        token = Token.objects.create(user=admin)
        as_user(client, token.key)
        assert client.get("/api/pharmacy/prescriptions/").data == []
        assert client.get("/api/pharmacy/requests/").data == []

    def test_a_radiology_account_gets_nothing_from_the_pharmacy_api(
            self, client, cast):
        _center, center_token = make(roles.RADIOLOGY, "unrelated_centre")
        as_user(client, center_token)
        assert client.get("/api/pharmacy/prescriptions/").data == []
        assert client.get("/api/pharmacy/requests/").data == []
        assert client.get("/api/pharmacy/inventory/").status_code == 403


# ---------------------------------------------------------------------------
# Dashboards, notifications and performance
# ---------------------------------------------------------------------------
class TestDashboardAndPerformance:
    def test_the_pharmacy_dashboard_reports_real_figures(self, client, cast):
        stock(cast["pharmacy_a"], cast["medication"], quantity=30,
              low_stock_threshold=40)
        prescription = write_prescription(client, cast, quantity=5)
        submit_request(client, cast, prescription)

        as_user(client, cast["pharmacy_a_token"])
        res = client.get("/api/dashboard/summary/")
        assert res.status_code == 200
        stats = res.data["stats"]
        assert stats["pending_orders"] == 1
        assert stats["inventory_items"] == 1
        assert stats["units_in_stock"] == 30
        assert stats["low_stock_items"] == 1
        # The tiles that now have a real source stop being reported as absent.
        assert "pending_orders" not in res.data["unsupported_metrics"]

    def test_active_prescriptions_stops_being_an_unsupported_metric(
            self, client, cast):
        """The patient dashboard has reported this as 'not tracked' until now."""
        write_prescription(client, cast)
        as_user(client, cast["patient_token"])
        res = client.get("/api/dashboard/summary/")
        assert res.data["stats"]["active_prescriptions"] == 1
        assert "active_prescriptions" not in res.data["unsupported_metrics"]
        assert res.data["pharmacy"]["prescribed_medications"] == 1

    def test_the_doctor_dashboard_counts_their_own_prescribing(self, client, cast):
        write_prescription(client, cast)
        write_prescription(client, cast, issue=False)
        as_user(client, cast["doctor_token"])
        res = client.get("/api/dashboard/summary/")
        assert res.data["pharmacy"]["prescriptions_written"] == 2
        assert res.data["pharmacy"]["prescriptions_issued"] == 1

    def test_listing_requests_does_not_scale_with_their_number(
            self, client, cast, django_assert_max_num_queries):
        """The list joins request → items → medication → prescription → doctor."""
        stock(cast["pharmacy_a"], cast["medication"], quantity=200)
        for index in range(6):
            med = medication(name=f"Drug {index}", strength="10 mg",
                             form=dosage_forms.TABLET, generic=f"Generic {index}")
            stock(cast["pharmacy_a"], med, quantity=50)
            as_user(client, cast["patient_token"])
            res = client.post("/api/pharmacy/requests/", {
                "pharmacy_id": cast["pharmacy_a"].id,
                "items": [{"medication_id": med.id, "quantity": 2}],
            }, format="json")
            assert res.status_code == 201, res.data

        as_user(client, cast["pharmacy_a_token"])
        with django_assert_max_num_queries(12):
            listed = client.get("/api/pharmacy/requests/")
        assert len(listed.data) == 6

    def test_availability_search_does_not_scale_with_pharmacy_count(
            self, client, cast, django_assert_max_num_queries):
        for index in range(6):
            extra, _token = make(roles.PHARMACY, f"bulk_pharmacy_{index}")
            stock(extra, cast["medication"], quantity=index + 1)
        as_user(client, cast["patient_token"])
        with django_assert_max_num_queries(8):
            res = client.get(
                f"/api/pharmacy/availability/?medication={cast['medication'].id}")
        assert len(res.data) == 6
