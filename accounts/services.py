"""Account use-cases: role resolution, registration, login and logout.

One registration workflow serves all five self-service roles. The only thing
that varies between them is which profile row is written, so that is the only
thing this module branches on — everything else (user creation, the uniqueness
race guard, the role record, the token) happens once, in one transaction.

Input validation (uniqueness, password strength, field formats) belongs to the
serializers in the adapter layer; this module owns the workflow.
"""
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from rest_framework.authtoken.models import Token

from appointments.authentication import is_expired

from . import roles
from .models import (
    LaboratoryProfile, PharmacyProfile, RadiologyProfile, UserAccount,
)


class UsernameAlreadyExists(Exception):
    """Raised when a requested username is already taken (race fallback)."""


class InvalidCredentials(Exception):
    """Raised when authentication fails."""


class AccountNotActive(Exception):
    """Raised when the credentials are correct but the account may not sign in.

    Kept distinct from :class:`InvalidCredentials` inside the service so the
    reason is logged accurately. The *API* still answers both with one generic
    message — telling an attacker "correct password, suspended account" confirms
    the password.
    """


class RoleNotSelfService(Exception):
    """Raised when registration is attempted for a role that is not public."""


# ---------------------------------------------------------------------------
# Role resolution
# ---------------------------------------------------------------------------
#: Profile attribute -> role, for users that predate ``UserAccount`` or were
#: created outside registration. Order matters: the most privileged wins, so a
#: mis-seeded user is never quietly upgraded *into* a role by a stray row.
_PROFILE_ROLE_ATTRS = (
    ("doctor_profile", roles.DOCTOR),
    ("laboratory_profile", roles.LABORATORY),
    ("radiology_profile", roles.RADIOLOGY),
    ("pharmacy_profile", roles.PHARMACY),
    ("patient_profile", roles.PATIENT),
)


