"""The unified appointment engine.

One engine books doctors, laboratories and radiology centres, so most of these
tests are parametrized across all three provider kinds: a rule that holds for a
doctor but not a lab would mean there are still three systems wearing one name.

The double-booking tests deliberately go around the service layer in places.
Anything the service refuses is only as good as the service being the only
writer, so the guarantee is checked where it actually lives — the database.
"""
import datetime

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from accounts import roles
from accounts.services import register_account
from appointments.models import Appointment, AvailabilityRule, Service, TimeOff
from appointments.services import availability

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
# Fixtures: one of every kind, each with published hours
# ---------------------------------------------------------------------------
SIGNUP_EXTRAS = {
    roles.DOCTOR: {"specialization": "Cardiology"},
    roles.LABORATORY: {"services": "CBC"},
    roles.RADIOLOGY: {"services": "MRI"},
}


def make_provider(role, username=None):
    username = username or f"{role}_prov"
    user, _account, _profile, token = register_account(
        role, username=username, password=PW, name=f"{role.title()} Place",
        **SIGNUP_EXTRAS[role])
    return user, token.key


def make_patient(username="booker"):
    user, _a, _p, token = register_account(
        roles.PATIENT, username=username, password=PW, name="Book Er", age=30)
    return user, token.key


def next_weekday_date(days_ahead=2):
    """A date safely in the future, avoiding today's already-past slots."""
    return (timezone.localtime() + datetime.timedelta(days=days_ahead)).date()


def open_hours(provider, date, start="09:00", end="12:00", slot_minutes=30,
               service=None):
    """Publish a one-off rule for a specific date (no weekday ambiguity)."""
    return AvailabilityRule.objects.create(
        provider=provider, service=service, date=date,
        start_time=datetime.time.fromisoformat(start),
        end_time=datetime.time.fromisoformat(end), slot_minutes=slot_minutes)


def at(date, hhmm):
    return availability.combine(date, datetime.time.fromisoformat(hhmm))


def book(client, token, provider, date, hhmm, service=None, expect=201):
    client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
    payload = {"provider_id": provider.id, "date": date.isoformat(),
               "time": f"{hhmm}:00"}
    if service is not None:
        payload["service_id"] = service.id
    res = client.post("/api/appointment/create/", payload, format="json")
    assert res.status_code == expect, res.data
    return res


# ---------------------------------------------------------------------------
# One engine, three provider kinds
# ---------------------------------------------------------------------------
class TestOneEngineForEveryProvider:
    @pytest.mark.parametrize("role", roles.BOOKABLE_ROLES)
    def test_every_bookable_kind_can_be_booked(self, client, role):
        provider, _ = make_provider(role)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date)

        res = book(client, token, provider, date, "09:00")
        assert res.data["provider"]["id"] == provider.id
        assert res.data["provider"]["role"] == role
        assert res.data["date"] == date.isoformat()
        assert res.data["time"] == "09:00:00"
        assert res.data["end_time"] == "09:30:00"

    @pytest.mark.parametrize("role", roles.BOOKABLE_ROLES)
    def test_the_provider_sees_its_own_booking(self, client, role):
        provider, provider_token = make_provider(role)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date)
        book(client, token, provider, date, "09:00")

        client.credentials(HTTP_AUTHORIZATION=f"Token {provider_token}")
        res = client.get("/api/appointments/provider/")
        assert res.status_code == 200
        assert len(res.data) == 1
        assert res.data[0]["patient"]["username"] == "booker"

    def test_the_provider_directory_covers_all_three_kinds(self, client):
        for role in roles.BOOKABLE_ROLES:
            make_provider(role)
        res = client.get("/api/providers/")
        assert res.status_code == 200
        assert {p["role"] for p in res.data} == set(roles.BOOKABLE_ROLES)

    def test_the_directory_can_be_filtered_to_one_kind(self, client):
        for role in roles.BOOKABLE_ROLES:
            make_provider(role)
        res = client.get("/api/providers/", {"type": roles.LABORATORY})
        assert {p["role"] for p in res.data} == {roles.LABORATORY}


