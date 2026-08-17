"""The Unified Communication & Notification Module.

Three things are worth proving here, and the rest is coverage:

* **One architecture.** Every module raises the same kind of row through one
  service. A test asserts the app owns exactly three models and that no
  ``DoctorNotification`` / ``LabNotification`` / ``PharmacyNotification``
  exists anywhere.
* **Notifications carry no clinical content.** Every producer is driven with
  sentinel wording — a report impression, a prescription's medication, a
  message body — and the notification list is asserted never to contain it.
* **A notification can never break what raised it.** A deliberately broken
  channel, and a failure inside the notification write itself, must leave the
  booking, the release and the dispense intact.
"""
import datetime

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from accounts import roles
from accounts.services import register_account
from appointments.models import Appointment, Service
from appointments.services import availability, scheduling
from comms import channels, messaging, notifications, types
from comms.models import Conversation, Message, Notification
from pharmacy import dosage_forms
from pharmacy.models import Medication, PharmacyInventory
from radiology import modalities
from radiology.models import Examination, ImagingOrder, RadiologyReport

pytestmark = pytest.mark.django_db

PW = "Str0ng!Passw0rd"

#: Wording that must never reach a notification. Each producer is driven with
#: it so "no clinical content" is checked against real text, not asserted.
SENTINEL_REPORT = "SENTINEL-IMPRESSION suspicious lesion in the left lobe"
SENTINEL_MESSAGE = "SENTINEL-MESSAGE my private symptom description"
SENTINEL_MEDICATION = "Sentinelamide"


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
        roles.PATIENT: {"age": 39},
        roles.DOCTOR: {"specialization": "General Practice"},
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
    start = availability.combine(
        (timezone.localtime() + datetime.timedelta(days=days)).date(),
        datetime.time(9, 0))
    return Appointment.objects.create(
        provider=provider, patient=patient, start_at=start,
        end_at=start + datetime.timedelta(minutes=minutes),
        status=status, service=service, reason="Follow-up")


def open_provider(provider, day, service, start="08:00", end="18:00"):
    """Publish availability so the engine will accept a booking."""
    from appointments.models import AvailabilityRule
    return AvailabilityRule.objects.create(
        provider=provider, service=service, date=day,
        start_time=datetime.time.fromisoformat(start),
        end_time=datetime.time.fromisoformat(end),
        slot_minutes=service.duration_minutes)


def book(patient, provider, day, at, service):
    """Open the provider's day, then book through the engine."""
    open_provider(provider, day, service)
    return scheduling.create_appointment(
        patient, date=day, time=at, provider_id=provider.id,
        service_id=service.id)


@pytest.fixture
def cast():
    patient, patient_token = make(roles.PATIENT, "cm_patient")
    doctor, doctor_token = make(roles.DOCTOR, "cm_doctor")
    center, center_token = make(roles.RADIOLOGY, "cm_centre")
    pharmacy, pharmacy_token = make(roles.PHARMACY, "cm_pharmacy")
    visit = appointment(doctor, patient)
    return {
        "patient": patient, "patient_token": patient_token,
        "doctor": doctor, "doctor_token": doctor_token,
        "center": center, "center_token": center_token,
        "pharmacy": pharmacy, "pharmacy_token": pharmacy_token,
        "visit": visit,
    }


def inbox(user, notification_type=None):
    queryset = Notification.objects.filter(recipient=user)
    if notification_type:
        queryset = queryset.filter(type=notification_type)
    return list(queryset)


def kinds(user):
    return {n.type for n in inbox(user)}


def all_text(user):
    return " ".join(f"{n.title} {n.body}" for n in inbox(user))


def released_report(cast, impression=SENTINEL_REPORT):
    """A full imaging chain, released through radiology's own workflow."""
    from radiology import services as radiology_services
    service = Service.objects.create(provider=cast["center"], name="MRI Brain",
                                     category=modalities.MRI,
                                     duration_minutes=60)
    booking = appointment(cast["center"], cast["patient"], days=-2, minutes=60,
                          service=service)
    order = ImagingOrder.objects.create(
        patient=cast["patient"], doctor=cast["doctor"],
        modality=modalities.MRI, study_name="MRI Brain",
        status=ImagingOrder.REPORT_PENDING)
    examination = Examination.objects.create(
        appointment=booking, order=order, status=Examination.COMPLETED)
    report = RadiologyReport.objects.create(
        examination=examination, author=cast["center"],
        findings="No acute abnormality.", impression=impression,
        status=RadiologyReport.DRAFT)
    for step in (RadiologyReport.PENDING_REVIEW, RadiologyReport.VERIFIED,
                 RadiologyReport.RELEASED):
        radiology_services.transition_report(cast["center"], report.id, step)
    report.refresh_from_db()
    return report


