"""The AI copilot's tools: real data, the caller's own, nothing written by surprise.

The assistant used to answer "I don't have access to that" about data Roshada
was holding. It now calls the same services the user's own pages call. This
suite covers the four things that would hurt if they were wrong:

* **It reads real data, scoped to the caller.** No tool takes an identifier for
  a person, so a patient cannot ask about another patient — the question cannot
  be expressed, not merely refused.
* **A doctor reaches only their own patients.** The care-relationship rule that
  gates prescribing and records gates this too.
* **Nothing is written without agreement.** Proved against a model that tries to
  self-confirm, to substitute a different action, and to reuse a token.
* **It degrades rather than failing.** A provider that cannot call tools still
  answers.

The model is scripted at the ``llm.converse`` seam, so the agent loop, the
executor and the services under it are all real. No credential is used.
"""
import datetime
import json

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts import roles
from accounts.services import register_account
from appointments.models import Appointment
from appointments.services import care, chat
from appointments.services.ai import agent, grounding, llm as llm_module, pipeline
from appointments.services.ai import tools
from appointments.services.ai.providers.base import ChatResponse, ToolCall

pytestmark = pytest.mark.django_db

PW = "Str0ng!Passw0rd"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_throttling(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK,
                               "DEFAULT_THROTTLE_CLASSES": []}


@pytest.fixture(autouse=True)
def _tools_on(monkeypatch):
    monkeypatch.setenv("AI_TOOLS", "on")
    # This suite is about tools; the knowledge path has its own.
    monkeypatch.setenv("AI_GROUNDING", "off")


def make(role, username, **extra):
    defaults = {
        roles.PATIENT: {"age": 40},
        roles.DOCTOR: {"specialization": "Cardiology"},
        roles.PHARMACY: {"services": "Dispensing"},
        roles.LABORATORY: {"services": "CBC"},
        roles.RADIOLOGY: {"services": "MRI"},
    }[role]
    fields = {"name": username.replace("_", " ").title(), **defaults, **extra}
    user, _account, _profile, token = register_account(
        role, username=username, password=PW, **fields)
    return user, token.key


def prescribe(doctor, patient, medicine="Metformin", dosage="500 mg"):
    """One issued prescription, through the real pharmacy service."""
    from pharmacy import services as pharmacy

    medication, _created = pharmacy.find_or_create_medication(
        doctor, medicine, strength=dosage)
    return pharmacy.create_prescription(
        doctor, patient.pk,
        [{"medication_id": medication.id, "dosage": dosage, "quantity": 30}],
        diagnosis="Type 2 diabetes")


def book(patient, provider, days=3, time="10:00", minutes=30):
    """One appointment, created the way the model actually stores them.

    ``date``/``time`` are read-only views over ``start_at``; the engine owns the
    period, so a test that set the parts would not be storing what production
    stores.
    """
    from django.utils import timezone

    from appointments.services import availability

    # localdate, not date.today(): the day-bounds query the services use is
    # timezone-aware, and "tomorrow" has to mean the same day to both.
    start = availability.combine(
        timezone.localdate() + datetime.timedelta(days=days),
        datetime.time.fromisoformat(time))
    return Appointment.objects.create(
        patient=patient, provider=provider, start_at=start,
        end_at=start + datetime.timedelta(minutes=minutes),
        reason="check-up")


class ScriptedModel:
    """A model that asks for the tool calls you give it, then answers.

    Patched over ``llm.converse``, which is the one seam the agent loop uses,
    so everything below it — the executor, the authorization, the services — is
    the real thing.
    """

    def __init__(self, *rounds, final="Here is what I found."):
        self.rounds = list(rounds)
        self.final = final
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.rounds:
            calls = self.rounds.pop(0)
            return ChatResponse(
                text="", provider="scripted", model="scripted-1",
                tool_calls=tuple(
                    ToolCall(id=f"c{i}", name=n,
                             arguments=json.dumps(a, ensure_ascii=False))
                    for i, (n, a) in enumerate(calls)))
        return ChatResponse(text=self.final, provider="scripted",
                            model="scripted-1")

    @property
    def tool_results(self):
        """Every tool result that was fed back, decoded."""
        out = []
        for request in self.requests:
            for message in request.messages:
                if message.role == "tool":
                    out.append(json.loads(message.content))
        return out


def script(monkeypatch, *rounds, final="Here is what I found."):
    model = ScriptedModel(*rounds, final=final)
    monkeypatch.setattr(llm_module, "converse", model)
    monkeypatch.setattr(llm_module, "supports_tools", lambda *a, **k: True)
    return model


def run(user, message, monkeypatch, *rounds, final="Here is what I found."):
    model = script(monkeypatch, *rounds, final=final)
    return pipeline.ask(user, message), model