# ---------------------------------------------------------------------------
# Services drive duration
# ---------------------------------------------------------------------------
class TestServicesAndDuration:
    def test_the_service_duration_decides_the_slot_length(self, client):
        provider, _ = make_provider(roles.RADIOLOGY)
        _patient, token = make_patient()
        date = next_weekday_date()
        mri = Service.objects.create(provider=provider, name="MRI Brain",
                                     duration_minutes=60)
        open_hours(provider, date, "10:00", "16:00", slot_minutes=30, service=mri)

        res = book(client, token, provider, date, "10:00", service=mri)
        assert res.data["duration_minutes"] == 60
        assert res.data["end_time"] == "11:00:00"
        assert res.data["service"]["name"] == "MRI Brain"

    def test_a_long_service_is_offered_on_its_own_grid(self):
        """A 60-minute MRI inside a rule with 30-minute slots must be offered
        hourly, or the slots handed out could not hold the appointment."""
        provider, _ = make_provider(roles.RADIOLOGY)
        date = next_weekday_date()
        mri = Service.objects.create(provider=provider, name="MRI", duration_minutes=60)
        open_hours(provider, date, "10:00", "13:00", slot_minutes=30, service=mri)

        slots = availability.available_slots(provider, date, mri)
        assert [availability.timezone.localtime(s).strftime("%H:%M")
                for s, _e in slots] == ["10:00", "11:00", "12:00"]

    def test_a_partial_slot_at_the_end_is_not_offered(self):
        provider, _ = make_provider(roles.LABORATORY)
        date = next_weekday_date()
        # 09:00-10:20 with 30-minute slots fits 09:00 and 09:30; 10:00-10:30
        # would run past the close.
        open_hours(provider, date, "09:00", "10:20", slot_minutes=30)
        slots = availability.available_slots(provider, date)
        assert [availability.timezone.localtime(s).strftime("%H:%M")
                for s, _e in slots] == ["09:00", "09:30"]

    def test_another_providers_service_cannot_be_attached(self, client):
        lab_a, _ = make_provider(roles.LABORATORY, "lab_a")
        lab_b, _ = make_provider(roles.RADIOLOGY, "lab_b")
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(lab_a, date)
        cheap = Service.objects.create(provider=lab_b, name="Someone Elses Test")

        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        res = client.post("/api/appointment/create/",
                          {"provider_id": lab_a.id, "service_id": cheap.id,
                           "date": date.isoformat(), "time": "09:00:00"},
                          format="json")
        assert res.status_code == 404

    def test_a_service_in_use_is_withdrawn_not_deleted(self, client):
        provider, provider_token = make_provider(roles.LABORATORY)
        _patient, token = make_patient()
        date = next_weekday_date()
        cbc = Service.objects.create(provider=provider, name="CBC")
        open_hours(provider, date, service=cbc)
        book(client, token, provider, date, "09:00", service=cbc)

        client.credentials(HTTP_AUTHORIZATION=f"Token {provider_token}")
        res = client.delete(f"/api/me/services/{cbc.id}/")
        assert res.status_code == 200
        cbc.refresh_from_db()
        assert cbc.is_active is False
        # The booking still knows what was booked.
        assert Appointment.objects.get(service=cbc).service.name == "CBC"


