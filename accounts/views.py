"""The authentication surface: registration, login, logout and profile.

These endpoints keep the exact paths they had before roles existed
(``/api/login/``, ``/api/signup/patient/``, …) — only the module they live in
changed, so no client has to be updated.

There is one registration *view*, parameterised by role. Adding a seventh role
later means adding a serializer and one URL line, not another copy of this
logic; that is the concrete form of "do not duplicate authentication for every
role".
"""
import logging

from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.exceptions import api_error

from . import roles, services
from .models import UserAccount
from .permissions import IsPlatformAdmin
from .serializers import (
    FACILITY_SERIALIZERS, LoginSerializer, ProfileUpdateSerializer,
    SignupDoctorSerializer, SignupLaboratorySerializer, SignupPatientSerializer,
    SignupPharmacySerializer, SignupRadiologySerializer,
)

logger = logging.getLogger("appointments")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
class _SignupView(APIView):
    """Base registration endpoint. Subclasses set ``role`` + ``serializer_class``."""

    permission_classes = [AllowAny]
    parser_classes = [JSONParser]
    throttle_scope = 'auth'

    role = None
    serializer_class = None

    def profile_payload(self, profile):
        """Extra response keys for this role. Empty by default."""
        return {}

    def post(self, request):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        fields = dict(serializer.validated_data)
        username = fields.pop("username")
        password = fields.pop("password")
        name = fields.pop("name")

        try:
            _user, account, profile, token = services.register_account(
                self.role, username=username, password=password, name=name,
                **fields)
        except services.UsernameAlreadyExists:
            return api_error("Username already exists.", status.HTTP_400_BAD_REQUEST)
        except services.RoleNotSelfService as exc:
            return api_error(str(exc), status.HTTP_403_FORBIDDEN)

        logger.info("%s registered: %s", roles.label(self.role), username)
        body = {
            "message": f"{roles.label(self.role)} registered",
            "role": account.role,
            "token": token.key,
        }
        body.update(self.profile_payload(profile))
        return Response(body, status=status.HTTP_201_CREATED)


class SignupPatient(_SignupView):
    role = roles.PATIENT
    serializer_class = SignupPatientSerializer


class SignupDoctor(_SignupView):
    role = roles.DOCTOR
    serializer_class = SignupDoctorSerializer

    def profile_payload(self, profile):
        # The "doctor" key is part of the existing client contract.
        from appointments.serializers import DoctorSerializer
        return {"doctor": DoctorSerializer(profile).data}


class _SignupFacility(_SignupView):
    def profile_payload(self, profile):
        serializer = FACILITY_SERIALIZERS[self.role]
        return {"profile": serializer(profile).data}


class SignupLaboratory(_SignupFacility):
    role = roles.LABORATORY
    serializer_class = SignupLaboratorySerializer


class SignupRadiology(_SignupFacility):
    role = roles.RADIOLOGY
    serializer_class = SignupRadiologySerializer


class SignupPharmacy(_SignupFacility):
    role = roles.PHARMACY
    serializer_class = SignupPharmacySerializer


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------
class Login(APIView):
    permission_classes = [AllowAny]
    parser_classes = [JSONParser]
    throttle_scope = 'auth'

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            token, role = services.login_user(**serializer.validated_data)
        except services.InvalidCredentials:
            # Generic message — never reveal whether the username exists.
            return api_error("Invalid credentials", status.HTTP_400_BAD_REQUEST)
        except services.AccountNotActive as exc:
            # Deliberately the *same* message: distinguishing "suspended" from
            # "wrong password" would confirm the password to an attacker.
            logger.warning("Sign-in refused for %s: account is %s",
                           serializer.validated_data["username"], exc)
            return api_error("Invalid credentials", status.HTTP_400_BAD_REQUEST)

        return Response({"token": token.key, "role": role},
                        status=status.HTTP_200_OK)