# ---------------------------------------------------------------------------
# Section 2 — the role comes from the backend
# ---------------------------------------------------------------------------
class TestRoleAwareness:
    def test_the_role_is_read_from_the_account_not_guessed(self):
        for role in (roles.PATIENT, roles.DOCTOR, roles.LABORATORY,
                     roles.RADIOLOGY, roles.PHARMACY):
            user, _token = make(role, f"role_{role}")
            assert tools.role_of(user) == role

    def test_a_facility_is_no_longer_mistaken_for_a_patient(self):
        """The old check sniffed for a doctor_profile and defaulted to patient."""
        from appointments.services.ai import context as context_module

        lab, _token = make(roles.LABORATORY, "role_lab_ctx")
        assert context_module.role_of(lab) == roles.LABORATORY

    def test_nothing_the_caller_sends_can_change_their_role(self):
        patient, token = make(roles.PATIENT, "role_spoof")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        # Even if the body claims otherwise, the role comes from the record.
        client.post("/api/chat/ask/",
                    {"message": "hello", "role": "doctor",
                     "user_id": 1, "patient_id": 1}, format="json")
        assert tools.role_of(patient) == roles.PATIENT


# ---------------------------------------------------------------------------
# Sections 3 and 5 — which tools each role holds
# ---------------------------------------------------------------------------
class TestCatalogue:
    REQUIRED_PATIENT = {"search_doctors", "get_doctor_availability",
                        "get_patient_appointments", "get_patient_lab_results",
                        "get_patient_radiology_reports",
                        "get_patient_prescriptions",
                        "search_pharmacy_availability"}
    REQUIRED_DOCTOR = {"get_doctor_appointments",
                       "search_doctor_patient_appointments",
                       "get_doctor_schedule"}

    def test_the_patient_holds_every_required_tool(self):
        assert self.REQUIRED_PATIENT <= set(tools.names_for(roles.PATIENT))

    def test_the_doctor_holds_every_required_tool(self):
        assert self.REQUIRED_DOCTOR <= set(tools.names_for(roles.DOCTOR))

    def test_a_patient_holds_no_doctor_tool(self):
        held = set(tools.names_for(roles.PATIENT))
        assert not (held & self.REQUIRED_DOCTOR)

    def test_a_doctor_holds_no_patient_record_tool(self):
        held = set(tools.names_for(roles.DOCTOR))
        assert "get_patient_prescriptions" not in held
        assert "get_patient_radiology_reports" not in held

    @pytest.mark.parametrize("role", [roles.LABORATORY, roles.RADIOLOGY,
                                      roles.PHARMACY, roles.ADMIN])
    def test_roles_without_an_ai_surface_hold_no_tools(self, role):
        assert tools.names_for(role) == []

    def test_every_tool_declares_a_description_and_a_schema(self):
        for name, tool_ in tools.REGISTRY.items():
            assert tool_.description, name
            assert tool_.parameters.get("type") == "object", name
            assert tool_.roles, name


# ---------------------------------------------------------------------------
# Section 6 — the model cannot choose whose data it reads
# ---------------------------------------------------------------------------
class TestIdentityIsNeverTheModelsChoice:
    def test_no_tool_accepts_an_identifier_for_a_person(self):
        """Section 6: no arbitrary patient_id or doctor_id.

        A provider id is allowed — the directory is public to signed-in users —
        but nothing that names a *person whose data would be read*.
        """
        forbidden = {"patient_id", "user_id", "patient_user_id", "account_id",
                     "username", "doctor_id"}
        for name, tool_ in tools.REGISTRY.items():
            declared = set((tool_.parameters or {}).get("properties") or {})
            assert not (declared & forbidden), f"{name} accepts {declared & forbidden}"

    def test_an_invented_identifier_is_discarded(self):
        alice, _ = make(roles.PATIENT, "id_alice")
        bob, _ = make(roles.PATIENT, "id_bob")
        doctor, _ = make(roles.DOCTOR, "id_doc")
        book(bob, doctor)

        result = tools.execute(alice, "get_patient_appointments",
                               json.dumps({"patient_id": bob.pk}))
        assert result["ok"] is True
        # Alice's own appointments — she has none. Bob's are untouched.
        assert result["count"] == 0

    def test_a_tool_the_role_lacks_is_refused(self):
        patient, _ = make(roles.PATIENT, "id_wrongrole")
        result = tools.execute(patient, "get_doctor_appointments")
        assert result["ok"] is False
        assert "not allowed" in result["error"]

    def test_an_unknown_tool_is_reported_not_invented(self):
        patient, _ = make(roles.PATIENT, "id_unknown")
        result = tools.execute(patient, "read_all_patient_records")
        assert result["ok"] is False
        assert "no tool called" in result["error"].lower()

    def test_malformed_arguments_are_reported(self):
        patient, _ = make(roles.PATIENT, "id_badjson")
        result = tools.execute(patient, "get_patient_appointments", "{not json")
        assert result["ok"] is False
        assert "JSON" in result["error"]

    def test_no_tool_handler_takes_raw_sql_or_a_queryset(self):
        import inspect

        for name, tool_ in tools.REGISTRY.items():
            source = inspect.getsource(tool_.handler)
            for forbidden in ("raw(", "cursor(", "extra(", "RawSQL"):
                assert forbidden not in source, f"{name} uses {forbidden}"


