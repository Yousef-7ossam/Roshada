"""The six-role authentication and authorization system.

The tests that matter most here are the negative ones. Authentication is easy to
demonstrate — someone signs in and something appears. Authorization is only
proven by what is *refused*, so most of this file is one role reaching for
another role's endpoint and being turned away by the API rather than by a hidden
menu item.
"""
import pytest
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from accounts import roles
from accounts.models import (
    LaboratoryProfile, PharmacyProfile, RadiologyProfile, UserAccount,
)
from accounts.services import register_account, role_of

pytestmark = pytest.mark.django_db

STRONG_PASSWORD = "Str0ng!Passw0rd"


@pytest.fixture(autouse=True)
def _no_throttling(settings):
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK,
                               "DEFAULT_THROTTLE_CLASSES": []}


@pytest.fixture
def client():
    return APIClient()


#: Minimal valid signup body per role — the extra fields each form requires.
SIGNUP_EXTRAS = {
    roles.PATIENT: {"age": 30},
    roles.DOCTOR: {"specialization": "Cardiology"},
    roles.LABORATORY: {"services": "CBC, HbA1c"},
    roles.RADIOLOGY: {"services": "MRI, CT"},
    roles.PHARMACY: {"operating_hours": "9-5"},
}


def signup(client, role, username=None):
    """Register an account of ``role`` and return its token."""
    username = username or f"{role}_user"
    res = client.post(f"/api/signup/{role}/",
                      {"username": username, "password": STRONG_PASSWORD,
                       "name": f"{role.title()} One", **SIGNUP_EXTRAS[role]},
                      format="json")
    assert res.status_code == 201, res.data
    return res.data["token"]


@pytest.fixture
def tokens(client):
    """One signed-in account of every role, admin included."""
    issued = {role: signup(client, role) for role in roles.SELF_SERVICE_ROLES}

    # Administrators are created through Django's own mechanism, never through
    # registration — which is exactly what this fixture has to model.
    admin = User.objects.create_superuser("root_admin", "root@example.com",
                                          STRONG_PASSWORD)
    UserAccount.objects.create(user=admin, role=roles.ADMIN)
    res = client.post("/api/login/",
                      {"username": "root_admin", "password": STRONG_PASSWORD},
                      format="json")
    assert res.status_code == 200
    issued[roles.ADMIN] = res.data["token"]
    return issued


def as_role(client, tokens, role):
    client.credentials(HTTP_AUTHORIZATION=f"Token {tokens[role]}")
    return client


# ---------------------------------------------------------------------------
# The role matrix itself
# ---------------------------------------------------------------------------
class TestRoleDefinitions:
    def test_there_are_exactly_six_roles(self):
        assert len(roles.ALL_ROLES) == 6
        assert set(roles.ALL_ROLES) == {
            "patient", "doctor", "laboratory", "radiology", "pharmacy", "admin"}

    def test_admin_is_not_publicly_registerable(self):
        """The one role that can read the whole platform must not be obtainable
        from the signup form."""
        assert roles.ADMIN not in roles.SELF_SERVICE_ROLES

    def test_every_role_has_a_permission_entry(self):
        """A role missing from the matrix would silently hold nothing, which
        looks like a bug report ("my account can't do anything") rather than a
        misconfiguration."""
        assert set(roles.PERMISSIONS) == set(roles.ALL_ROLES)

    def test_no_role_holds_a_capability_that_is_not_declared(self):
        """Catches a typo'd constant, which would grant nothing but read as a
        grant in the matrix."""
        declared = {
            value for name, value in vars(roles).items()
            if name.isupper() and isinstance(value, str) and "." in value
        }
        assert roles.ALL_CAPABILITIES <= declared

    def test_only_the_clinical_roles_may_use_the_assistant(self):
        holders = {role for role in roles.ALL_ROLES
                   if roles.has_capability(role, roles.AI_ASSISTANT)}
        assert holders == set(roles.AI_ROLES) == {roles.PATIENT, roles.DOCTOR}

    def test_only_admin_holds_platform_administration(self):
        holders = {role for role in roles.ALL_ROLES
                   if roles.has_capability(role, roles.ADMIN_PLATFORM)}
        assert holders == {roles.ADMIN}

    def test_an_unknown_role_holds_nothing(self):
        """Fail closed: a role string the matrix does not know grants no access
        rather than falling through to a default set."""
        assert roles.capabilities("superuser_of_everything") == frozenset()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