def role_of(user):
    """The user's role.

    ``UserAccount`` is authoritative. The fallback exists for users created
    outside registration — ``createsuperuser``, the Django admin, test fixtures
    — and deliberately does not write a row: resolving a role is a read, and a
    read that creates records surprises everyone who calls it.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return None

    account = getattr(user, "account", None)
    if account is not None:
        return account.role

    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return roles.ADMIN
    for attribute, role in _PROFILE_ROLE_ATTRS:
        if hasattr(user, attribute):
            return role
    # No account and no profile: the least-privileged role that can still use
    # the product. Every patient capability is scoped to the caller's own data,
    # so this cannot expose another user's records.
    return roles.PATIENT


# Kept under its original name because the rest of the codebase already calls it.
get_user_role = role_of


def capabilities_of(user):
    return roles.capabilities(role_of(user))


def user_has_capability(user, capability):
    return roles.has_capability(role_of(user), capability)


def profile_of(user, role=None):
    """The role-specific profile row for a user, or ``None``."""
    role = role or role_of(user)
    for attribute, attribute_role in _PROFILE_ROLE_ATTRS:
        if attribute_role == role:
            return getattr(user, attribute, None)
    return None


#: Everything ``role_of`` and ``profile_of`` may touch, so resolving a whole
#: directory costs one query rather than one per user.
_DIRECTORY_RELATIONS = ("account",) + tuple(
    attribute for attribute, _role in _PROFILE_ROLE_ATTRS)


def directory(role=None):
    """Every user with their role, status and profile resolved.

    Built from ``User`` rather than from ``UserAccount`` on purpose. A user
    created outside registration — ``createsuperuser`` is the everyday case —
    has no account row and would simply be missing from a ``UserAccount`` query,
    which is precisely the person an administrator most needs to see. Resolving
    through :func:`role_of` means the fallback is applied here exactly as it is
    everywhere else, rather than restated as a second rule in SQL.

    The resolution is a Python pass over the result set. That is the right shape
    while "every user on the platform" is a page of results; it stops being so
    somewhere around tens of thousands of accounts, at which point the fallback
    should be retired by backfilling the missing rows and this becomes an
    ordinary indexed ``UserAccount`` query again.
    """
    entries = []
    for user in User.objects.select_related(*_DIRECTORY_RELATIONS).order_by("id"):
        resolved = role_of(user)
        if role and resolved != role:
            continue
        account = getattr(user, "account", None)
        entries.append({
            "user": user,
            "role": resolved,
            # No account row means nothing has ever suspended them.
            "status": account.status if account else UserAccount.ACTIVE,
            "profile": profile_of(user, resolved),
        })
    return entries


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def _create_patient_profile(user, name, fields):
    from appointments.models import PatientProfile
    return PatientProfile.objects.create(
        user=user,
        age=fields.get("age", 0),
        gender=fields.get("gender", ""),
        address=fields.get("address", ""),
        phone=fields.get("phone", ""),
    )


def _create_doctor_profile(user, name, fields):
    from appointments.models import Doctor
    return Doctor.objects.create(
        user=user, name=name,
        specialization=fields.get("specialization", ""),
        license_number=fields.get("license_number", ""),
        phone=fields.get("phone", ""),
        clinic=fields.get("clinic", ""),
    )


def _facility_profile_factory(model):
    def create(user, name, fields):
        return model.objects.create(
            user=user, name=name,
            license_number=fields.get("license_number", ""),
            phone=fields.get("phone", ""),
            email=fields.get("email", ""),
            address=fields.get("address", ""),
            services=fields.get("services", ""),
            operating_hours=fields.get("operating_hours", ""),
        )
    return create


#: role -> callable(user, name, fields) -> profile instance.
PROFILE_BUILDERS = {
    roles.PATIENT: _create_patient_profile,
    roles.DOCTOR: _create_doctor_profile,
    roles.LABORATORY: _facility_profile_factory(LaboratoryProfile),
    roles.RADIOLOGY: _facility_profile_factory(RadiologyProfile),
    roles.PHARMACY: _facility_profile_factory(PharmacyProfile),
}


def register_account(role, username, password, name, **profile_fields):
    """Create a user, their role record and their profile; return them + a token.

    Returns ``(user, account, profile, token)``.

    The whole thing is one transaction: a user without a role record would
    authenticate but resolve through the legacy fallback, and a role record
    without a profile would render an empty portal. Neither half is useful
    alone, so neither is allowed to survive on its own.
    """
    if role not in roles.SELF_SERVICE_ROLES:
        raise RoleNotSelfService(
            f"{role!r} accounts cannot be created through registration.")

    builder = PROFILE_BUILDERS[role]
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=username, password=password, first_name=name,
                email=profile_fields.get("email", ""))
            account = UserAccount.objects.create(user=user, role=role,
                                                 status=UserAccount.ACTIVE)
            profile = builder(user, name, profile_fields)
    except IntegrityError:
        # The serializer already checked availability; this catches the race
        # between that check and the insert.
        raise UsernameAlreadyExists()

    token, _ = Token.objects.get_or_create(user=user)
    return user, account, profile, token


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
def login_user(username, password):
    """Authenticate credentials and return ``(token, role)``.

    An expired token is replaced rather than handed back, so a stale credential
    can never be revived by signing in again. A still-valid token is reused,
    which keeps the user's other active sessions working.
    """
    user = authenticate(username=username, password=password)
    if not user:
        raise InvalidCredentials()

    account = getattr(user, "account", None)
    if account is not None and not account.can_authenticate:
        raise AccountNotActive(account.status)

    token, created = Token.objects.get_or_create(user=user)
    if not created and is_expired(token):
        token.delete()
        token = Token.objects.create(user=user)
    return token, role_of(user)


def logout_user(user):
    """Invalidate the user's auth token (server-side logout)."""
    Token.objects.filter(user=user).delete()