# ---------------------------------------------------------------------------
# Booking rules
# ---------------------------------------------------------------------------
class TestBookingRules:
    def test_a_past_slot_is_refused(self, client):
        provider, _ = make_provider(roles.DOCTOR)
        _patient, token = make_patient()
        past = (timezone.localtime() - datetime.timedelta(days=1)).date()
        open_hours(provider, past)
        book(client, token, provider, past, "09:00", expect=400)

    def test_a_time_outside_the_published_hours_is_refused(self, client):
        provider, _ = make_provider(roles.LABORATORY)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date, "09:00", "12:00")
        book(client, token, provider, date, "15:00", expect=400)

    def test_a_time_off_the_slot_grid_is_refused(self, client):
        """09:15 is inside opening hours but is not a slot the provider offers."""
        provider, _ = make_provider(roles.LABORATORY)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date, "09:00", "12:00", slot_minutes=30)
        book(client, token, provider, date, "09:15", expect=400)

    def test_a_date_the_provider_does_not_open_is_refused(self, client):
        provider, _ = make_provider(roles.RADIOLOGY)
        _patient, token = make_patient()
        open_hours(provider, next_weekday_date(2))
        book(client, token, provider, next_weekday_date(3), "09:00", expect=400)

    def test_a_blocked_period_is_refused(self, client):
        provider, _ = make_provider(roles.DOCTOR)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date, "09:00", "14:00")
        TimeOff.objects.create(provider=provider, date=date,
                               start_time=datetime.time(13, 0),
                               end_time=datetime.time(14, 0), reason="Lunch")

        book(client, token, provider, date, "13:00", expect=400)
        book(client, token, provider, date, "09:00")      # outside the block

    def test_a_whole_day_off_blocks_everything(self, client):
        provider, _ = make_provider(roles.DOCTOR)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date, "09:00", "12:00")
        TimeOff.objects.create(provider=provider, date=date, reason="Leave")

        assert availability.available_slots(provider, date) == []
        book(client, token, provider, date, "09:00", expect=400)

    def test_an_unavailable_provider_is_refused(self, client):
        provider, _ = make_provider(roles.LABORATORY)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date)
        provider.laboratory_profile.available = False
        provider.laboratory_profile.save()
        book(client, token, provider, date, "09:00", expect=400)

    def test_a_dated_rule_replaces_the_weekly_pattern(self):
        """An override must not leave the usual hours bookable as well."""
        provider, _ = make_provider(roles.DOCTOR)
        date = next_weekday_date()
        AvailabilityRule.objects.create(
            provider=provider, weekday=date.weekday(),
            start_time=datetime.time(9, 0), end_time=datetime.time(12, 0))
        open_hours(provider, date, "14:00", "16:00")     # the override

        offered = {availability.timezone.localtime(s).strftime("%H:%M")
                   for s, _e in availability.available_slots(provider, date)}
        assert offered == {"14:00", "14:30", "15:00", "15:30"}

    def test_a_provider_with_no_rules_keeps_the_pre_unification_behaviour(
            self, client):
        """A doctor who never published hours must stay bookable, or this change
        would have invalidated every appointment made before availability
        existed."""
        provider, _ = make_provider(roles.DOCTOR)
        _patient, token = make_patient()
        assert not availability.has_rules(provider)
        book(client, token, provider, next_weekday_date(), "09:17")

    def test_publishing_one_rule_starts_enforcing_them(self, client):
        provider, _ = make_provider(roles.DOCTOR)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date, "09:00", "12:00")
        book(client, token, provider, date, "09:17", expect=400)