class TestRegistration:
    @pytest.mark.parametrize("role", roles.SELF_SERVICE_ROLES)
    def test_every_public_role_can_register_and_gets_that_role(self, client, role):
        token = signup(client, role)
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        res = client.get("/api/profile/")
        assert res.status_code == 200
        assert res.data["role"] == role

    @pytest.mark.parametrize("role", roles.SELF_SERVICE_ROLES)
    def test_registration_creates_the_role_record_and_the_profile(self, client, role):
        signup(client, role)
        user = User.objects.get(username=f"{role}_user")
        assert user.account.role == role
        assert user.account.status == UserAccount.ACTIVE
        assert role_of(user) == role
        # The profile half of the transaction landed too.
        from accounts.services import profile_of
        assert profile_of(user) is not None

    def test_there_is_no_public_admin_registration_endpoint(self, client):
        res = client.post("/api/signup/admin/",
                          {"username": "sneaky", "password": STRONG_PASSWORD,
                           "name": "Sneaky"}, format="json")
        assert res.status_code == 404

    def test_the_service_refuses_to_register_an_admin(self):
        """Belt and braces: even a caller that reaches the service directly
        cannot mint an administrator."""
        from accounts.services import RoleNotSelfService
        with pytest.raises(RoleNotSelfService):
            register_account(roles.ADMIN, username="sneaky2",
                             password=STRONG_PASSWORD, name="Sneaky")

    def test_a_failed_registration_leaves_nothing_behind(self, client):
        """The user, role record and profile are written in one transaction, so
        a half-registered account cannot exist."""
        res = client.post("/api/signup/doctor/",
                          {"username": "weakpass", "password": "123",
                           "name": "W", "specialization": "X"}, format="json")
        assert res.status_code == 400
        assert not User.objects.filter(username="weakpass").exists()
        assert not UserAccount.objects.filter(user__username="weakpass").exists()

    def test_a_facility_cannot_register_itself_as_verified(self, client):
        """Verification is an administrator's judgement. A facility that could
        set the flag would make it meaningless."""
        res = client.post("/api/signup/laboratory/",
                          {"username": "fakelab", "password": STRONG_PASSWORD,
                           "name": "Totally Legit Labs", "verified": True},
                          format="json")
        assert res.status_code == 201
        assert LaboratoryProfile.objects.get(user__username="fakelab").verified is False

    def test_duplicate_usernames_are_rejected_across_roles(self, client):
        """One credential store: a username taken by a pharmacy is not available
        to a patient."""
        signup(client, roles.PHARMACY, username="shared_name")
        res = client.post("/api/signup/patient/",
                          {"username": "shared_name", "password": STRONG_PASSWORD,
                           "name": "Someone"}, format="json")
        assert res.status_code == 400


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
class TestLogin:
    @pytest.mark.parametrize("role", roles.SELF_SERVICE_ROLES)
    def test_one_login_endpoint_serves_every_role(self, client, role):
        signup(client, role)
        res = client.post("/api/login/",
                          {"username": f"{role}_user", "password": STRONG_PASSWORD},
                          format="json")
        assert res.status_code == 200
        assert res.data["role"] == role

    def test_a_suspended_account_cannot_sign_in(self, client):
        signup(client, roles.PHARMACY)
        account = UserAccount.objects.get(user__username="pharmacy_user")
        account.status = UserAccount.SUSPENDED
        account.save()

        res = client.post("/api/login/",
                          {"username": "pharmacy_user", "password": STRONG_PASSWORD},
                          format="json")
        assert res.status_code == 400

    def test_a_suspended_account_is_refused_indistinguishably(self, client):
        """The message must match a wrong password exactly. "Correct password,
        suspended account" confirms the password to whoever is guessing."""
        signup(client, roles.PATIENT)
        wrong = client.post("/api/login/",
                            {"username": "patient_user", "password": "wrong-one"},
                            format="json")

        UserAccount.objects.filter(user__username="patient_user").update(
            status=UserAccount.SUSPENDED)
        suspended = client.post(
            "/api/login/",
            {"username": "patient_user", "password": STRONG_PASSWORD},
            format="json")

        assert wrong.status_code == suspended.status_code == 400
        assert wrong.data == suspended.data