class Logout(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        services.logout_user(request.user)
        return Response({"message": "Logged out"}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
#: role -> the profile columns a caller of that role may read and write.
#:
#: This is what stops a pharmacy from writing ``medical_history`` by adding it
#: to the request body: the update only ever touches the fields listed for the
#: caller's *own* role, whatever the payload contains.
_FACILITY_FIELDS = ("name", "license_number", "phone", "email", "address",
                    "services", "operating_hours", "available")

ROLE_PROFILE_FIELDS = {
    roles.PATIENT: ("age", "date_of_birth", "gender", "phone", "address",
                    "medical_history"),
    roles.DOCTOR: ("name", "specialization", "license_number", "phone",
                   "clinic", "available"),
    roles.LABORATORY: _FACILITY_FIELDS,
    roles.RADIOLOGY: _FACILITY_FIELDS,
    roles.PHARMACY: _FACILITY_FIELDS,
    roles.ADMIN: (),
}


def profile_payload(user):
    """The caller's own profile. Never accepts an id — always ``request.user``."""
    role = services.role_of(user)
    account = getattr(user, "account", None)

    data = {
        "id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "email": user.email,
        "role": role,
        "role_label": roles.label(role),
        # No account row means nothing has ever suspended them (see
        # services.directory, which resolves the same way).
        "status": account.status if account else UserAccount.ACTIVE,
        "capabilities": sorted(roles.capabilities(role)),
    }

    profile = services.profile_of(user, role)
    if profile is None:
        return data

    for field in ROLE_PROFILE_FIELDS.get(role, ()):
        value = getattr(profile, field, None)
        # Dates are not JSON; everything else in these tuples already is.
        data[field] = value.isoformat() if hasattr(value, "isoformat") else value
    if roles.is_facility(role):
        data["verified"] = profile.verified
    return data


class ProfileView(APIView):
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def get(self, request):
        return Response(profile_payload(request.user), status=status.HTTP_200_OK)

    def put(self, request):
        serializer = ProfileUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user

        if "first_name" in data:
            user.first_name = data["first_name"]
        if "email" in data:
            user.email = data["email"]
        user.save()

        role = services.role_of(user)
        profile = services.profile_of(user, role)
        if profile is not None:
            changed = False
            for field in ROLE_PROFILE_FIELDS.get(role, ()):
                if field in data:
                    setattr(profile, field, data[field])
                    changed = True
            if changed:
                profile.save()

        return Response({"message": "Profile updated"}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Administration
# ---------------------------------------------------------------------------
class AdminUserList(APIView):
    """The account directory behind the admin portal's people/provider pages.

    Deliberately account-level only — username, role, status, facility name. No
    appointment or chat data appears here: administering the platform
    does not require reading anybody's medical record, so this endpoint cannot.
    """

    permission_classes = [IsPlatformAdmin]

    DEFAULT_LIMIT = 100
    MAX_LIMIT = 500

    def _limit(self, request):
        try:
            return max(1, min(int(request.query_params.get("limit",
                                                           self.DEFAULT_LIMIT)),
                              self.MAX_LIMIT))
        except (TypeError, ValueError):
            return self.DEFAULT_LIMIT

    @staticmethod
    def _row(entry):
        user, profile = entry["user"], entry["profile"]
        row = {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": entry["role"],
            "role_label": roles.label(entry["role"]),
            "status": entry["status"],
            "date_joined": user.date_joined.isoformat(),
            "name": getattr(profile, "name", None) or user.get_full_name()
                    or user.username,
        }
        if profile is not None:
            # Whatever this role calls its headline detail.
            row["detail"] = getattr(profile, "specialization",
                                    getattr(profile, "services", "")) or ""
            if hasattr(profile, "available"):
                row["available"] = profile.available
            if hasattr(profile, "verified"):
                row["verified"] = profile.verified
        return row

    def get(self, request):
        role = request.query_params.get("role")
        if role and role not in roles.ALL_ROLES:
            return api_error(f"Unknown role {role!r}.", status.HTTP_400_BAD_REQUEST)

        entries = services.directory(role=role)
        return Response({
            "count": min(len(entries), self._limit(request)),
            "total": len(entries),
            "results": [self._row(entry)
                        for entry in entries[:self._limit(request)]],
        }, status=status.HTTP_200_OK)