# ---------------------------------------------------------------------------
# Double booking — the critical guarantee
# ---------------------------------------------------------------------------
class TestDoubleBookingIsImpossible:
    def test_the_same_slot_cannot_be_booked_twice(self, client):
        provider, _ = make_provider(roles.LABORATORY)
        _a, first = make_patient("first")
        _b, second = make_patient("second")
        date = next_weekday_date()
        open_hours(provider, date)

        book(client, first, provider, date, "09:00")
        book(client, second, provider, date, "09:00", expect=409)

    def test_a_longer_appointment_blocks_the_slot_it_overlaps(self, client):
        """The old schema compared start instants only, so a 60-minute booking
        at 10:00 did not conflict with a 30-minute one at 10:30."""
        provider, _ = make_provider(roles.RADIOLOGY)
        _a, first = make_patient("first")
        _b, second = make_patient("second")
        date = next_weekday_date()
        mri = Service.objects.create(provider=provider, name="MRI",
                                     duration_minutes=60)
        scan = Service.objects.create(provider=provider, name="CT",
                                      duration_minutes=30)
        open_hours(provider, date, "10:00", "13:00", slot_minutes=30, service=mri)
        open_hours(provider, date, "10:00", "13:00", slot_minutes=30, service=scan)

        book(client, first, provider, date, "10:00", service=mri)      # 10:00-11:00
        # 409, not 400: 10:30 is a slot this centre offers — the MRI running
        # through it is a conflict, not a malformed request.
        book(client, second, provider, date, "10:30", service=scan, expect=409)

    def test_back_to_back_appointments_are_allowed(self, client):
        """Touching periods do not overlap — '[)' bounds. Without this, a full
        day of consecutive slots could never be booked."""
        provider, _ = make_provider(roles.LABORATORY)
        _a, first = make_patient("first")
        _b, second = make_patient("second")
        date = next_weekday_date()
        open_hours(provider, date, "09:00", "12:00", slot_minutes=30)

        book(client, first, provider, date, "09:00")
        book(client, second, provider, date, "09:30")

    def test_the_database_refuses_an_overlap_even_without_the_service(self):
        """The guarantee has to live below the service layer: a direct ORM write
        must be refused too, or it is only a convention."""
        provider, _ = make_provider(roles.DOCTOR)
        patient, _ = make_patient()
        start = availability.combine(next_weekday_date(), datetime.time(9, 0))

        Appointment.objects.create(provider=provider, patient=patient,
                                   start_at=start,
                                   end_at=start + datetime.timedelta(hours=1))
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Appointment.objects.create(
                    provider=provider, patient=patient,
                    start_at=start + datetime.timedelta(minutes=30),
                    end_at=start + datetime.timedelta(minutes=90))

    def test_two_providers_may_hold_the_same_hour(self):
        """The constraint is per provider, not global."""
        a, _ = make_provider(roles.DOCTOR, "doc_a")
        b, _ = make_provider(roles.LABORATORY, "lab_b")
        patient, _ = make_patient()
        start = availability.combine(next_weekday_date(), datetime.time(9, 0))
        for provider in (a, b):
            Appointment.objects.create(
                provider=provider, patient=patient, start_at=start,
                end_at=start + datetime.timedelta(minutes=30))
        assert Appointment.objects.count() == 2

    def test_a_cancelled_appointment_releases_its_slot(self, client):
        provider, _ = make_provider(roles.LABORATORY)
        _a, first = make_patient("first")
        _b, second = make_patient("second")
        date = next_weekday_date()
        open_hours(provider, date)

        booked = book(client, first, provider, date, "09:00")
        client.credentials(HTTP_AUTHORIZATION=f"Token {first}")
        client.post(f"/api/appointments/{booked.data['id']}/cancel/", {},
                    format="json")

        book(client, second, provider, date, "09:00")
        assert Appointment.objects.filter(status="cancelled").count() == 1

    def test_a_taken_slot_disappears_from_the_offer(self, client):
        provider, _ = make_provider(roles.LABORATORY)
        _a, first = make_patient("first")
        _b, second = make_patient("second")
        date = next_weekday_date()
        open_hours(provider, date, "09:00", "10:00", slot_minutes=30)
        book(client, first, provider, date, "09:00")

        client.credentials(HTTP_AUTHORIZATION=f"Token {second}")
        res = client.get("/api/slots/", {"provider": provider.id,
                                         "date": date.isoformat()})
        assert res.status_code == 200
        states = {s["start_time"]: s["state"] for s in res.data["slots"]}
        assert states["09:00"] == "booked"
        assert states["09:30"] == "available"
        assert [s["start_time"] for s in res.data["available"]] == ["09:30"]