def prescription(cast, issue=True, medication_name=SENTINEL_MEDICATION):
    from pharmacy import services as pharmacy_services
    medication, _ = Medication.objects.get_or_create(
        name=medication_name, strength="500 mg", form=dosage_forms.CAPSULE,
        defaults={"generic_name": medication_name})
    return pharmacy_services.create_prescription(
        cast["doctor"], cast["patient"].id,
        [{"medication_id": medication.id, "quantity": 21}],
        diagnosis="Throat infection", issue=issue)


def stocked_request(cast):
    """A medication request, with the pharmacy holding stock for it."""
    from pharmacy import services as pharmacy_services
    issued = prescription(cast)
    medication = issued.items.first().medication
    PharmacyInventory.objects.create(pharmacy=cast["pharmacy"],
                                     medication=medication, quantity=100,
                                     price="50.00")
    return pharmacy_services.create_request(
        cast["patient"], cast["pharmacy"].id,
        [{"prescription_item_id": issued.items.first().id}],
        prescription_id=issued.id)


# ---------------------------------------------------------------------------
# One architecture
# ---------------------------------------------------------------------------
class TestOneUnifiedArchitecture:
    def test_the_app_owns_exactly_three_models(self):
        from django.apps import apps
        names = {m.__name__ for m in apps.get_app_config("comms").get_models()}
        assert names == {"Notification", "Conversation", "Message"}

    def test_no_per_role_notification_model_exists_anywhere(self):
        """Section 3: one table, not one per portal."""
        from django.apps import apps
        every = {m.__name__ for m in apps.get_models()}
        for forbidden in ("DoctorNotification", "LabNotification",
                          "RadiologyNotification", "PharmacyNotification",
                          "PatientNotification", "Chat", "ChatRoom"):
            assert forbidden not in every

    def test_every_module_writes_to_the_same_table(self, cast):
        """Three different producers, one table, one shape.

        The booking goes through the engine's own service rather than
        ``Appointment.objects.create``: the callbacks hang off the service, so
        writing the row directly would (correctly) notify nobody.
        """
        released_report(cast)
        stocked_request(cast)
        service = Service.objects.create(provider=cast["doctor"], name="Visit",
                                         duration_minutes=30)
        book(cast["patient"], cast["doctor"],
             (timezone.localtime() + datetime.timedelta(days=4)).date(),
             datetime.time(10, 0), service)

        rows = Notification.objects.all()
        assert rows.count() > 4
        assert {row._meta.label for row in rows} == {"comms.Notification"}
        # From radiology, pharmacy and the scheduling engine alike.
        assert {"radiology.RadiologyReport", "pharmacy.MedicationRequest",
                "appointments.Appointment"} <= {row.source for row in rows}

    def test_the_derived_notification_builder_is_gone_from_the_frontend(self):
        """Section 21: no second architecture running alongside this one."""
        import pathlib
        source = pathlib.Path("streamlit_app.py").read_text(encoding="utf-8")
        assert "_pharmacy_notifications" not in source
        assert "def _build_notifications" not in source

    def test_the_vocabulary_is_declared_in_one_place(self):
        for value in types.ALL:
            assert types.is_valid(value)
            assert types.category_of(value) in types.CATEGORIES
        assert types.LAB_RESULT_RELEASED in types.UNPRODUCIBLE


