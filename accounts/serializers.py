"""Validation for the authentication surface.

Every registration form shares the same credential rules, so they are declared
once on :class:`_SignupSerializer` and the subclasses add only the fields their
role actually has. That is what keeps "one authentication system, six roles"
true at the validation layer rather than only in the diagram.
"""
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from . import roles
from .models import (
    LaboratoryProfile, PharmacyProfile, RadiologyProfile, UserAccount,
)

# Shared field-level rules (moved here with the auth surface; the value and the
# message are unchanged, so existing clients see the same validation errors).
USERNAME_REGEX = r'^[A-Za-z0-9._-]{3,30}$'
USERNAME_HELP = "3-30 characters; letters, digits, dots, underscores or hyphens only."


class _PasswordMixin(serializers.Serializer):
    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as e:
            raise serializers.ValidationError(list(e.messages))
        return value


class _SignupSerializer(_PasswordMixin):
    """Credentials + display name: the part of registration that never varies."""

    username = serializers.RegexField(USERNAME_REGEX, help_text=USERNAME_HELP)
    password = serializers.CharField(write_only=True,
                                     style={'input_type': 'password'})
    name = serializers.CharField(max_length=100)

    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError("Username already exists.")
        return value


class SignupPatientSerializer(_SignupSerializer):
    age = serializers.IntegerField(min_value=0, max_value=120, required=False, default=0)
    gender = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    address = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class SignupDoctorSerializer(_SignupSerializer):
    specialization = serializers.CharField(max_length=200)
    license_number = serializers.CharField(max_length=60, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    clinic = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")


class _SignupFacilitySerializer(_SignupSerializer):
    """Laboratories, radiology centres and pharmacies register identically.

    They differ in what ``services`` means, not in its shape, so one serializer
    covers all three rather than three copies that would drift.
    """
    license_number = serializers.CharField(max_length=60, required=False, allow_blank=True, default="")
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    address = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    services = serializers.CharField(max_length=2000, required=False, allow_blank=True, default="")
    operating_hours = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")


class SignupLaboratorySerializer(_SignupFacilitySerializer):
    services = serializers.CharField(
        max_length=2000, required=False, allow_blank=True, default="",
        help_text="Tests offered, e.g. 'CBC, Lipid profile, HbA1c'.")


class SignupRadiologySerializer(_SignupFacilitySerializer):
    services = serializers.CharField(
        max_length=2000, required=False, allow_blank=True, default="",
        help_text="Imaging offered, e.g. 'X-ray, CT, MRI, Ultrasound'.")


class SignupPharmacySerializer(_SignupFacilitySerializer):
    pass


#: role -> the serializer that validates its registration form.
SIGNUP_SERIALIZERS = {
    roles.PATIENT: SignupPatientSerializer,
    roles.DOCTOR: SignupDoctorSerializer,
    roles.LABORATORY: SignupLaboratorySerializer,
    roles.RADIOLOGY: SignupRadiologySerializer,
    roles.PHARMACY: SignupPharmacySerializer,
}


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True,
                                     style={'input_type': 'password'})


class ProfileUpdateSerializer(serializers.Serializer):
    """One update form for every role.

    Fields are optional and applied only where the caller's own profile has
    them, so a pharmacy sending ``specialization`` changes nothing rather than
    erroring — and, critically, cannot reach another role's columns.
    """
    # Account-level
    first_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)

    # Patient
    age = serializers.IntegerField(min_value=0, max_value=120, required=False)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    gender = serializers.CharField(max_length=20, required=False, allow_blank=True)
    address = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    medical_history = serializers.CharField(max_length=5000, required=False, allow_blank=True)

    # Shared by doctor + facilities
    phone = serializers.CharField(max_length=30, required=False, allow_blank=True)
    license_number = serializers.CharField(max_length=60, required=False, allow_blank=True)

    # Doctor
    specialization = serializers.CharField(max_length=200, required=False, allow_blank=True)
    clinic = serializers.CharField(max_length=200, required=False, allow_blank=True)

    # Facility
    name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    services = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    operating_hours = serializers.CharField(max_length=200, required=False, allow_blank=True)
    available = serializers.BooleanField(required=False)


# ---------------------------------------------------------------------------
# Read serializers
# ---------------------------------------------------------------------------
class UserAccountSerializer(serializers.ModelSerializer):
    role_label = serializers.SerializerMethodField()

    class Meta:
        model = UserAccount
        fields = ['role', 'role_label', 'status', 'created_at']

    def get_role_label(self, obj):
        return roles.label(obj.role)


class FacilityProfileSerializer(serializers.ModelSerializer):
    """Shared output shape for the three facility profiles.

    ``verified`` is read-only: a facility marking *itself* verified would make
    the flag meaningless. It is set by an administrator.
    """

    class Meta:
        model = LaboratoryProfile      # replaced per-subclass below
        fields = ['id', 'name', 'license_number', 'phone', 'email', 'address',
                  'services', 'operating_hours', 'available', 'verified']
        read_only_fields = ['verified']


def facility_serializer_for(model):
    """A :class:`FacilityProfileSerializer` bound to one concrete model."""
    meta = type("Meta", (FacilityProfileSerializer.Meta,), {"model": model})
    return type(f"{model.__name__}Serializer", (FacilityProfileSerializer,),
                {"Meta": meta})


LaboratoryProfileSerializer = facility_serializer_for(LaboratoryProfile)
RadiologyProfileSerializer = facility_serializer_for(RadiologyProfile)
PharmacyProfileSerializer = facility_serializer_for(PharmacyProfile)

FACILITY_SERIALIZERS = {
    roles.LABORATORY: LaboratoryProfileSerializer,
    roles.RADIOLOGY: RadiologyProfileSerializer,
    roles.PHARMACY: PharmacyProfileSerializer,
}