# ---------------------------------------------------------------------------
# Rescheduling
# ---------------------------------------------------------------------------
class TestRescheduling:
    def test_a_booking_moves_to_another_free_slot(self, client):
        provider, _ = make_provider(roles.DOCTOR)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date, "09:00", "12:00")
        booked = book(client, token, provider, date, "09:00")

        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        res = client.post(f"/api/appointments/{booked.data['id']}/reschedule/",
                          {"date": date.isoformat(), "time": "10:30:00"},
                          format="json")
        assert res.status_code == 200
        assert res.data["time"] == "10:30:00"

    def test_rescheduling_onto_a_taken_slot_is_refused(self, client):
        provider, _ = make_provider(roles.LABORATORY)
        _a, first = make_patient("first")
        _b, second = make_patient("second")
        date = next_weekday_date()
        open_hours(provider, date, "09:00", "12:00")
        mine = book(client, first, provider, date, "09:00")
        book(client, second, provider, date, "10:00")

        client.credentials(HTTP_AUTHORIZATION=f"Token {first}")
        res = client.post(f"/api/appointments/{mine.data['id']}/reschedule/",
                          {"date": date.isoformat(), "time": "10:00:00"},
                          format="json")
        assert res.status_code in (400, 409)

    def test_rescheduling_outside_the_published_hours_is_refused(self, client):
        provider, _ = make_provider(roles.DOCTOR)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date, "09:00", "12:00")
        booked = book(client, token, provider, date, "09:00")

        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        res = client.post(f"/api/appointments/{booked.data['id']}/reschedule/",
                          {"date": date.isoformat(), "time": "20:00:00"},
                          format="json")
        assert res.status_code == 400

    def test_an_appointment_does_not_block_its_own_move(self, client):
        """Its current slot must not count as taken when it is the one moving —
        otherwise a 30-minute shift would collide with itself."""
        provider, _ = make_provider(roles.RADIOLOGY)
        _patient, token = make_patient()
        date = next_weekday_date()
        mri = Service.objects.create(provider=provider, name="MRI",
                                     duration_minutes=60)
        open_hours(provider, date, "10:00", "16:00", service=mri)
        booked = book(client, token, provider, date, "10:00", service=mri)

        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        res = client.post(f"/api/appointments/{booked.data['id']}/reschedule/",
                          {"date": date.isoformat(), "time": "11:00:00"},
                          format="json")
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# Authorization — the part that must not be frontend-only
# ---------------------------------------------------------------------------
class TestSchedulingAuthorization:
    def test_one_provider_cannot_read_anothers_queue(self, client):
        lab_a, token_a = make_provider(roles.LABORATORY, "lab_a")
        lab_b, _token_b = make_provider(roles.RADIOLOGY, "lab_b")
        _patient, patient_token = make_patient()
        date = next_weekday_date()
        open_hours(lab_b, date)
        book(client, patient_token, lab_b, date, "09:00")

        client.credentials(HTTP_AUTHORIZATION=f"Token {token_a}")
        res = client.get("/api/appointments/provider/")
        assert res.status_code == 200
        assert res.data == [], "a provider saw another provider's appointments"

    def test_a_patient_cannot_read_another_patients_appointment(self, client):
        provider, _ = make_provider(roles.DOCTOR)
        _a, first = make_patient("first")
        _b, second = make_patient("second")
        date = next_weekday_date()
        open_hours(provider, date)
        booked = book(client, first, provider, date, "09:00")

        client.credentials(HTTP_AUTHORIZATION=f"Token {second}")
        # 404, not 403: a different answer would confirm the id exists.
        assert client.post(f"/api/appointments/{booked.data['id']}/cancel/",
                           {}, format="json").status_code == 404

    def test_a_patient_cannot_publish_availability(self, client):
        _patient, token = make_patient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        assert client.get("/api/me/availability/").status_code == 403
        assert client.post("/api/me/availability/",
                           {"weekday": 0, "start_time": "09:00",
                            "end_time": "12:00"}, format="json").status_code == 403

    def test_a_provider_cannot_edit_another_providers_availability(self, client):
        doc_a, token_a = make_provider(roles.DOCTOR, "doc_a")
        doc_b, _ = make_provider(roles.LABORATORY, "lab_b")
        rule = open_hours(doc_b, next_weekday_date())

        client.credentials(HTTP_AUTHORIZATION=f"Token {token_a}")
        assert client.delete(f"/api/me/availability/{rule.id}/").status_code == 404
        assert AvailabilityRule.objects.filter(pk=rule.id).exists()

    def test_a_provider_cannot_edit_another_providers_service(self, client):
        _doc_a, token_a = make_provider(roles.DOCTOR, "doc_a")
        lab_b, _ = make_provider(roles.LABORATORY, "lab_b")
        service = Service.objects.create(provider=lab_b, name="CBC")

        client.credentials(HTTP_AUTHORIZATION=f"Token {token_a}")
        assert client.patch(f"/api/me/services/{service.id}/",
                            {"name": "Hijacked"}, format="json").status_code == 404
        service.refresh_from_db()
        assert service.name == "CBC"

    def test_availability_written_for_the_caller_not_the_payload(self, client):
        """The provider comes from the token. A provider_id in the body is
        ignored rather than obeyed."""
        doc_a, token_a = make_provider(roles.DOCTOR, "doc_a")
        lab_b, _ = make_provider(roles.LABORATORY, "lab_b")

        client.credentials(HTTP_AUTHORIZATION=f"Token {token_a}")
        res = client.post("/api/me/availability/",
                          {"provider": lab_b.id, "weekday": 0,
                           "start_time": "09:00", "end_time": "12:00"},
                          format="json")
        assert res.status_code == 201
        assert AvailabilityRule.objects.get(pk=res.data["id"]).provider_id == doc_a.id

    def test_a_provider_cannot_close_out_someone_elses_appointment(self, client):
        lab_a, token_a = make_provider(roles.LABORATORY, "lab_a")
        doc_b, _ = make_provider(roles.DOCTOR, "doc_b")
        _patient, patient_token = make_patient()
        date = next_weekday_date()
        open_hours(doc_b, date)
        booked = book(client, patient_token, doc_b, date, "09:00")

        client.credentials(HTTP_AUTHORIZATION=f"Token {token_a}")
        assert client.post(f"/api/appointments/{booked.data['id']}/outcome/",
                           {"status": "completed"},
                           format="json").status_code == 404

    def test_slots_require_authentication(self, client):
        provider, _ = make_provider(roles.DOCTOR)
        assert client.get("/api/slots/", {"provider": provider.id}).status_code == 401

    def test_a_pharmacy_cannot_manage_a_schedule(self, client):
        user, _a, _p, token = register_account(
            roles.PHARMACY, username="pharm_sched", password=PW, name="Pharm")
        client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
        assert client.get("/api/me/availability/").status_code == 403
        assert client.get("/api/appointments/provider/").status_code == 403