# ---------------------------------------------------------------------------
# Section 3 — the patient's own data, and only theirs
# ---------------------------------------------------------------------------
class TestPatientAccess:
    def test_appointments_are_the_callers_own(self):
        patient, _ = make(roles.PATIENT, "pa_own")
        other, _ = make(roles.PATIENT, "pa_other")
        doctor, _ = make(roles.DOCTOR, "pa_doc")
        book(patient, doctor, days=2)
        book(other, doctor, days=4)

        result = tools.execute(patient, "get_patient_appointments")
        assert result["count"] == 1
        assert all("pa_other" not in json.dumps(a)
                   for a in result["appointments"])

    def test_doctors_can_be_searched_by_specialisation(self):
        patient, _ = make(roles.PATIENT, "pa_search")
        make(roles.DOCTOR, "pa_cardio", specialization="Cardiology")
        make(roles.DOCTOR, "pa_derm", specialization="Dermatology")

        result = tools.execute(patient, "search_doctors",
                               json.dumps({"specialization": "cardio"}))
        names = [d["specialization"] for d in result["doctors"]]
        assert names and all("cardio" in n.lower() for n in names)

    def test_a_doctor_who_does_not_exist_is_reported_not_invented(self):
        patient, _ = make(roles.PATIENT, "pa_nodoc")
        result = tools.execute(patient, "search_doctors",
                               json.dumps({"name": "Nobody At All"}))
        assert result["doctors"] == []
        assert "no doctor" in result["note"].lower()

    def test_availability_for_an_unknown_provider_is_an_error_not_a_guess(self):
        patient, _ = make(roles.PATIENT, "pa_badprov")
        result = tools.execute(patient, "get_doctor_availability",
                               json.dumps({"doctor_user_id": 999999}))
        assert result["ok"] is False
        assert "no such provider" in result["error"].lower()

    def test_lab_results_say_roshada_has_no_lab_module(self):
        """Honesty about a gap beats an empty list that reads as 'all clear'."""
        patient, _ = make(roles.PATIENT, "pa_lab")
        result = tools.execute(patient, "get_patient_lab_results")
        assert result["results"] == []
        assert "does not have a laboratory results module" in result["note"]

    def test_prescriptions_come_back_with_their_medicines(self):
        patient, _ = make(roles.PATIENT, "pa_rx")
        doctor, _ = make(roles.DOCTOR, "pa_rxdoc")
        book(patient, doctor, days=-1)
        prescribe(doctor, patient)

        result = tools.execute(patient, "get_patient_prescriptions")
        assert result["count"] == 1
        assert result["prescriptions"][0]["medicines"][0]["name"]

    def test_a_patient_never_sees_another_patients_prescriptions(self):
        alice, _ = make(roles.PATIENT, "pa_alice")
        bob, _ = make(roles.PATIENT, "pa_bob")
        doctor, _ = make(roles.DOCTOR, "pa_bothdoc")
        book(bob, doctor, days=-1)
        prescribe(doctor, bob, medicine="SENTINEL-DRUG")

        result = tools.execute(alice, "get_patient_prescriptions")
        assert "SENTINEL-DRUG" not in json.dumps(result)
        assert result["count"] == 0

    def test_radiology_reports_come_back_released_only(self):
        """A draft report is not a clinical document and must not surface."""
        import datetime as _dt

        from django.utils import timezone as _tz

        from appointments.models import Service
        from radiology import modalities
        from radiology import services as radiology
        from radiology.models import Examination, RadiologyReport

        patient, _ = make(roles.PATIENT, "pa_rad")
        centre, _ = make(roles.RADIOLOGY, "pa_radcentre")
        service = Service.objects.create(provider=centre, name="MRI Brain",
                                         category=modalities.MRI,
                                         duration_minutes=60)
        start = _tz.now() - _dt.timedelta(days=2)
        booking = Appointment.objects.create(
            provider=centre, patient=patient, start_at=start,
            end_at=start + _dt.timedelta(minutes=60), service=service)
        examination = Examination.objects.create(
            appointment=booking, status=Examination.COMPLETED)
        report = RadiologyReport.objects.create(
            examination=examination, author=centre,
            impression="SENTINEL-IMPRESSION", status=RadiologyReport.DRAFT)

        # Still a draft: nothing to show.
        assert tools.execute(patient, "get_patient_radiology_reports")["count"] == 0

        for step in (RadiologyReport.PENDING_REVIEW, RadiologyReport.VERIFIED,
                     RadiologyReport.RELEASED):
            radiology.transition_report(centre, report.id, step)

        result = tools.execute(patient, "get_patient_radiology_reports")
        assert result["count"] == 1
        assert result["reports"][0]["impression"] == "SENTINEL-IMPRESSION"
        assert result["reports"][0]["study"] == "MRI Brain"

    def test_pharmacy_stock_is_looked_up_for_real(self):
        from pharmacy import services as pharmacy

        patient, _ = make(roles.PATIENT, "pa_pharm")
        chemist, _ = make(roles.PHARMACY, "pa_chemist")
        medication, _created = pharmacy.find_or_create_medication(
            chemist, "Metformin", strength="850 mg")
        pharmacy.upsert_inventory(chemist, medication.id, quantity=20,
                                  price="55.00")

        result = tools.execute(patient, "search_pharmacy_availability",
                               json.dumps({"medication": "Metformin"}))
        assert result["pharmacies"]
        assert result["pharmacies"][0]["price"] == "55.00"

    def test_a_medication_roshada_does_not_stock_is_reported_honestly(self):
        patient, _ = make(roles.PATIENT, "pa_nostock")
        result = tools.execute(patient, "search_pharmacy_availability",
                               json.dumps({"medication": "Unobtainium"}))
        assert result["pharmacies"] == []
        assert "does not have that medication" in result["note"]

