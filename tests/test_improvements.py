"""Tests for the improvements added after the 2026-08 improvement audit.

Covers: per-user chat isolation (P0-1), real dashboard aggregates (P0-2),
appointment lifecycle (P1-1),
and AI conversation memory + emergency guardrails (P1-4).
"""
import datetime

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from appointments.models import Appointment, ChatMessage

pytestmark = pytest.mark.django_db

PW = "Str0ng!Passw0rd"


@pytest.fixture(autouse=True)
def _no_throttling(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_CLASSES": []}


@pytest.fixture
def client():
    return APIClient()


def _patient(client, username):
    res = client.post("/api/signup/patient/",
                      {"username": username, "password": PW, "name": username.title()},
                      format="json")
    assert res.status_code == 201
    return res.data["token"]


def _doctor(client, username="doc1"):
    res = client.post("/api/signup/doctor/",
                      {"username": username, "password": PW, "name": "Doc One",
                       "specialization": "Cardiology"}, format="json")
    assert res.status_code == 201
    return res.data["doctor"]["id"], res.data["token"]


def _slot(days=3, hour=10):
    day = timezone.localtime() + datetime.timedelta(days=days)
    return {"date": day.date().isoformat(), "time": f"{hour:02d}:00:00"}


def _book(client, token, doctor_id, **overrides):
    payload = {"doctor_id": doctor_id, **_slot(), "reason": "checkup", **overrides}
    client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
    res = client.post("/api/appointment/create/", payload, format="json")
    assert res.status_code == 201, res.data
    return res.data


def _user(username, patient=True):
    """A bare user for service-level tests that don't need the HTTP layer."""
    from django.contrib.auth.models import User

    from appointments.models import PatientProfile

    user = User.objects.create_user(username=username, password=PW)
    if patient:
        PatientProfile.objects.create(user=user, age=54, gender="Male",
                                      medical_history="Type 2 diabetes since 2019")
    return user


def _stub_llm(monkeypatch, text="ok"):
    """Replace the provider seam and capture what the pipeline sent it.

    Tests never resolve a real provider: the suite must not depend on which keys
    happen to be in the developer's .env, and must never make a network call.
    """
    from appointments.services.ai import llm, pipeline

    captured = {}

    def fake_complete(prompt, *, history=None, system_prompt=None):
        captured.update(prompt=prompt, history=history, system_prompt=system_prompt)
        return llm.LLMResult(text=text, provider="stub", model="stub-1")

    monkeypatch.setattr(pipeline.llm, "complete", fake_complete)
    return captured


# ===========================================================================
# P0-1 — chat history is private to each user
# ===========================================================================
class TestChatPrivacy:
    def test_a_user_never_sees_another_users_messages(self, client):
        alice = _patient(client, "alice")
        bob = _patient(client, "bob")

        client.credentials(HTTP_AUTHORIZATION=f"Token {alice}")
        client.post("/api/chat/messages/",
                    {"prompt": "chest pain and HbA1c 9.2", "reply": "see a doctor"},
                    format="json")

        client.credentials(HTTP_AUTHORIZATION=f"Token {bob}")
        res = client.get("/api/chat/history/")
        assert res.status_code == 200
        assert res.data == [], "Bob can read Alice's medical questions"

    def test_history_round_trips_for_its_owner(self, client):
        token = _patient(client, "carol")
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        client.post("/api/chat/messages/",
                    {"prompt": "is metformin ok?", "reply": "ask your pharmacist"},
                    format="json")
        res = client.get("/api/chat/history/")
        assert [m["role"] for m in res.data] == ["user", "assistant"]
        assert res.data[0]["text"] == "is metformin ok?"

    def test_anonymous_access_is_rejected(self, client):
        assert client.get("/api/chat/history/").status_code == 401
        assert client.post("/api/chat/messages/",
                           {"prompt": "x", "reply": "y"}, format="json").status_code == 401

    def test_clearing_only_removes_your_own_messages(self, client):
        alice = _patient(client, "alice2")
        bob = _patient(client, "bob2")
        for token in (alice, bob):
            client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
            client.post("/api/chat/messages/", {"prompt": "q", "reply": "a"}, format="json")

        client.credentials(HTTP_AUTHORIZATION=f"Token {alice}")
        assert client.delete("/api/chat/history/").status_code == 200
        assert client.get("/api/chat/history/").data == []

        client.credentials(HTTP_AUTHORIZATION=f"Token {bob}")
        assert len(client.get("/api/chat/history/").data) == 2, "Bob's history was destroyed"

    def test_context_endpoint_returns_llm_shaped_turns(self, client):
        token = _patient(client, "dave")
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        client.post("/api/chat/messages/", {"prompt": "q1", "reply": "a1"}, format="json")
        res = client.get("/api/chat/context/")
        assert res.status_code == 200
        assert res.data == [{"role": "user", "content": "q1"},
                            {"role": "assistant", "content": "a1"}]


# ===========================================================================
# P1-1 — appointment lifecycle
# ===========================================================================
class TestAppointmentLifecycle:
    def test_patient_can_cancel_and_the_slot_frees_up(self, client):
        doctor_id, _ = _doctor(client)
        alice = _patient(client, "alice3")
        appt = _book(client, alice, doctor_id)

        res = client.post(f"/api/appointments/{appt['id']}/cancel/",
                          {"reason": "feeling better"}, format="json")
        assert res.status_code == 200
        assert res.data["status"] == "cancelled"

        # The same slot must now be bookable by someone else — an unconditional
        # unique constraint would have kept it reserved forever.
        bob = _patient(client, "bob3")
        client.credentials(HTTP_AUTHORIZATION=f"Token {bob}")
        again = client.post("/api/appointment/create/",
                            {"doctor_id": doctor_id, **_slot()}, format="json")
        assert again.status_code == 201

    def test_cancelling_twice_is_rejected(self, client):
        doctor_id, _ = _doctor(client)
        token = _patient(client, "alice4")
        appt = _book(client, token, doctor_id)
        client.post(f"/api/appointments/{appt['id']}/cancel/", {}, format="json")
        second = client.post(f"/api/appointments/{appt['id']}/cancel/", {}, format="json")
        assert second.status_code == 409

    def test_a_stranger_cannot_touch_someone_elses_appointment(self, client):
        doctor_id, _ = _doctor(client)
        alice = _patient(client, "alice5")
        appt = _book(client, alice, doctor_id)

        mallory = _patient(client, "mallory")
        client.credentials(HTTP_AUTHORIZATION=f"Token {mallory}")
        res = client.post(f"/api/appointments/{appt['id']}/cancel/", {}, format="json")
        # 404 not 403, so the endpoint cannot be used to enumerate appointment ids.
        assert res.status_code == 404
        assert Appointment.objects.get(pk=appt["id"]).status == "scheduled"

    def test_reschedule_moves_the_slot(self, client):
        doctor_id, _ = _doctor(client)
        token = _patient(client, "alice6")
        appt = _book(client, token, doctor_id)
        new = _slot(days=6, hour=15)
        res = client.post(f"/api/appointments/{appt['id']}/reschedule/", new, format="json")
        assert res.status_code == 200
        assert res.data["date"] == new["date"]

    def test_reschedule_into_the_past_is_rejected(self, client):
        doctor_id, _ = _doctor(client)
        token = _patient(client, "alice7")
        appt = _book(client, token, doctor_id)
        res = client.post(f"/api/appointments/{appt['id']}/reschedule/",
                          {"date": "2001-01-01", "time": "09:00:00"}, format="json")
        assert res.status_code == 400
        assert "future" in res.data["error"].lower()

    def test_reschedule_onto_a_taken_slot_conflicts(self, client):
        doctor_id, _ = _doctor(client)
        alice = _patient(client, "alice8")
        first = _book(client, alice, doctor_id, **_slot(days=3, hour=9))
        _book(client, alice, doctor_id, **_slot(days=3, hour=11))
        res = client.post(f"/api/appointments/{first['id']}/reschedule/",
                          _slot(days=3, hour=11), format="json")
        assert res.status_code == 409

    def test_doctor_closes_out_a_visit(self, client):
        doctor_id, doc_token = _doctor(client)
        token = _patient(client, "alice9")
        appt = _book(client, token, doctor_id)

        client.credentials(HTTP_AUTHORIZATION=f"Token {doc_token}")
        res = client.post(f"/api/appointments/{appt['id']}/outcome/",
                          {"status": "completed"}, format="json")
        assert res.status_code == 200
        assert res.data["status"] == "completed"

    def test_patient_cannot_mark_their_own_visit_completed(self, client):
        doctor_id, _ = _doctor(client)
        token = _patient(client, "alice10")
        appt = _book(client, token, doctor_id)
        res = client.post(f"/api/appointments/{appt['id']}/outcome/",
                          {"status": "completed"}, format="json")
        assert res.status_code == 403

    def test_invalid_outcome_is_rejected(self, client):
        doctor_id, doc_token = _doctor(client)
        token = _patient(client, "alice11")
        appt = _book(client, token, doctor_id)
        client.credentials(HTTP_AUTHORIZATION=f"Token {doc_token}")
        res = client.post(f"/api/appointments/{appt['id']}/outcome/",
                          {"status": "banana"}, format="json")
        assert res.status_code == 400


# ===========================================================================
# P0-2 — dashboards report real figures
# ===========================================================================
class TestDashboardSummary:
    def test_patient_summary_counts_real_records(self, client):
        doctor_id, _ = _doctor(client)
        token = _patient(client, "alice18")
        _book(client, token, doctor_id)

        res = client.get("/api/dashboard/summary/")
        assert res.status_code == 200
        assert res.data["role"] == "patient"
        assert res.data["stats"]["upcoming_appointments"] == 1
        assert len(res.data["upcoming_appointments"]) == 1

    def test_untracked_metrics_are_null_not_invented(self, client):
        token = _patient(client, "alice19")
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        res = client.get("/api/dashboard/summary/")
        stats = res.data["stats"]
        # The UI renders these as "—". A number here would be a fabricated
        # clinical figure, which is what this endpoint exists to eliminate.
        assert stats["medication_adherence"] is None
        assert "medication_adherence" in res.data["unsupported_metrics"]
        # active_prescriptions used to be on this list. The Pharmacy module
        # gave it a real source, so 0 here is a counted answer rather than an
        # invented one — which is exactly the transition the derived
        # unsupported_metrics list exists to make automatic.
        assert stats["active_prescriptions"] == 0
        assert "active_prescriptions" not in res.data["unsupported_metrics"]

    def test_a_new_patient_sees_zeroes_not_demo_data(self, client):
        token = _patient(client, "fresh")
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        stats = client.get("/api/dashboard/summary/").data["stats"]
        assert stats["upcoming_appointments"] == 0
        assert stats["total_appointments"] == 0

    def test_doctor_summary_is_scoped_to_that_doctor(self, client):
        doctor_id, doc_token = _doctor(client)
        _doctor(client, "doc2")
        token = _patient(client, "alice20")
        _book(client, token, doctor_id)

        client.credentials(HTTP_AUTHORIZATION=f"Token {doc_token}")
        res = client.get("/api/dashboard/summary/")
        assert res.data["role"] == "doctor"
        assert res.data["stats"]["total_patients"] == 1
        assert len(res.data["weekly_appointments"]) == 7

        # The other doctor must see none of it.
        second = client.post("/api/login/", {"username": "doc2", "password": PW},
                             format="json")
        client.credentials(HTTP_AUTHORIZATION=f"Token {second.data['token']}")
        other = client.get("/api/dashboard/summary/")
        assert other.data["stats"]["total_patients"] == 0

    def test_cancelled_appointments_leave_the_upcoming_count(self, client):
        doctor_id, _ = _doctor(client)
        token = _patient(client, "alice21")
        appt = _book(client, token, doctor_id)
        client.post(f"/api/appointments/{appt['id']}/cancel/", {}, format="json")
        stats = client.get("/api/dashboard/summary/").data["stats"]
        assert stats["upcoming_appointments"] == 0


# ===========================================================================
# P1-4 — AI memory and emergency guardrails
# ===========================================================================
class TestAISafety:
    """The context-only assistant path.

    Tools are switched off here on purpose: these guarantees — the emergency
    notice, the conversation window, the current question staying out of its own
    history — are properties of the pipeline, and they must hold whether or not
    the deployment has a tool-capable provider. The tool-using path has its own
    suite in ``test_ai_tools.py``.
    """

    @pytest.fixture(autouse=True)
    def _no_tools(self, monkeypatch):
        monkeypatch.setenv("AI_TOOLS", "off")

    @pytest.mark.parametrize("text,expected", [
        ("I have chest pain radiating to my arm", True),
        ("my father is having a stroke", True),
        ("I think I took an overdose", True),
        ("I can't breathe properly", True),
        ("I want to kill myself", True),
        ("what foods lower cholesterol?", False),
        ("is walking good for diabetes?", False),
        # Must not fire on a substring of an unrelated word.
        ("I get heartburn after meals", False),
    ])
    def test_emergency_detection(self, text, expected):
        from shared import safety
        assert safety.is_emergency(text) is expected

    # The four tests below moved with the assistant. They used to patch
    # ``shared.ai`` in the Streamlit process; the orchestration now lives in
    # ``appointments.services.ai``, so they assert the same guarantees against
    # the pipeline. The guarantees themselves are unchanged — that is the point
    # of keeping them.
    def test_emergency_notice_is_prepended_even_with_no_provider(self, monkeypatch):
        from appointments.services.ai import llm, pipeline
        monkeypatch.setattr(llm, "active_provider", lambda: None)
        reply = pipeline.ask(_user("ai_noprov"), "I have severe chest pain").reply
        assert "emergency" in reply.lower()
        assert "123" in reply, "the local emergency number must be shown"

    def test_emergency_notice_survives_a_provider_failure(self, monkeypatch):
        from appointments.services.ai import llm, pipeline

        def boom(*args, **kwargs):
            raise llm.LLMFailed("groq")

        monkeypatch.setattr(llm, "complete", boom)
        result = pipeline.ask(_user("ai_boom"), "I think I'm having a heart attack")
        assert "emergency" in result.reply.lower()
        assert result.degraded is True

    def test_ordinary_questions_get_no_emergency_banner(self, monkeypatch):
        from appointments.services.ai import pipeline
        _stub_llm(monkeypatch, "Eat more fibre.")
        assert pipeline.ask(_user("ai_plain"), "what should I eat?").reply == "Eat more fibre."

    def test_history_is_forwarded_to_the_model(self, monkeypatch):
        """A follow-up must arrive with the turns it refers to."""
        from appointments.services.ai import pipeline
        captured = _stub_llm(monkeypatch, "ok")

        user = _user("ai_history")
        ChatMessage.objects.create(user=user, role="user", text="I have type 2 diabetes")
        ChatMessage.objects.create(user=user, role="assistant", text="Noted.")

        pipeline.ask(user, "what about metformin?")
        assert captured["history"] == [
            {"role": "user", "content": "I have type 2 diabetes"},
            {"role": "assistant", "content": "Noted."},
        ], "follow-ups lose their context"

    def test_the_current_question_is_not_in_its_own_history(self, monkeypatch):
        """Context is built before the exchange is recorded."""
        from appointments.services.ai import pipeline
        captured = _stub_llm(monkeypatch, "sure")
        pipeline.ask(_user("ai_selfref"), "first question")
        assert captured["history"] == []

    # Two tests moved out of this class when the providers were replaced by the
    # adapter layer (Task 03). The guarantees are unchanged, only their home:
    #   test_groq_places_history_between_system_and_prompt
    #     -> test_llm_providers.py::TestGroq
    #        ::test_history_sits_between_the_system_prompt_and_the_new_question
    #   test_system_prompt_carries_the_safety_rules
    #     -> test_ai_assistant.py::TestPromptManagement
    #        ::test_every_template_carries_the_safety_rules
    #        (which checks every template, not just the one retired string)


# ===========================================================================
# Regression guards on behaviour the improvements touched
# ===========================================================================
class TestNoRegressions:
    def test_booking_and_conflict_still_work(self, client):
        doctor_id, _ = _doctor(client)
        alice = _patient(client, "alice22")
        _book(client, alice, doctor_id)
        client.credentials(HTTP_AUTHORIZATION=f"Token {alice}")
        dup = client.post("/api/appointment/create/",
                          {"doctor_id": doctor_id, **_slot()}, format="json")
        assert dup.status_code == 409

    def test_appointment_payload_keeps_its_original_fields(self, client):
        doctor_id, _ = _doctor(client)
        alice = _patient(client, "alice23")
        appt = _book(client, alice, doctor_id)
        # 'doctor' became 'provider' when the engine was unified: the same
        # payload now describes a lab and an imaging booking, where naming the
        # counterparty "doctor" would be wrong. Everything else is unchanged,
        # including date/time, which are derived from the stored instant.
        for key in ("id", "provider", "patient", "date", "time", "end_time",
                    "reason", "created_at"):
            assert key in appt, f"{key} disappeared from the appointment contract"
        assert "doctor" not in appt, "the retired doctor key came back"
        assert appt["provider"]["role"] == "doctor"
        assert appt["status"] == "scheduled"

    def test_logout_still_invalidates_the_token(self, client):
        token = _patient(client, "alice24")
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        assert client.post("/api/logout/").status_code == 200
        assert client.get("/api/chat/history/").status_code == 401

    def test_deleting_a_user_removes_their_chat(self, client):
        """Cascade must hold — orphaned medical data would be a retention bug."""
        from django.contrib.auth.models import User
        token = _patient(client, "alice25")
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        client.post("/api/chat/messages/", {"prompt": "q", "reply": "a"}, format="json")
        user = User.objects.get(username="alice25")
        user.delete()
        assert ChatMessage.objects.count() == 0