# ---------------------------------------------------------------------------
# Provider self-management
# ---------------------------------------------------------------------------
class TestProviderManagesOwnSchedule:
    @pytest.mark.parametrize("role", roles.BOOKABLE_ROLES)
    def test_a_provider_publishes_and_withdraws_hours(self, client, role):
        provider, token = make_provider(role)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")

        created = client.post("/api/me/availability/",
                              {"weekday": 1, "start_time": "09:00",
                               "end_time": "13:00", "slot_minutes": 30},
                              format="json")
        assert created.status_code == 201
        assert client.get("/api/me/availability/").data[0]["weekday_display"] == "Tuesday"

        assert client.delete(
            f"/api/me/availability/{created.data['id']}/").status_code == 200
        assert client.get("/api/me/availability/").data == []

    def test_a_rule_needs_a_weekday_or_a_date_but_not_both(self, client):
        _provider, token = make_provider(roles.DOCTOR)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        both = client.post("/api/me/availability/",
                           {"weekday": 1, "date": next_weekday_date().isoformat(),
                            "start_time": "09:00", "end_time": "13:00"},
                           format="json")
        neither = client.post("/api/me/availability/",
                              {"start_time": "09:00", "end_time": "13:00"},
                              format="json")
        assert both.status_code == 400 and neither.status_code == 400

    def test_a_rule_cannot_end_before_it_starts(self, client):
        _provider, token = make_provider(roles.DOCTOR)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        assert client.post("/api/me/availability/",
                           {"weekday": 1, "start_time": "13:00",
                            "end_time": "09:00"}, format="json").status_code == 400

    def test_a_provider_records_and_clears_time_off(self, client):
        _provider, token = make_provider(roles.RADIOLOGY)
        date = next_weekday_date()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")

        created = client.post("/api/me/time-off/",
                              {"date": date.isoformat(), "start_time": "10:00",
                               "end_time": "13:00", "reason": "Maintenance"},
                              format="json")
        assert created.status_code == 201
        assert len(client.get("/api/me/time-off/").data) == 1
        assert client.delete(
            f"/api/me/time-off/?id={created.data['id']}").status_code == 200

    def test_half_a_time_off_window_is_refused(self, client):
        _provider, token = make_provider(roles.DOCTOR)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        assert client.post("/api/me/time-off/",
                           {"date": next_weekday_date().isoformat(),
                            "start_time": "10:00"}, format="json").status_code == 400

    def test_a_provider_manages_its_service_catalogue(self, client):
        _provider, token = make_provider(roles.LABORATORY)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")

        created = client.post("/api/me/services/",
                              {"name": "HbA1c", "duration_minutes": 15,
                               "preparation": "Fast for 8 hours."}, format="json")
        assert created.status_code == 201
        duplicate = client.post("/api/me/services/", {"name": "hba1c"},
                                format="json")
        assert duplicate.status_code == 400, "duplicate service names allowed"

        patched = client.patch(f"/api/me/services/{created.data['id']}/",
                               {"duration_minutes": 20}, format="json")
        assert patched.data["duration_minutes"] == 20