# ---------------------------------------------------------------------------
# Notifications never carry clinical content
# ---------------------------------------------------------------------------
class TestNoClinicalContentLeaks:
    def test_a_released_report_notifies_without_its_impression(self, cast):
        report = released_report(cast)
        assert types.RADIOLOGY_REPORT_RELEASED in kinds(cast["patient"])
        assert SENTINEL_REPORT not in all_text(cast["patient"])
        assert SENTINEL_REPORT not in all_text(cast["doctor"])
        # It links to the report rather than restating it.
        notice = inbox(cast["patient"], types.RADIOLOGY_REPORT_RELEASED)[0]
        assert notice.source == "radiology.RadiologyReport"
        assert notice.reference == report.id

    def test_a_prescription_notifies_without_naming_the_medication(self, cast):
        issued = prescription(cast)
        assert types.PRESCRIPTION_CREATED in kinds(cast["patient"])
        assert SENTINEL_MEDICATION not in all_text(cast["patient"])
        notice = inbox(cast["patient"], types.PRESCRIPTION_CREATED)[0]
        assert notice.reference == issued.id

    def test_a_message_notifies_without_its_body(self, cast):
        conversation, _created = messaging.start_conversation(
            cast["doctor"], cast["patient"].id)
        messaging.send_message(cast["doctor"], conversation.id,
                               SENTINEL_MESSAGE)
        assert types.MESSAGE_RECEIVED in kinds(cast["patient"])
        assert SENTINEL_MESSAGE not in all_text(cast["patient"])

    def test_a_draft_report_notifies_nobody(self, cast):
        """The release gate is what makes a report a clinical document."""
        from radiology import services as radiology_services
        service = Service.objects.create(provider=cast["center"],
                                         name="MRI Brain",
                                         category=modalities.MRI,
                                         duration_minutes=60)
        booking = appointment(cast["center"], cast["patient"], days=-2,
                              minutes=60, service=service)
        examination = Examination.objects.create(
            appointment=booking, status=Examination.COMPLETED)
        report = RadiologyReport.objects.create(
            examination=examination, author=cast["center"],
            impression=SENTINEL_REPORT, status=RadiologyReport.DRAFT)
        for step in (RadiologyReport.PENDING_REVIEW, RadiologyReport.VERIFIED):
            radiology_services.transition_report(cast["center"], report.id, step)
        assert types.RADIOLOGY_REPORT_RELEASED not in kinds(cast["patient"])

    def test_a_draft_prescription_notifies_nobody(self, cast):
        """Telling a patient about a draft leaks that a draft exists."""
        prescription(cast, issue=False)
        assert types.PRESCRIPTION_CREATED not in kinds(cast["patient"])



# ---------------------------------------------------------------------------
# A notification can never break what raised it
# ---------------------------------------------------------------------------
class TestNotificationsAreNonFatal:
    def test_a_broken_channel_does_not_break_the_clinical_action(self, cast):
        def explode(notification):
            raise RuntimeError("the pager is on fire")

        channels.register("email")(explode)
        try:
            report = released_report(cast)
            report.refresh_from_db()
            # The release went through, and so did the notification row.
            assert report.status == RadiologyReport.RELEASED
            assert types.RADIOLOGY_REPORT_RELEASED in kinds(cast["patient"])
        finally:
            channels._DELIVERY.pop("email", None)

    def test_a_failed_notification_write_does_not_break_the_booking(
            self, cast, monkeypatch):
        """The savepoint is the point: a poisoned transaction would take the
        booking down with it."""
        from comms.models import Notification as NotificationModel

        original = NotificationModel.objects.create

        def fail(*args, **kwargs):
            raise RuntimeError("notification table unavailable")

        monkeypatch.setattr(NotificationModel.objects, "create", fail)
        service = Service.objects.create(provider=cast["doctor"],
                                         name="Consultation",
                                         duration_minutes=30)
        day = (timezone.localtime() + datetime.timedelta(days=3)).date()
        booking = book(cast["patient"], cast["doctor"], day,
                       datetime.time(11, 0), service)
        monkeypatch.setattr(NotificationModel.objects, "create", original)

        booking.refresh_from_db()
        assert booking.status == Appointment.SCHEDULED

    def test_an_unknown_type_is_refused_rather_than_stored(self, cast):
        """An unfilterable row would be invisible in the UI."""
        before = Notification.objects.count()
        assert notifications.notify(cast["patient"], "not_a_real_type",
                                    "x") is None
        assert Notification.objects.count() == before