# ---------------------------------------------------------------------------
# Section 4 — the doctor's own practice
# ---------------------------------------------------------------------------
class TestDoctorAccess:
    def test_a_named_patient_of_theirs_is_found_with_real_appointments(self):
        doctor, _ = make(roles.DOCTOR, "da_doc")
        patient, _ = make(roles.PATIENT, "da_ahmed", name="Ahmed Salah")
        book(patient, doctor, days=1)

        result = tools.execute(doctor, "search_doctor_patient_appointments",
                               json.dumps({"patient_name": "Ahmed"}))
        assert result["found"] is True
        assert result["matches"][0]["appointments"][0]["date"]

    def test_an_unrelated_patient_is_not_found(self):
        """Section 4: a doctor must not reach patients they do not treat."""
        doctor, _ = make(roles.DOCTOR, "da_mine")
        other_doctor, _ = make(roles.DOCTOR, "da_theirs")
        stranger, _ = make(roles.PATIENT, "da_stranger", name="Sara Nour")
        book(stranger, other_doctor, days=1)

        result = tools.execute(doctor, "search_doctor_patient_appointments",
                               json.dumps({"patient_name": "Sara"}))
        assert result["found"] is False
        assert result["matches"] == []

    def test_the_answer_is_identical_for_unknown_and_not_mine(self):
        """Otherwise the reply confirms that a patient exists elsewhere."""
        doctor, _ = make(roles.DOCTOR, "da_probe")
        other_doctor, _ = make(roles.DOCTOR, "da_probe2")
        real, _ = make(roles.PATIENT, "da_real", name="Mona Adel")
        book(real, other_doctor)

        exists = tools.execute(doctor, "search_doctor_patient_appointments",
                               json.dumps({"patient_name": "Mona"}))
        invented = tools.execute(doctor, "search_doctor_patient_appointments",
                                 json.dumps({"patient_name": "Zzzz"}))
        assert exists["found"] == invented["found"] is False
        assert exists["matches"] == invented["matches"] == []

    def test_the_days_list_is_the_doctors_own(self):
        doctor, _ = make(roles.DOCTOR, "da_sched")
        colleague, _ = make(roles.DOCTOR, "da_colleague")
        patient, _ = make(roles.PATIENT, "da_schedpt")
        book(patient, colleague, days=1)

        result = tools.execute(doctor, "get_doctor_appointments",
                               json.dumps({"date": "tomorrow"}))
        assert result["count"] == 0

    def test_tomorrow_is_understood(self):
        doctor, _ = make(roles.DOCTOR, "da_tomorrow")
        patient, _ = make(roles.PATIENT, "da_tompt", name="Nour Ali")
        book(patient, doctor, days=1)

        result = tools.execute(doctor, "get_doctor_appointments",
                               json.dumps({"date": "tomorrow"}))
        assert result["count"] == 1
        assert result["appointments"][0]["patient"] == "Nour Ali"

    def test_an_unreadable_date_is_explained(self):
        doctor, _ = make(roles.DOCTOR, "da_baddate")
        result = tools.execute(doctor, "get_doctor_appointments",
                               json.dumps({"date": "next thursdayish"}))
        assert result["ok"] is False
        assert "YYYY-MM-DD" in result["error"]

    def test_the_care_relationship_rule_is_the_shared_one(self):
        """The same rule that gates prescribing and records, not a copy."""
        doctor, _ = make(roles.DOCTOR, "da_rule")
        patient, _ = make(roles.PATIENT, "da_rulept")
        assert care.treats_patient(doctor, patient) is False
        book(patient, doctor)
        assert care.treats_patient(doctor, patient) is True