# ---------------------------------------------------------------------------
# Patient search
# ---------------------------------------------------------------------------
class TestPatientSearch:
    def test_search_returns_only_providers_with_free_slots(self, client):
        busy, _ = make_provider(roles.LABORATORY, "busy_lab")
        free, _ = make_provider(roles.RADIOLOGY, "free_centre")
        _a, first = make_patient("first")
        date = next_weekday_date()
        open_hours(busy, date, "09:00", "09:30", slot_minutes=30)   # exactly one
        open_hours(free, date, "09:00", "11:00", slot_minutes=30)
        book(client, first, busy, date, "09:00")                    # now full

        client.credentials(HTTP_AUTHORIZATION=f"Token {first}")
        res = client.get("/api/availability/search/", {"date": date.isoformat()})
        assert res.status_code == 200
        names = {r["id"] for r in res.data}
        assert free.id in names
        assert busy.id not in names, "a fully booked provider was offered"

    def test_search_can_be_narrowed_to_a_provider_type(self, client):
        lab, _ = make_provider(roles.LABORATORY, "a_lab")
        centre, _ = make_provider(roles.RADIOLOGY, "a_centre")
        _a, token = make_patient()
        date = next_weekday_date()
        for provider in (lab, centre):
            open_hours(provider, date)

        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        res = client.get("/api/availability/search/",
                         {"date": date.isoformat(), "type": roles.RADIOLOGY})
        assert {r["id"] for r in res.data} == {centre.id}

    def test_the_slot_grid_reports_state_not_just_availability(self, client):
        provider, _ = make_provider(roles.DOCTOR)
        _a, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date, "09:00", "11:00", slot_minutes=60)
        TimeOff.objects.create(provider=provider, date=date,
                               start_time=datetime.time(10, 0),
                               end_time=datetime.time(11, 0), reason="Clinic")

        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        res = client.get("/api/slots/", {"provider": provider.id,
                                         "date": date.isoformat()})
        states = {s["start_time"]: s["state"] for s in res.data["slots"]}
        assert states == {"09:00": "available", "10:00": "unavailable"}