# ---------------------------------------------------------------------------
# The six workflows the brief names
# ---------------------------------------------------------------------------
class TestWorkflows:
    def test_workflow_1_prescription(self, client, cast):
        issued = prescription(cast)
        as_user(client, cast["patient_token"])
        payload = client.get("/api/notifications/?type=prescription_created").data
        assert payload["results"][0]["reference"] == issued.id
        # …and the deep link opens the real prescription.
        assert client.get(
            f"/api/pharmacy/prescriptions/{issued.id}/").status_code == 200

    def test_workflow_3_radiology(self, client, cast):
        report = released_report(cast)
        as_user(client, cast["patient_token"])
        payload = client.get(
            "/api/notifications/?type=radiology_report_released").data
        assert payload["results"][0]["reference"] == report.id
        assert client.get("/api/radiology/reports/").status_code == 200

    def test_workflow_4_pharmacy_round_trip(self, client, cast):
        from pharmacy import services as pharmacy_services
        request = stocked_request(cast)
        # The pharmacy hears about it.
        assert types.PHARMACY_REQUEST_CREATED in kinds(cast["pharmacy"])

        pharmacy_services.transition_request(
            cast["pharmacy"], request.id, "confirmed")
        assert types.PHARMACY_REQUEST_CONFIRMED in kinds(cast["patient"])

        for step, expected in (("preparing", types.PHARMACY_ORDER_PREPARING),
                               ("ready", types.PHARMACY_ORDER_READY),
                               ("completed", types.PHARMACY_ORDER_COMPLETED)):
            pharmacy_services.transition_request(cast["pharmacy"], request.id,
                                                 step)
            assert expected in kinds(cast["patient"])

        # The pharmacy is never notified about its own actions.
        assert types.PHARMACY_ORDER_READY not in kinds(cast["pharmacy"])

    def test_workflow_5_messaging_both_directions(self, client, cast):
        conversation, _c = messaging.start_conversation(cast["doctor"],
                                                        cast["patient"].id)
        messaging.send_message(cast["doctor"], conversation.id, "How are you?")
        assert types.MESSAGE_RECEIVED in kinds(cast["patient"])
        assert types.MESSAGE_RECEIVED not in kinds(cast["doctor"])

        messaging.send_message(cast["patient"], conversation.id, "Much better.")
        assert types.MESSAGE_RECEIVED in kinds(cast["doctor"])

    def test_workflow_6_appointment_lifecycle(self, cast):
        service = Service.objects.create(provider=cast["doctor"],
                                         name="Consultation",
                                         duration_minutes=30)
        day = (timezone.localtime() + datetime.timedelta(days=3)).date()
        booking = book(cast["patient"], cast["doctor"], day,
                       datetime.time(11, 0), service)
        # Both sides, in their own words.
        assert types.APPOINTMENT_CREATED in kinds(cast["patient"])
        assert types.APPOINTMENT_CREATED in kinds(cast["doctor"])

        scheduling.reschedule_appointment(cast["patient"], booking.id, day,
                                          datetime.time(12, 0))
        assert types.APPOINTMENT_RESCHEDULED in kinds(cast["patient"])
        moved = inbox(cast["patient"], types.APPOINTMENT_RESCHEDULED)[0]
        assert "11:00" in moved.body and "12:00" in moved.body

        scheduling.set_outcome(cast["doctor"], booking.id,
                               Appointment.COMPLETED)
        assert types.APPOINTMENT_COMPLETED in kinds(cast["patient"])

    def test_cancelling_notifies_both_parties(self, cast):
        service = Service.objects.create(provider=cast["doctor"], name="Visit",
                                         duration_minutes=30)
        day = (timezone.localtime() + datetime.timedelta(days=3)).date()
        booking = book(cast["patient"], cast["doctor"], day,
                       datetime.time(14, 0), service)
        scheduling.cancel_appointment(cast["patient"], booking.id, "Unwell")
        assert types.APPOINTMENT_CANCELLED in kinds(cast["patient"])
        assert types.APPOINTMENT_CANCELLED in kinds(cast["doctor"])

    def test_a_no_show_does_not_notify_the_patient(self, cast):
        """Not an accusation delivered to a bell."""
        service = Service.objects.create(provider=cast["doctor"], name="Visit",
                                         duration_minutes=30)
        day = (timezone.localtime() + datetime.timedelta(days=3)).date()
        booking = book(cast["patient"], cast["doctor"], day,
                       datetime.time(15, 0), service)
        scheduling.set_outcome(cast["doctor"], booking.id, Appointment.NO_SHOW)
        assert types.APPOINTMENT_COMPLETED not in kinds(cast["patient"])

    def test_unrelated_users_are_never_notified(self, cast):
        """Section 8: no broadcast."""
        bystander, _token = make(roles.PATIENT, "cm_bystander")
        other_doctor, _t = make(roles.DOCTOR, "cm_other_doctor")
        released_report(cast)
        stocked_request(cast)
        assert inbox(bystander) == []
        assert inbox(other_doctor) == []


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------
class TestReminders:
    def test_a_reminder_is_raised_for_a_soon_appointment(self, cast):
        start = timezone.now() + datetime.timedelta(hours=6)
        Appointment.objects.create(
            provider=cast["doctor"], patient=cast["patient"], start_at=start,
            end_at=start + datetime.timedelta(minutes=30),
            status=Appointment.SCHEDULED)
        raised = notifications.create_due_reminders(within_hours=24)
        assert len(raised) == 1
        assert types.APPOINTMENT_REMINDER in kinds(cast["patient"])

    def test_running_twice_does_not_remind_twice(self, cast):
        """Guaranteed by a partial unique constraint, not by a flag."""
        start = timezone.now() + datetime.timedelta(hours=6)
        Appointment.objects.create(
            provider=cast["doctor"], patient=cast["patient"], start_at=start,
            end_at=start + datetime.timedelta(minutes=30),
            status=Appointment.SCHEDULED)
        notifications.create_due_reminders()
        notifications.create_due_reminders()
        assert len(inbox(cast["patient"], types.APPOINTMENT_REMINDER)) == 1

    def test_the_constraint_is_enforced_by_the_database(self, cast):
        """Written directly, bypassing the service."""
        from django.db import IntegrityError, transaction
        Notification.objects.create(
            recipient=cast["patient"], type=types.APPOINTMENT_REMINDER,
            title="Upcoming", source="appointments.Appointment", reference=99)
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Notification.objects.create(
                    recipient=cast["patient"],
                    type=types.APPOINTMENT_REMINDER, title="Upcoming again",
                    source="appointments.Appointment", reference=99)

    def test_distant_appointments_are_not_reminded(self, cast):
        start = timezone.now() + datetime.timedelta(days=9)
        Appointment.objects.create(
            provider=cast["doctor"], patient=cast["patient"], start_at=start,
            end_at=start + datetime.timedelta(minutes=30),
            status=Appointment.SCHEDULED)
        assert notifications.create_due_reminders(within_hours=24) == []

    def test_nothing_runs_reminders_automatically(self, cast):
        """Section 19: no background jobs were invented."""
        start = timezone.now() + datetime.timedelta(hours=2)
        Appointment.objects.create(
            provider=cast["doctor"], patient=cast["patient"], start_at=start,
            end_at=start + datetime.timedelta(minutes=30),
            status=Appointment.SCHEDULED)
        assert types.APPOINTMENT_REMINDER not in kinds(cast["patient"])