# ---------------------------------------------------------------------------
# Section 7 — nothing is written without confirmation
# ---------------------------------------------------------------------------
class TestConfirmationGate:
    """Section 7: never book automatically.

    The gate turns on the person's own next message, so every test here runs two
    turns — the assistant proposes, the person answers — which is the shape the
    guarantee actually has.
    """

    def _bookable(self):
        patient, _ = make(roles.PATIENT, "cg_patient")
        doctor, _ = make(roles.DOCTOR, "cg_doctor")
        when = timezone.localdate() + datetime.timedelta(days=2)
        return patient, doctor, when

    def _propose(self, user, tool_name, arguments, message="please"):
        """Turn one: the assistant proposes, and the proposal is recorded."""
        result = tools.execute(user, tool_name, json.dumps(arguments),
                               message=message)
        pending = result.pop("_pending", None)
        chat.record_exchange(user, message, "Shall I go ahead?",
                             pending_action=pending)
        return result

    def _confirm(self, user, tool_name, arguments, message="yes"):
        """Turn two: the person answers."""
        return tools.execute(user, tool_name,
                             json.dumps({**arguments, "confirm": True}),
                             message=message)

    def test_the_first_call_books_nothing(self):
        patient, doctor, when = self._bookable()
        result = self._propose(patient, "book_appointment", {
            "provider_user_id": doctor.pk, "date": when.isoformat(),
            "time": "10:00"}, message="book me in")

        assert result["confirmation_required"] is True
        assert result["executed"] is False
        assert result["summary"]
        assert Appointment.objects.count() == 0

    def test_a_model_cannot_confirm_in_the_same_turn_it_proposed(self):
        """No proposal has reached the person yet, so nothing can be agreed."""
        patient, doctor, when = self._bookable()
        result = tools.execute(patient, "book_appointment", json.dumps({
            "provider_user_id": doctor.pk, "date": when.isoformat(),
            "time": "10:00", "confirm": True}), message="book me in")

        assert result.get("executed") is not True
        assert Appointment.objects.count() == 0

    def test_a_request_to_book_is_not_itself_agreement(self):
        """A request for a booking must not read as confirmation of one."""
        assert tools.is_affirmative("احجزلي معاد بكرة") is False
        assert tools.is_affirmative("book it for me") is False

    def test_agreement_to_one_slot_cannot_book_another(self):
        """The bait-and-switch: agreed for Tuesday, executed for Wednesday."""
        patient, doctor, when = self._bookable()
        self._propose(patient, "book_appointment", {
            "provider_user_id": doctor.pk, "date": when.isoformat(),
            "time": "10:00"})

        other = (when + datetime.timedelta(days=1)).isoformat()
        result = self._confirm(patient, "book_appointment", {
            "provider_user_id": doctor.pk, "date": other, "time": "10:00"})

        assert result.get("executed") is not True
        assert Appointment.objects.count() == 0

    def test_agreement_to_book_cannot_cancel(self):
        patient, doctor, when = self._bookable()
        appointment = book(patient, doctor, days=5)
        self._propose(patient, "book_appointment", {
            "provider_user_id": doctor.pk, "date": when.isoformat(),
            "time": "10:00"})

        result = self._confirm(patient, "cancel_appointment",
                               {"appointment_id": appointment.id})

        assert result.get("executed") is not True
        appointment.refresh_from_db()
        assert appointment.status != Appointment.CANCELLED

    def test_a_proposal_without_agreement_does_nothing(self):
        patient, doctor, when = self._bookable()
        arguments = {"provider_user_id": doctor.pk, "date": when.isoformat(),
                     "time": "10:00"}
        self._propose(patient, "book_appointment", arguments)
        result = self._confirm(patient, "book_appointment", arguments,
                               message="actually no, not yet")

        assert result.get("executed") is not True
        assert Appointment.objects.count() == 0

    def test_a_proposed_and_agreed_booking_is_made(self):
        patient, doctor, when = self._bookable()
        arguments = {"provider_user_id": doctor.pk, "date": when.isoformat(),
                     "time": "10:00"}
        self._propose(patient, "book_appointment", arguments,
                      message="book me with the cardiologist")
        confirmed = self._confirm(patient, "book_appointment", arguments,
                                  message="نعم")

        assert confirmed["executed"] is True
        assert Appointment.objects.filter(patient=patient).count() == 1

    def test_an_expired_proposal_no_longer_authorises_a_write(self):
        from appointments.models import ChatMessage

        patient, doctor, when = self._bookable()
        arguments = {"provider_user_id": doctor.pk, "date": when.isoformat(),
                     "time": "10:00"}
        self._propose(patient, "book_appointment", arguments)

        stale = timezone.now() - datetime.timedelta(
            minutes=chat.PENDING_ACTION_TTL_MINUTES + 5)
        ChatMessage.objects.filter(user=patient).update(created_at=stale)

        result = self._confirm(patient, "book_appointment", arguments)
        assert result.get("executed") is not True
        assert Appointment.objects.count() == 0

    def test_a_newer_proposal_supersedes_an_older_one(self):
        patient, doctor, when = self._bookable()
        first = {"provider_user_id": doctor.pk, "date": when.isoformat(),
                 "time": "10:00"}
        second = {**first, "time": "11:00"}
        self._propose(patient, "book_appointment", first)
        self._propose(patient, "book_appointment", second)

        # "yes" now answers the 11:00 proposal, not the 10:00 one.
        assert self._confirm(patient, "book_appointment",
                             first).get("executed") is not True
        assert self._confirm(patient, "book_appointment",
                             second)["executed"] is True

    def test_a_patient_cannot_cancel_someone_elses_appointment(self):
        alice, _ = make(roles.PATIENT, "cg_alice")
        bob, _ = make(roles.PATIENT, "cg_bob")
        doctor, _ = make(roles.DOCTOR, "cg_shared")
        theirs = book(bob, doctor, days=6)

        self._propose(alice, "cancel_appointment",
                      {"appointment_id": theirs.id}, message="cancel it")
        result = self._confirm(alice, "cancel_appointment",
                               {"appointment_id": theirs.id})

        assert result["ok"] is False
        theirs.refresh_from_db()
        assert theirs.status != Appointment.CANCELLED

    def test_one_persons_agreement_cannot_authorise_anothers_write(self):
        alice, _ = make(roles.PATIENT, "cg_crossa")
        bob, _ = make(roles.PATIENT, "cg_crossb")
        doctor, _ = make(roles.DOCTOR, "cg_crossdoc")
        when = timezone.localdate() + datetime.timedelta(days=2)
        arguments = {"provider_user_id": doctor.pk, "date": when.isoformat(),
                     "time": "10:00"}

        # Alice is asked; Bob says yes.
        self._propose(alice, "book_appointment", arguments)
        result = self._confirm(bob, "book_appointment", arguments)

        assert result.get("executed") is not True
        assert Appointment.objects.count() == 0

    @pytest.mark.parametrize("message,expected", [
        ("yes", True), ("نعم", True), ("أيوه", True), ("ok go ahead", True),
        ("تمام", True), ("that works", True),
        ("no", False), ("لا", False), ("not yet", False), ("", False),
        ("yes but not that one", False), ("what times are free?", False),
        ("احجز", False), ("book it", False),
    ])
    def test_agreement_is_recognised_in_both_languages(self, message, expected):
        assert tools.is_affirmative(message) is expected

    def test_a_proposal_authorises_exactly_one_write(self):
        """Agreement is spent when it is acted on, not left standing."""
        patient, doctor, when = self._bookable()
        arguments = {"provider_user_id": doctor.pk, "date": when.isoformat(),
                     "time": "10:00"}
        self._propose(patient, "book_appointment", arguments)
        assert self._confirm(patient, "book_appointment",
                             arguments)["executed"] is True
        assert chat.pending_action(patient) is None

        again = self._confirm(patient, "book_appointment", arguments)
        assert again.get("executed") is not True
        assert Appointment.objects.filter(patient=patient).count() == 1

    def test_the_whole_flow_runs_through_the_assistant(self, monkeypatch):
        """Two real turns: the model proposes, the person agrees, it books."""
        patient, doctor, when = self._bookable()
        arguments = {"provider_user_id": doctor.pk, "date": when.isoformat(),
                     "time": "10:00"}

        first, _model = run(patient, "احجزلي معاد بكرة", monkeypatch,
                            [("book_appointment", arguments)],
                            final="Shall I book 10:00?")
        assert Appointment.objects.count() == 0
        assert first.tools_used == ["book_appointment"]

        second, _model = run(patient, "نعم", monkeypatch,
                             [("book_appointment", {**arguments,
                                                    "confirm": True})],
                             final="Booked.")
        assert Appointment.objects.filter(patient=patient).count() == 1
        assert second.reply == "Booked."