# ---------------------------------------------------------------------------
# Dashboards and notifications
#
# The frontend derives its notification bell from dashboard/summary/. That
# pipeline already existed; these check it now carries provider bookings too,
# rather than a new notification system being bolted on beside it.
# ---------------------------------------------------------------------------
class TestDashboardsAndNotifications:
    def test_a_patient_sees_the_provider_not_a_doctor(self, client):
        provider, _ = make_provider(roles.LABORATORY)
        _patient, token = make_patient()
        date = next_weekday_date()
        cbc = Service.objects.create(provider=provider, name="CBC")
        open_hours(provider, date, service=cbc)
        book(client, token, provider, date, "09:00", service=cbc)

        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        summary = client.get("/api/dashboard/summary/").data
        upcoming = summary["upcoming_appointments"]
        assert len(upcoming) == 1
        entry = upcoming[0]
        assert entry["provider"] == "Laboratory Place"
        assert entry["provider_role"] == roles.LABORATORY
        assert entry["service"] == "CBC"
        assert "doctor" not in entry, "the retired doctor key came back"

    @pytest.mark.parametrize("role", roles.BOOKABLE_ROLES)
    def test_a_provider_dashboard_counts_its_own_bookings(self, client, role):
        provider, provider_token = make_provider(role)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date)
        book(client, token, provider, date, "09:00")

        client.credentials(HTTP_AUTHORIZATION=f"Token {provider_token}")
        summary = client.get("/api/dashboard/summary/").data
        assert summary["stats"]["upcoming"] == 1
        assert summary["upcoming_appointments"][0]["patient"] == "Book Er"

    def test_a_facility_dashboard_flags_unpublished_hours(self, client):
        """The dashboard has to be able to say "nobody can book you yet"."""
        provider, token = make_provider(roles.RADIOLOGY)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        assert client.get("/api/dashboard/summary/").data[
            "publishes_availability"] is False

        open_hours(provider, next_weekday_date())
        assert client.get("/api/dashboard/summary/").data[
            "publishes_availability"] is True


# ---------------------------------------------------------------------------
# Timezone handling
# ---------------------------------------------------------------------------
class TestTimezone:
    def test_stored_instants_are_aware(self, client):
        provider, _ = make_provider(roles.DOCTOR)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date)
        booked = book(client, token, provider, date, "09:00")

        appointment = Appointment.objects.get(pk=booked.data["id"])
        assert timezone.is_aware(appointment.start_at)
        assert timezone.is_aware(appointment.end_at)

    def test_the_wall_clock_time_survives_the_round_trip(self, client):
        """A patient who books 09:00 must be shown 09:00 back, whatever the
        instant is stored as."""
        provider, _ = make_provider(roles.LABORATORY)
        _patient, token = make_patient()
        date = next_weekday_date()
        open_hours(provider, date)
        booked = book(client, token, provider, date, "09:00")

        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        listed = client.get("/api/appointments/mine/").data[0]
        assert listed["time"] == "09:00:00"
        assert listed["date"] == date.isoformat()
        assert Appointment.objects.get(pk=booked.data["id"]).time == \
            datetime.time(9, 0)


# ---------------------------------------------------------------------------
# The legacy identifier
# ---------------------------------------------------------------------------
class TestLegacyDoctorIdStillWorks:
    def test_booking_by_doctor_id_maps_to_the_provider(self, client):
        provider, _ = make_provider(roles.DOCTOR)
        _patient, token = make_patient()
        date = next_weekday_date()

        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        res = client.post("/api/appointment/create/",
                          {"doctor_id": provider.doctor_profile.id,
                           "date": date.isoformat(), "time": "09:00:00"},
                          format="json")
        assert res.status_code == 201
        assert res.data["provider"]["id"] == provider.id

    def test_a_doctor_id_and_a_provider_id_together_are_refused(self, client):
        provider, _ = make_provider(roles.DOCTOR)
        _patient, token = make_patient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        res = client.post("/api/appointment/create/",
                          {"doctor_id": provider.doctor_profile.id,
                           "provider_id": provider.id,
                           "date": next_weekday_date().isoformat(),
                           "time": "09:00:00"}, format="json")
        assert res.status_code == 400

    def test_a_booking_needs_some_provider(self, client):
        _patient, token = make_patient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        res = client.post("/api/appointment/create/",
                          {"date": next_weekday_date().isoformat(),
                           "time": "09:00:00"}, format="json")
        assert res.status_code == 400

    def test_a_patient_id_is_not_a_provider_id(self, client):
        """provider_id is a user id, so a patient's id must not resolve."""
        other, _ = make_patient("someone_else")
        _patient, token = make_patient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        res = client.post("/api/appointment/create/",
                          {"provider_id": other.id,
                           "date": next_weekday_date().isoformat(),
                           "time": "09:00:00"}, format="json")
        assert res.status_code == 404