# ---------------------------------------------------------------------------
# The notification API
# ---------------------------------------------------------------------------
class TestNotificationAPI:
    def test_listing_read_state_and_counts(self, client, cast):
        released_report(cast)
        prescription(cast)
        as_user(client, cast["patient_token"])

        listed = client.get("/api/notifications/")
        assert listed.status_code == 200
        assert listed.data["total"] >= 2
        assert listed.data["unread"] == listed.data["total"]

        first = listed.data["results"][0]
        assert first["is_read"] is False

        read = client.post(f"/api/notifications/{first['id']}/read/", {},
                           format="json")
        assert read.status_code == 200 and read.data["is_read"] is True
        assert client.get("/api/notifications/unread/").data["unread"] == \
            listed.data["total"] - 1

        # And back again.
        client.post(f"/api/notifications/{first['id']}/read/", {"read": False},
                    format="json")
        assert client.get("/api/notifications/unread/").data["unread"] == \
            listed.data["total"]

    def test_mark_all_read(self, client, cast):
        released_report(cast)
        prescription(cast)
        as_user(client, cast["patient_token"])
        res = client.post("/api/notifications/read-all/", {}, format="json")
        assert res.status_code == 200 and res.data["unread"] == 0
        assert client.get("/api/notifications/?unread=true").data["results"] == []

    def test_filtering_by_category_and_type(self, client, cast):
        released_report(cast)
        prescription(cast)
        as_user(client, cast["patient_token"])
        medical = client.get("/api/notifications/?category=medical").data
        assert medical["results"]
        assert all(n["category"] == "medical" for n in medical["results"])

        one = client.get(
            "/api/notifications/?type=prescription_created").data
        assert all(n["type"] == "prescription_created"
                   for n in one["results"])

    def test_an_unknown_filter_is_an_error_not_an_empty_list(self, client, cast):
        as_user(client, cast["patient_token"])
        assert client.get(
            "/api/notifications/?category=astrology").status_code == 400
        assert client.get(
            "/api/notifications/?type=nonsense").status_code == 400

    def test_pagination(self, client, cast):
        for index in range(8):
            notifications.notify(cast["patient"], types.SYSTEM_NOTIFICATION,
                                 f"Notice {index}")
        as_user(client, cast["patient_token"])
        page = client.get("/api/notifications/?limit=3").data
        assert page["count"] == 3 and page["has_more"] is True
        second = client.get("/api/notifications/?limit=3&offset=3").data
        assert not ({n["id"] for n in page["results"]}
                    & {n["id"] for n in second["results"]})

    def test_the_page_size_is_capped(self, client, cast):
        as_user(client, cast["patient_token"])
        assert client.get("/api/notifications/?limit=9999").data["limit"] <= 100

    def test_the_vocabulary_and_channels_are_served(self, client, cast):
        as_user(client, cast["patient_token"])
        res = client.get("/api/notifications/types/")
        assert res.status_code == 200
        assert {c["value"] for c in res.data["categories"]} == set(types.CATEGORIES)
        by_value = {t["value"]: t for t in res.data["types"]}
        assert by_value["lab_result_released"]["available"] is False
        assert by_value["radiology_report_released"]["available"] is True
        # Only in-app has a backend; nothing claims email works.
        enabled = {c["value"] for c in res.data["channels"] if c["enabled"]}
        assert enabled == {"in_app"}

    def test_every_role_has_a_notification_centre(self, client, cast):
        """Facilities get operational notifications, so they need one too."""
        stocked_request(cast)
        as_user(client, cast["pharmacy_token"])
        res = client.get("/api/notifications/")
        assert res.status_code == 200
        assert res.data["total"] >= 1

    def test_anonymous_callers_get_nothing(self, client, cast):
        client.credentials()
        for path in ("/api/notifications/", "/api/notifications/unread/",
                     "/api/notifications/types/", "/api/conversations/"):
            assert client.get(path).status_code == 401, path


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------
class TestMessaging:
    def test_a_conversation_needs_a_care_relationship(self, cast):
        stranger, _token = make(roles.DOCTOR, "cm_stranger_doctor")
        with pytest.raises(messaging.NotAuthorized):
            messaging.start_conversation(cast["patient"], stranger.id)

    def test_starting_twice_returns_the_same_thread(self, cast):
        first, created_one = messaging.start_conversation(cast["patient"],
                                                          cast["doctor"].id)
        second, created_two = messaging.start_conversation(cast["doctor"],
                                                           cast["patient"].id)
        assert first.id == second.id
        assert created_one is True and created_two is False
        assert Conversation.objects.count() == 1

    def test_the_database_refuses_a_duplicate_thread(self, cast):
        from django.db import IntegrityError, transaction
        Conversation.objects.create(patient=cast["patient"],
                                    doctor=cast["doctor"])
        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Conversation.objects.create(patient=cast["patient"],
                                            doctor=cast["doctor"])

    def test_sending_and_reading(self, client, cast):
        conversation, _c = messaging.start_conversation(cast["patient"],
                                                        cast["doctor"].id)
        as_user(client, cast["patient_token"])
        sent = client.post(
            f"/api/conversations/{conversation.id}/messages/",
            {"body": "I have a question about my prescription."},
            format="json")
        assert sent.status_code == 201
        assert sent.data["is_mine"] is True
        assert sent.data["is_read"] is False

        as_user(client, cast["doctor_token"])
        thread = client.get(f"/api/conversations/{conversation.id}/messages/")
        assert thread.status_code == 200
        assert thread.data["results"][0]["is_mine"] is False

        client.post(f"/api/conversations/{conversation.id}/read/", {},
                    format="json")
        assert Message.objects.get(pk=sent.data["id"]).read_at is not None

    def test_reading_does_not_mark_your_own_messages_read(self, cast):
        conversation, _c = messaging.start_conversation(cast["patient"],
                                                        cast["doctor"].id)
        mine = messaging.send_message(cast["patient"], conversation.id, "Hello")
        messaging.mark_conversation_read(cast["patient"], conversation.id)
        mine.refresh_from_db()
        assert mine.read_at is None

    def test_an_empty_message_is_refused(self, cast):
        conversation, _c = messaging.start_conversation(cast["patient"],
                                                        cast["doctor"].id)
        with pytest.raises(messaging.InvalidMessage):
            messaging.send_message(cast["patient"], conversation.id, "   ")

    def test_contacts_come_from_the_care_relationship(self, client, cast):
        make(roles.DOCTOR, "cm_unrelated_doctor")
        as_user(client, cast["patient_token"])
        res = client.get("/api/conversations/contacts/")
        assert res.status_code == 200
        assert [c["username"] for c in res.data] == ["cm_doctor"]

    def test_message_pagination_returns_the_latest_page(self, client, cast):
        conversation, _c = messaging.start_conversation(cast["patient"],
                                                        cast["doctor"].id)
        for index in range(12):
            messaging.send_message(cast["patient"], conversation.id,
                                   f"message {index}")
        as_user(client, cast["patient_token"])
        page = client.get(
            f"/api/conversations/{conversation.id}/messages/?limit=5").data
        assert page["total"] == 12 and page["has_more"] is True
        # The newest five, in reading order.
        assert [m["body"] for m in page["results"]] == [
            f"message {i}" for i in range(7, 12)]

    def test_one_unread_badge_per_conversation(self, cast):
        conversation, _c = messaging.start_conversation(cast["doctor"],
                                                        cast["patient"].id)
        for index in range(5):
            messaging.send_message(cast["doctor"], conversation.id,
                                   f"note {index}")
        # Five messages, one notification — not five identical badges.
        assert len(inbox(cast["patient"], types.MESSAGE_RECEIVED)) == 1

    def test_opening_a_conversation_clears_its_notification(self, cast):
        conversation, _c = messaging.start_conversation(cast["doctor"],
                                                        cast["patient"].id)
        messaging.send_message(cast["doctor"], conversation.id, "Hello")
        assert notifications.unread_count(cast["patient"]) == 1
        messaging.mark_conversation_read(cast["patient"], conversation.id)
        assert notifications.unread_count(cast["patient"]) == 0

    def test_messages_never_enter_the_medical_record(self, client, cast):
        """Section 30: chat is not a clinical record."""
        conversation, _c = messaging.start_conversation(cast["doctor"],
                                                        cast["patient"].id)
        messaging.send_message(cast["doctor"], conversation.id,
                               SENTINEL_MESSAGE)
        as_user(client, cast["patient_token"])
        timeline = client.get("/api/records/me/timeline/?limit=100")
        assert timeline.status_code == 200
        assert SENTINEL_MESSAGE not in str(timeline.data)
        assert "message" not in {e["type"] for e in timeline.data["results"]}


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
class TestSecurity:
    def test_a_patient_cannot_read_another_patients_notifications(
            self, client, cast):
        released_report(cast)
        _other, other_token = make(roles.PATIENT, "cm_nosy")
        as_user(client, other_token)
        assert client.get("/api/notifications/").data["results"] == []

    def test_a_patient_cannot_mark_another_patients_notification_read(
            self, client, cast):
        released_report(cast)
        target = inbox(cast["patient"])[0]
        _other, other_token = make(roles.PATIENT, "cm_nosy_two")
        as_user(client, other_token)
        res = client.post(f"/api/notifications/{target.id}/read/", {},
                          format="json")
        assert res.status_code == 404
        target.refresh_from_db()
        assert target.read_at is None

    def test_mark_all_read_touches_only_your_own(self, client, cast):
        released_report(cast)
        other, other_token = make(roles.PATIENT, "cm_nosy_three")
        notifications.notify(other, types.SYSTEM_NOTIFICATION, "Theirs")
        as_user(client, other_token)
        client.post("/api/notifications/read-all/", {}, format="json")
        assert notifications.unread_count(cast["patient"]) > 0

    def test_a_patient_cannot_read_another_patients_conversation(
            self, client, cast):
        conversation, _c = messaging.start_conversation(cast["patient"],
                                                        cast["doctor"].id)
        messaging.send_message(cast["patient"], conversation.id,
                               SENTINEL_MESSAGE)
        _other, other_token = make(roles.PATIENT, "cm_nosy_four")
        as_user(client, other_token)
        assert client.get("/api/conversations/").data == []
        res = client.get(f"/api/conversations/{conversation.id}/messages/")
        assert res.status_code == 404
        assert SENTINEL_MESSAGE not in str(res.data)

    def test_a_patient_cannot_post_into_another_patients_conversation(
            self, client, cast):
        conversation, _c = messaging.start_conversation(cast["patient"],
                                                        cast["doctor"].id)
        _other, other_token = make(roles.PATIENT, "cm_nosy_five")
        as_user(client, other_token)
        res = client.post(f"/api/conversations/{conversation.id}/messages/",
                          {"body": "let me in"}, format="json")
        assert res.status_code == 404
        assert Message.objects.filter(conversation=conversation).count() == 0

    def test_a_doctor_cannot_read_another_doctors_conversation(
            self, client, cast):
        conversation, _c = messaging.start_conversation(cast["doctor"],
                                                        cast["patient"].id)
        other, other_token = make(roles.DOCTOR, "cm_other_doc")
        # Even with their own care relationship to the same patient.
        appointment(other, cast["patient"], days=-6)
        as_user(client, other_token)
        assert client.get("/api/conversations/").data == []
        assert client.get(
            f"/api/conversations/{conversation.id}/messages/").status_code == 404

    def test_a_pharmacy_has_no_messaging_surface(self, client, cast):
        conversation, _c = messaging.start_conversation(cast["patient"],
                                                        cast["doctor"].id)
        as_user(client, cast["pharmacy_token"])
        assert client.get("/api/conversations/").data == []
        assert client.get("/api/conversations/contacts/").data == []
        assert client.get(
            f"/api/conversations/{conversation.id}/messages/").status_code == 404

    def test_a_radiology_centre_has_no_messaging_surface(self, client, cast):
        as_user(client, cast["center_token"])
        assert client.get("/api/conversations/").data == []
        assert client.get("/api/conversations/contacts/").data == []

    def test_a_facility_cannot_open_a_conversation(self, cast):
        with pytest.raises(messaging.NotAuthorized):
            messaging.start_conversation(cast["pharmacy"], cast["patient"].id)

    def test_messages_cannot_be_edited_or_deleted_through_the_api(
            self, client, cast):
        conversation, _c = messaging.start_conversation(cast["patient"],
                                                        cast["doctor"].id)
        messaging.send_message(cast["patient"], conversation.id, "Original")
        as_user(client, cast["patient_token"])
        path = f"/api/conversations/{conversation.id}/messages/"
        for method in ("put", "patch", "delete"):
            assert getattr(client, method)(path, {}, format="json"
                                           ).status_code == 405, method


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------
class TestPerformance:
    def test_the_unread_count_is_one_query(self, cast,
                                           django_assert_max_num_queries):
        for index in range(10):
            notifications.notify(cast["patient"], types.SYSTEM_NOTIFICATION,
                                 f"Notice {index}")
        with django_assert_max_num_queries(1):
            assert notifications.unread_count(cast["patient"]) == 10

    def test_listing_notifications_does_not_scale_with_their_number(
            self, client, cast, django_assert_max_num_queries):
        for index in range(25):
            notifications.notify(cast["patient"], types.SYSTEM_NOTIFICATION,
                                 f"Notice {index}")
        as_user(client, cast["patient_token"])
        with django_assert_max_num_queries(8):
            assert client.get("/api/notifications/?limit=20").status_code == 200

    def test_listing_conversations_does_not_scale_with_their_number(
            self, client, cast, django_assert_max_num_queries):
        for index in range(6):
            doctor, _token = make(roles.DOCTOR, f"cm_bulk_doc_{index}")
            appointment(doctor, cast["patient"], days=-(5 + index))
            conversation, _c = messaging.start_conversation(cast["patient"],
                                                            doctor.id)
            messaging.send_message(cast["patient"], conversation.id, "Hello")

        as_user(client, cast["patient_token"])
        with django_assert_max_num_queries(12):
            listed = client.get("/api/conversations/")
        assert len(listed.data) == 6
        assert all(c["counterparty"] for c in listed.data)