# ---------------------------------------------------------------------------
# The agent loop
# ---------------------------------------------------------------------------
class TestAgentLoop:
    def test_a_tool_result_reaches_the_model(self, monkeypatch):
        patient, _ = make(roles.PATIENT, "al_patient")
        doctor, _ = make(roles.DOCTOR, "al_doctor")
        book(patient, doctor, days=3)

        result, model = run(patient, "when is my appointment?", monkeypatch,
                            [("get_patient_appointments", {})],
                            final="You have one appointment.")

        assert result.tools_used == ["get_patient_appointments"]
        assert model.tool_results[0]["count"] == 1
        assert result.reply == "You have one appointment."

    def test_several_tools_in_one_round_all_run(self, monkeypatch):
        patient, _ = make(roles.PATIENT, "al_multi")
        result, _model = run(
            patient, "what have I got?", monkeypatch,
            [("get_patient_appointments", {}), ("get_patient_prescriptions", {})])
        assert result.tools_used == ["get_patient_appointments",
                                     "get_patient_prescriptions"]

    def test_the_loop_is_bounded(self, monkeypatch):
        patient, _ = make(roles.PATIENT, "al_bounded")
        # More rounds than the limit allows.
        rounds = [[("get_patient_appointments", {})]] * 10
        model = script(monkeypatch, *rounds, final="Enough.")
        turn = agent.run(patient, "loop forever", role=roles.PATIENT)

        assert turn.exhausted is True
        assert len(turn.used) == agent.MAX_STEPS
        # The last call withholds tools, so the model has to answer.
        assert model.requests[-1].tool_choice == "none"

    def test_a_failing_tool_is_reported_to_the_model(self, monkeypatch):
        patient, _ = make(roles.PATIENT, "al_failing")
        _result, model = run(patient, "check", monkeypatch,
                             [("get_doctor_availability",
                               {"doctor_user_id": 999999})])
        assert model.tool_results[0]["ok"] is False

    def test_a_provider_without_tool_support_still_answers(self, monkeypatch):
        patient, _ = make(roles.PATIENT, "al_notools")
        monkeypatch.setattr(llm_module, "supports_tools", lambda *a, **k: False)
        monkeypatch.setattr(
            llm_module, "complete",
            lambda *a, **k: ChatResponse(text="A plain answer.",
                                         provider="plain", model="p-1"))

        result = pipeline.ask(patient, "when is my appointment?")
        assert result.reply == "A plain answer."
        assert result.tools_used == []

    def test_tools_can_be_switched_off(self, monkeypatch):
        patient, _ = make(roles.PATIENT, "al_off")
        monkeypatch.setenv("AI_TOOLS", "off")
        monkeypatch.setattr(llm_module, "supports_tools", lambda *a, **k: True)
        monkeypatch.setattr(
            llm_module, "complete",
            lambda *a, **k: ChatResponse(text="No tools here.",
                                         provider="plain", model="p-1"))

        assert agent.is_available(roles.PATIENT) is False
        assert pipeline.ask(patient, "hello").reply == "No tools here."

    def test_the_model_is_offered_only_this_roles_tools(self, monkeypatch):
        doctor, _ = make(roles.DOCTOR, "al_doconly")
        _result, model = run(doctor, "who is booked?", monkeypatch)
        offered = {t["function"]["name"] for t in model.requests[0].tools}
        assert offered == set(tools.names_for(roles.DOCTOR))
        assert "get_patient_prescriptions" not in offered

    def test_the_answer_still_passes_the_safety_check(self, monkeypatch):
        patient, _ = make(roles.PATIENT, "al_safety")
        result, _model = run(patient, "how much should I take?", monkeypatch,
                             final="Take 500 mg twice daily.")
        assert result.warnings or result.degraded