# ---------------------------------------------------------------------------
# Cross-role authorization — the point of the exercise
# ---------------------------------------------------------------------------
class TestCrossRoleAccessIsRefused:
    @pytest.mark.parametrize("role", [roles.PATIENT, roles.LABORATORY,
                                      roles.RADIOLOGY, roles.PHARMACY])
    def test_only_a_doctor_reads_the_doctor_schedule(self, client, tokens, role):
        assert as_role(client, tokens, role).get(
            "/api/appointments/doctor/").status_code == 403

    @pytest.mark.parametrize("role", [roles.DOCTOR, roles.LABORATORY,
                                      roles.RADIOLOGY, roles.PHARMACY,
                                      roles.ADMIN])
    def test_only_a_patient_books_an_appointment(self, client, tokens, role):
        assert as_role(client, tokens, role).post(
            "/api/appointment/create/", {}, format="json").status_code == 403

    @pytest.mark.parametrize("role", [roles.LABORATORY, roles.RADIOLOGY,
                                      roles.PHARMACY, roles.ADMIN])
    @pytest.mark.parametrize("endpoint", ["/api/chat/history/", "/api/chat/context/",
                                          "/api/chat/status/"])
    def test_the_ai_assistant_is_refused_to_unauthorized_roles(
            self, client, tokens, role, endpoint):
        """Hiding the page would not be enough — the endpoint itself refuses."""
        assert as_role(client, tokens, role).get(endpoint).status_code == 403

    @pytest.mark.parametrize("role", [roles.PATIENT, roles.DOCTOR])
    def test_the_ai_assistant_is_allowed_for_the_clinical_roles(
            self, client, tokens, role):
        assert as_role(client, tokens, role).get(
            "/api/chat/history/").status_code == 200

    @pytest.mark.parametrize("role", [roles.LABORATORY, roles.RADIOLOGY,
                                      roles.PHARMACY, roles.ADMIN])
    @pytest.mark.parametrize("endpoint", ["/api/appointments/mine/"])
    def test_own_clinical_record_endpoints_refuse_non_clinical_roles(
            self, client, tokens, role, endpoint):
        """These are scoped to the caller's own data, so a pharmacy would only
        ever get an empty list — but 200-with-[] claims an access the published
        permission matrix withholds. The two must agree."""
        assert as_role(client, tokens, role).get(endpoint).status_code == 403

    @pytest.mark.parametrize("role", [roles.PATIENT, roles.DOCTOR])
    @pytest.mark.parametrize("endpoint", ["/api/appointments/mine/"])
    def test_the_clinical_roles_keep_their_own_record_endpoints(
            self, client, tokens, role, endpoint):
        assert as_role(client, tokens, role).get(endpoint).status_code == 200

    @pytest.mark.parametrize("role", [roles.PATIENT, roles.DOCTOR,
                                      roles.LABORATORY, roles.RADIOLOGY,
                                      roles.PHARMACY])
    def test_no_ordinary_role_reaches_the_admin_directory(self, client, tokens, role):
        assert as_role(client, tokens, role).get(
            "/api/admin/users/").status_code == 403

    def test_the_admin_reaches_the_admin_directory(self, client, tokens):
        res = as_role(client, tokens, roles.ADMIN).get("/api/admin/users/")
        assert res.status_code == 200
        assert res.data["total"] == User.objects.count()

    def test_an_anonymous_caller_reaches_nothing(self, client):
        for endpoint in ("/api/profile/", "/api/admin/users/",
                         "/api/chat/history/", "/api/dashboard/summary/"):
            assert client.get(endpoint).status_code == 401, endpoint

    def test_a_role_is_not_taken_from_the_request(self, client, tokens):
        """The role comes from the token's account, never from the payload.

        Sending ``role: admin`` alongside a patient's token must change nothing.
        """
        patient = as_role(client, tokens, roles.PATIENT)
        assert patient.get("/api/admin/users/", {"role": "admin"}).status_code == 403
        res = patient.put("/api/profile/", {"role": "admin", "first_name": "P"},
                          format="json")
        assert res.status_code == 200
        assert UserAccount.objects.get(user__username="patient_user").role == roles.PATIENT