# ---------------------------------------------------------------------------
# Nothing else broke
# ---------------------------------------------------------------------------
class TestExistingModulesStillWork:
    def test_booking_still_works(self, cast):
        service = Service.objects.create(provider=cast["doctor"], name="Visit",
                                         duration_minutes=30)
        day = (timezone.localtime() + datetime.timedelta(days=2)).date()
        booking = book(cast["patient"], cast["doctor"], day,
                       datetime.time(9, 30), service)
        assert booking.status == Appointment.SCHEDULED

    def test_radiology_report_scoping_is_unchanged(self, cast):
        from radiology import services as radiology_services
        released_report(cast)
        assert radiology_services.reports_for(cast["patient"]).count() == 1

    def test_pharmacy_stock_still_moves_correctly(self, cast):
        from pharmacy import services as pharmacy_services
        request = stocked_request(cast)
        line = PharmacyInventory.objects.get(pharmacy=cast["pharmacy"])
        pharmacy_services.transition_request(cast["pharmacy"], request.id,
                                             "confirmed")
        line.refresh_from_db()
        assert line.reserved == 21

    def test_the_medical_record_still_answers(self, client, cast):
        as_user(client, cast["patient_token"])
        assert client.get("/api/records/me/").status_code == 200

    def test_the_dashboard_still_answers(self, client, cast):
        as_user(client, cast["patient_token"])
        assert client.get("/api/dashboard/summary/").status_code == 200
