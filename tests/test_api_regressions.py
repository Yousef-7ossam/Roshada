"""Regression tests for the bugs found in the 2026-08 audit.

Each test names the behaviour that was wrong before the fix, so a reintroduction
fails loudly rather than silently.
"""
import datetime
import io

import pytest
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

pytestmark = pytest.mark.django_db

STRONG_PASSWORD = "Str0ng!Passw0rd"


def png_bytes(size=(16, 16)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _no_throttling(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "DEFAULT_THROTTLE_CLASSES": []}


@pytest.fixture
def client():
    return APIClient()


@pytest.fixture
def patient(client):
    res = client.post("/api/signup/patient/",
                      {"username": "pat1", "password": STRONG_PASSWORD,
                       "name": "Pat One", "age": 30}, format="json")
    assert res.status_code == 201
    return res.data["token"]


@pytest.fixture
def doctor(client):
    res = client.post("/api/signup/doctor/",
                      {"username": "doc1", "password": STRONG_PASSWORD,
                       "name": "Doc One", "specialization": "Cardiology"},
                      format="json")
    assert res.status_code == 201
    return res.data["doctor"]["id"], res.data["token"]


def future_slot(days=3, hour=10):
    day = timezone.localtime() + datetime.timedelta(days=days)
    return day.date().isoformat(), f"{hour:02d}:00:00"


# ---------------------------------------------------------------------------
# BUG-006 — appointments could be booked in the past
# ---------------------------------------------------------------------------
class TestAppointmentDateValidation:
    def test_past_date_is_rejected(self, client, patient, doctor):
        doctor_id, _ = doctor
        client.credentials(HTTP_AUTHORIZATION=f"Token {patient}")
        res = client.post("/api/appointment/create/",
                          {"doctor_id": doctor_id, "date": "2001-01-01",
                           "time": "09:00:00", "reason": "x"}, format="json")
        assert res.status_code == 400
        assert "future" in res.data["error"].lower()

    def test_earlier_today_is_rejected(self, client, patient, doctor):
        doctor_id, _ = doctor
        client.credentials(HTTP_AUTHORIZATION=f"Token {patient}")
        yesterday = (timezone.localtime() - datetime.timedelta(days=1))
        res = client.post("/api/appointment/create/",
                          {"doctor_id": doctor_id,
                           "date": yesterday.date().isoformat(),
                           "time": "23:59:00"}, format="json")
        assert res.status_code == 400

    def test_absurdly_far_future_is_rejected(self, client, patient, doctor):
        doctor_id, _ = doctor
        client.credentials(HTTP_AUTHORIZATION=f"Token {patient}")
        far = timezone.localtime() + datetime.timedelta(days=800)
        res = client.post("/api/appointment/create/",
                          {"doctor_id": doctor_id, "date": far.date().isoformat(),
                           "time": "10:00:00"}, format="json")
        assert res.status_code == 400

    def test_future_slot_still_books(self, client, patient, doctor):
        doctor_id, _ = doctor
        client.credentials(HTTP_AUTHORIZATION=f"Token {patient}")
        date, time = future_slot()
        res = client.post("/api/appointment/create/",
                          {"doctor_id": doctor_id, "date": date, "time": time,
                           "reason": "checkup"}, format="json")
        assert res.status_code == 201

    def test_double_booking_still_conflicts(self, client, patient, doctor):
        doctor_id, _ = doctor
        client.credentials(HTTP_AUTHORIZATION=f"Token {patient}")
        date, time = future_slot(days=4)
        payload = {"doctor_id": doctor_id, "date": date, "time": time}
        assert client.post("/api/appointment/create/", payload, format="json").status_code == 201
        assert client.post("/api/appointment/create/", payload, format="json").status_code == 409


# ---------------------------------------------------------------------------
# Token expiry
# ---------------------------------------------------------------------------
class TestTokenExpiry:
    def test_expired_token_is_rejected(self, client, patient):
        token = Token.objects.get(key=patient)
        token.created = timezone.now() - datetime.timedelta(hours=48)
        token.save(update_fields=["created"])

        client.credentials(HTTP_AUTHORIZATION=f"Token {patient}")
        res = client.get("/api/profile/")
        assert res.status_code == 401
        assert not Token.objects.filter(key=patient).exists()

    def test_login_issues_a_fresh_token_after_expiry(self, client, patient):
        token = Token.objects.get(key=patient)
        token.created = timezone.now() - datetime.timedelta(hours=48)
        token.save(update_fields=["created"])

        res = client.post("/api/login/",
                          {"username": "pat1", "password": STRONG_PASSWORD},
                          format="json")
        assert res.status_code == 200
        assert res.data["token"] != patient

    def test_valid_token_still_works(self, client, patient):
        client.credentials(HTTP_AUTHORIZATION=f"Token {patient}")
        assert client.get("/api/profile/").status_code == 200

    def test_expiry_can_be_disabled(self, client, patient, settings):
        settings.AUTH_TOKEN_TTL_HOURS = 0
        token = Token.objects.get(key=patient)
        token.created = timezone.now() - datetime.timedelta(days=400)
        token.save(update_fields=["created"])
        client.credentials(HTTP_AUTHORIZATION=f"Token {patient}")
        assert client.get("/api/profile/").status_code == 200


# ---------------------------------------------------------------------------
# BUG-019 — liveness vs readiness were conflated
# ---------------------------------------------------------------------------
class TestHealthEndpoint:
    def test_liveness_is_200(self, client):
        res = client.get("/api/health/")
        assert res.status_code == 200
        assert res.data["status"] == "ok"

    def test_readiness_reports_the_database(self, client):
        res = client.get("/api/health/?ready=1")
        assert res.status_code == 200
        assert res.data["status"] == "ready"
        assert res.data["database"] == "ok"


# ---------------------------------------------------------------------------
# Guards on behaviour that already worked — must not regress
# ---------------------------------------------------------------------------
class TestExistingBehaviourPreserved:
    def test_roles_and_permissions(self, client, patient, doctor):
        doctor_id, doc_token = doctor
        client.credentials(HTTP_AUTHORIZATION=f"Token {patient}")
        assert client.get("/api/appointments/doctor/").status_code == 403
        client.credentials(HTTP_AUTHORIZATION=f"Token {doc_token}")
        assert client.get("/api/appointments/doctor/").status_code == 200

    def test_logout_invalidates_the_token(self, client, patient):
        client.credentials(HTTP_AUTHORIZATION=f"Token {patient}")
        assert client.post("/api/logout/").status_code == 200
        assert client.get("/api/profile/").status_code == 401

    def test_weak_password_and_duplicate_username_rejected(self, client):
        assert client.post("/api/signup/patient/",
                           {"username": "weak1", "password": "123", "name": "W"},
                           format="json").status_code == 400
        client.post("/api/signup/patient/",
                    {"username": "dup1", "password": STRONG_PASSWORD, "name": "D"},
                    format="json")
        assert client.post("/api/signup/patient/",
                           {"username": "dup1", "password": STRONG_PASSWORD, "name": "D"},
                           format="json").status_code == 400

    def test_doctor_list_is_public_and_unknown_doctor_is_404(self, client, patient, doctor):
        assert client.get("/api/doctors/").status_code == 200
        client.credentials(HTTP_AUTHORIZATION=f"Token {patient}")
        date, time = future_slot(days=5)
        res = client.post("/api/appointment/create/",
                          {"doctor_id": 999999, "date": date, "time": time},
                          format="json")
        assert res.status_code == 404