# ---------------------------------------------------------------------------
# Data isolation
# ---------------------------------------------------------------------------
class TestDataIsolation:
    def test_a_facility_cannot_write_another_roles_columns(self, client, tokens):
        """The update only ever touches the fields belonging to the caller's own
        role, whatever the request body contains."""
        as_role(client, tokens, roles.PHARMACY).put(
            "/api/profile/",
            {"medical_history": "injected", "specialization": "Cardiology",
             "name": "Real Pharmacy"},
            format="json")

        profile = PharmacyProfile.objects.get(user__username="pharmacy_user")
        assert profile.name == "Real Pharmacy"      # its own field was written
        from appointments.models import PatientProfile
        assert not PatientProfile.objects.filter(
            user__username="pharmacy_user").exists()

    def test_a_facility_cannot_verify_itself_through_the_profile_endpoint(
            self, client, tokens):
        as_role(client, tokens, roles.RADIOLOGY).put(
            "/api/profile/", {"verified": True}, format="json")
        assert RadiologyProfile.objects.get(
            user__username="radiology_user").verified is False

    def test_the_profile_endpoint_never_takes_an_id(self, client, tokens):
        """A profile is always the caller's own — there is no id to tamper with."""
        res = as_role(client, tokens, roles.PATIENT).get("/api/profile/")
        assert res.data["username"] == "patient_user"


# ---------------------------------------------------------------------------
# Role-scoped dashboards
# ---------------------------------------------------------------------------
class TestDashboardRouting:
    @pytest.mark.parametrize("role", roles.ALL_ROLES)
    def test_every_role_gets_its_own_dashboard(self, client, tokens, role):
        res = as_role(client, tokens, role).get("/api/dashboard/summary/")
        assert res.status_code == 200
        assert res.data["role"] == role

    @pytest.mark.parametrize("role", roles.FACILITY_ROLES)
    def test_facility_tiles_separate_the_countable_from_the_unbuilt(
            self, client, tokens, role):
        """0 claims we counted and found none; None says there is nothing to
        count. Both are now true — of different tiles.

        Scheduling is real for laboratories and radiology centres, so their
        appointment figures are genuine counts that happen to be zero. Orders,
        samples and results belong to domains that do not exist, and must keep
        reporting None rather than a zero that reads as "nothing pending".
        """
        res = as_role(client, tokens, role).get("/api/dashboard/summary/")
        stats = res.data["stats"]

        for metric in res.data["unsupported_metrics"]:
            assert stats[metric] is None, (
                f"{metric} has no data source but reported {stats[metric]!r}")

        bookable = role in roles.BOOKABLE_ROLES
        assert res.data["bookable"] is bookable
        if bookable:
            assert stats["appointments_today"] == 0
            assert stats["upcoming"] == 0
        else:
            # Pharmacy is not part of the appointment engine.
            assert stats["appointments_today"] is None

    def test_pharmacy_is_not_bookable(self, client, tokens):
        """Dispensing is not an appointment. Keeping pharmacy out of the engine
        is a decision, so it is asserted rather than left implicit."""
        assert roles.PHARMACY not in roles.BOOKABLE_ROLES
        res = client.get("/api/providers/", {"type": roles.PHARMACY})
        assert res.status_code == 400

    def test_the_facility_dashboard_shows_its_own_facility(self, client, tokens):
        res = as_role(client, tokens, roles.LABORATORY).get("/api/dashboard/summary/")
        assert res.data["facility"]["name"] == "Laboratory One"
        assert res.data["facility"]["verified"] is False

    def test_the_admin_dashboard_counts_every_role(self, client, tokens):
        res = as_role(client, tokens, roles.ADMIN).get("/api/dashboard/summary/")
        assert set(res.data["users_by_role"]) == set(roles.ALL_ROLES)
        assert res.data["stats"]["total_users"] == User.objects.count()
        # The matrix the UI draws is the one the API enforces.
        assert res.data["permissions"][roles.PATIENT] == sorted(
            roles.capabilities(roles.PATIENT))

    def test_the_figures_add_up(self, client, tokens):
        res = as_role(client, tokens, roles.ADMIN).get("/api/dashboard/summary/")
        assert res.data["stats"]["total_users"] == sum(
            res.data["users_by_role"].values())

    def test_a_user_with_no_account_row_is_still_administered(self, client, tokens):
        """``createsuperuser`` writes a User and nothing else.

        Counting UserAccount rows would leave exactly that person out of the
        directory and out of every role tally — the one person an administrator
        most needs to see. Both consumers resolve through role_of() instead.
        """
        stray = User.objects.create_superuser("stray_root", "s@example.com",
                                              STRONG_PASSWORD)
        assert not UserAccount.objects.filter(user=stray).exists()

        admin = as_role(client, tokens, roles.ADMIN)
        listing = admin.get("/api/admin/users/")
        rows = {row["username"]: row for row in listing.data["results"]}
        assert "stray_root" in rows, "a user with no account row vanished"
        assert rows["stray_root"]["role"] == roles.ADMIN
        assert listing.data["total"] == User.objects.count()

        summary = admin.get("/api/dashboard/summary/")
        assert summary.data["stats"]["total_users"] == User.objects.count()
        assert summary.data["stats"]["total_users"] == sum(
            summary.data["users_by_role"].values())

    def test_the_directory_can_be_filtered_to_one_role(self, client, tokens):
        res = as_role(client, tokens, roles.ADMIN).get(
            "/api/admin/users/", {"role": roles.PHARMACY})
        assert res.status_code == 200
        assert {row["role"] for row in res.data["results"]} == {roles.PHARMACY}

    def test_an_unknown_role_filter_is_rejected(self, client, tokens):
        assert as_role(client, tokens, roles.ADMIN).get(
            "/api/admin/users/", {"role": "wizard"}).status_code == 400

    def test_the_admin_dashboard_exposes_no_clinical_content(self, client, tokens):
        """An administrator needs platform health, not anybody's medical record."""
        res = as_role(client, tokens, roles.ADMIN).get("/api/dashboard/summary/")
        for forbidden in ("upcoming_appointments", "today"):
            assert forbidden not in res.data