# ---------------------------------------------------------------------------
# Routing — data questions go to tools, medical ones to the knowledge base
# ---------------------------------------------------------------------------
class TestRouting:
    @pytest.mark.parametrize("message", [
        "مين الدكاترة المتاحين؟",
        "عندي معاد امتى؟",
        "فين الدواء اللي الدكتور كتبهولي؟",
        "مين المرضى اللي عندي بكرة؟",
        "هل أحمد حجز معايا؟",
        "ما هي أدويتي؟",
        "which doctors are available?",
        "what are my medications?",
    ])
    def test_the_briefs_questions_are_treated_as_roshada_data(self, message):
        assert grounding.looks_like_medical_question(message) is False

    @pytest.mark.parametrize("message", [
        "What is hypertension?",
        "ما هو ارتفاع ضغط الدم؟",
        "ما هي أعراض السكري",
    ])
    def test_general_medical_questions_still_go_to_the_knowledge_base(self, message):
        assert grounding.looks_like_medical_question(message) is True

    def test_a_data_question_is_not_sent_to_retrieval_when_tools_exist(self):
        """A knowledge-base miss must not refuse a question Roshada can answer."""
        assert grounding.attempt("is Layla booked with me?",
                                 tools_available=True) is None


# ---------------------------------------------------------------------------
# The API surface
# ---------------------------------------------------------------------------
class TestApi:
    def test_the_reply_reports_which_tools_ran(self, monkeypatch):
        patient, token = make(roles.PATIENT, "api_tools")
        doctor, _ = make(roles.DOCTOR, "api_doc")
        book(patient, doctor, days=2)
        script(monkeypatch, [("get_patient_appointments", {})],
               final="One appointment.")

        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        body = client.post("/api/chat/ask/",
                           {"message": "when is my appointment?"},
                           format="json").json()

        assert body["tools_used"] == ["get_patient_appointments"]
        assert body["reply"] == "One appointment."

    def test_the_response_leaks_no_internals(self, monkeypatch):
        patient, token = make(roles.PATIENT, "api_leak")
        script(monkeypatch, [("get_patient_appointments", {})])
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        raw = client.post("/api/chat/ask/", {"message": "my appointments"},
                          format="json").content.decode()
        # pending_action is server bookkeeping: the person already read the
        # summary in the reply, and the serializer must not echo the record.
        for leak in ("gsk_", "SECRET_KEY", "Traceback", "pending_action",
                     "_pending"):
            assert leak not in raw


# ---------------------------------------------------------------------------
# Section 8 — Health Screening is gone
# ---------------------------------------------------------------------------
class TestHealthScreeningRemoved:
    @pytest.mark.parametrize("endpoint", [
        "/api/screenings/", "/api/screenings/patient/1/",
        "/api/predict/heart/", "/api/predict/diabetes/", "/api/tumor/detect/",
    ])
    def test_the_endpoints_are_gone(self, endpoint):
        _user, token = make(roles.PATIENT, f"hs_{abs(hash(endpoint)) % 9999}")
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        assert client.get(endpoint).status_code == 404
        assert client.post(endpoint, {}, format="json").status_code == 404

    def test_the_model_is_gone(self):
        from appointments import models

        assert not hasattr(models, "ScreeningResult")

    def test_the_services_and_views_are_gone(self):
        import importlib

        for module in ("appointments.services.screenings",
                       "appointments.services.predictions",
                       "appointments.views.screenings",
                       "appointments.views.predictions"):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(module)

    def test_no_tool_mentions_screening(self):
        assert "screening" not in json.dumps(
            [t.schema() for t in tools.REGISTRY.values()]).lower()

    def test_the_page_and_its_navigation_entry_are_gone(self):
        import pathlib

        source = pathlib.Path("streamlit_app.py").read_text(encoding="utf-8")
        for marker in ("page_health_screening", "Health Screening",
                       "SCREENING_LABELS", "_render_screening_table"):
            assert marker not in source

    def test_the_notification_type_is_gone(self):
        from comms import types

        assert not hasattr(types, "SCREENING_FLAGGED")
        assert "screening_flagged" not in types.LABELS

    def test_the_timeline_no_longer_declares_a_screening_type(self):
        from records import timeline

        assert "screening" not in timeline.ALL_TYPES

    def test_the_capability_matrix_no_longer_mentions_screening(self):
        assert not hasattr(roles, "SCREENING_RUN")
        assert not hasattr(roles, "SCREENING_REVIEW")
        for role, held in roles.PERMISSIONS.items():
            assert not any("screening" in c for c in held), role

    def test_what_the_brief_said_to_keep_still_works(self):
        """Lab, records, prescriptions, radiology and appointments stay."""
        patient, token = make(roles.PATIENT, "hs_keep")
        doctor, _ = make(roles.DOCTOR, "hs_keepdoc")
        book(patient, doctor, days=2)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")

        for endpoint in ("/api/appointments/mine/", "/api/records/me/",
                         "/api/pharmacy/prescriptions/",
                         "/api/radiology/reports/",
                         "/api/dashboard/summary/"):
            assert client.get(endpoint).status_code == 200, endpoint

    def test_the_care_relationship_rule_survived_the_removal(self):
        """It used to live in the screening module. Four features depend on it."""
        import inspect

        from comms import messaging
        from pharmacy import services as pharmacy_services
        from radiology import services as radiology_services
        from records import access

        for module in (messaging, pharmacy_services, radiology_services, access):
            source = inspect.getsource(module)
            assert "screenings.treats_patient" not in source
            assert "care.treats_patient" in source