# ---------------------------------------------------------------------------
# Role resolution for accounts created outside registration
# ---------------------------------------------------------------------------
class TestRoleResolutionFallback:
    def test_a_superuser_resolves_to_admin(self):
        user = User.objects.create_superuser("fallback_root", "r@example.com",
                                             STRONG_PASSWORD)
        assert not hasattr(user, "account")
        assert role_of(user) == roles.ADMIN

    def test_a_legacy_doctor_still_resolves_to_doctor(self):
        """Users predating UserAccount had their role inferred from the profile.
        The fallback keeps that working for fixtures and manual creation."""
        from appointments.models import Doctor
        user = User.objects.create_user("legacy_doc", password=STRONG_PASSWORD)
        Doctor.objects.create(user=user, name="Legacy", specialization="GP")
        assert role_of(User.objects.get(pk=user.pk)) == roles.DOCTOR

    def test_a_legacy_facility_resolves_to_its_facility_role(self):
        """Without this the fallback would call a pharmacy a patient — and hand
        it the patient capabilities, including the AI assistant."""
        user = User.objects.create_user("legacy_pharm", password=STRONG_PASSWORD)
        PharmacyProfile.objects.create(user=user, name="Legacy Pharmacy")
        assert role_of(User.objects.get(pk=user.pk)) == roles.PHARMACY

    def test_the_account_record_wins_over_the_profile(self):
        """UserAccount is authoritative; the profile inference is only a
        fallback for users that have no account row."""
        from appointments.models import PatientProfile
        user = User.objects.create_user("conflicted", password=STRONG_PASSWORD)
        PatientProfile.objects.create(user=user)
        UserAccount.objects.create(user=user, role=roles.LABORATORY)
        assert role_of(User.objects.get(pk=user.pk)) == roles.LABORATORY

    def test_role_of_does_not_create_a_row(self):
        """Resolving a role is a read. A read that writes surprises every
        caller — and would mask a missing backfill."""
        user = User.objects.create_user("no_account", password=STRONG_PASSWORD)
        before = UserAccount.objects.count()
        role_of(user)
        assert UserAccount.objects.count() == before

    def test_an_anonymous_user_has_no_role(self):
        from django.contrib.auth.models import AnonymousUser
        assert role_of(AnonymousUser()) is None
        assert role_of(None) is None
