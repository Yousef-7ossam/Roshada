"""Serializers for the scheduling and chat domains.

Registration, login and profile serializers live in ``accounts.serializers``
alongside the role model they validate against.
"""
import datetime

from django.contrib.auth.models import User
from django.utils import timezone
from rest_framework import serializers

from accounts import roles
from accounts.services import profile_of, role_of

from .models import (
    Appointment, AvailabilityRule, ChatMessage, Doctor, PatientProfile,
    Service, TimeOff,
)

GENDER_CHOICES = ["Male", "Female"]
RACE_CHOICES = ["African", "American", "Asian", "Caucasian", "Hispanic", "Other"]
SMOKING_CHOICES = ["Never", "Former", "Current"]


# ---------------------------------------------------------------------------
# Read (output) serializers — response shapes are part of the public contract.
# ---------------------------------------------------------------------------
class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['id', 'username', 'first_name', 'email']


class DoctorSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = Doctor
        fields = ['id', 'user', 'name', 'specialization', 'license_number',
                  'phone', 'clinic', 'available']


class PatientProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = PatientProfile
        fields = ['user', 'age', 'date_of_birth', 'gender', 'phone', 'address',
                  'medical_history']


def provider_brief(user):
    """How a provider is shown on an appointment, whatever kind they are.

    The display name lives on the role's own profile — ``Doctor.name``,
    ``LaboratoryProfile.name`` — so it is resolved through the role rather than
    read from a column that only doctors have.
    """
    role = role_of(user)
    profile = profile_of(user, role)
    return {
        "id": user.id,
        "role": role,
        "role_label": roles.label(role),
        "name": (getattr(profile, "name", None) or user.get_full_name()
                 or user.username),
        "detail": getattr(profile, "specialization",
                          getattr(profile, "services", "")) or "",
        "location": getattr(profile, "clinic",
                            getattr(profile, "address", "")) or "",
    }


class ServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Service
        fields = ['id', 'name', 'description', 'category', 'duration_minutes',
                  'preparation', 'is_active', 'created_at', 'updated_at']
        read_only_fields = ['created_at', 'updated_at']


class AvailabilityRuleSerializer(serializers.ModelSerializer):
    weekday_display = serializers.CharField(source='get_weekday_display',
                                            read_only=True)
    service_name = serializers.CharField(source='service.name', read_only=True,
                                         default=None)

    class Meta:
        model = AvailabilityRule
        fields = ['id', 'service', 'service_name', 'weekday', 'weekday_display',
                  'date', 'start_time', 'end_time', 'slot_minutes', 'is_active']

    def validate(self, attrs):
        """Mirror the database's check constraints as field errors.

        The constraints are the real guarantee; catching the same conditions
        here turns an IntegrityError into a message naming the field.
        """
        weekday = attrs.get('weekday', getattr(self.instance, 'weekday', None))
        date = attrs.get('date', getattr(self.instance, 'date', None))
        if (weekday is None) == (date is None):
            raise serializers.ValidationError(
                {"weekday": "Provide either a weekday (for a repeating rule) or "
                            "a date (for a one-off), but not both."})

        start = attrs.get('start_time', getattr(self.instance, 'start_time', None))
        end = attrs.get('end_time', getattr(self.instance, 'end_time', None))
        if start is not None and end is not None and end <= start:
            raise serializers.ValidationError(
                {"end_time": "The end time must be after the start time."})
        return attrs


class TimeOffSerializer(serializers.ModelSerializer):
    class Meta:
        model = TimeOff
        fields = ['id', 'date', 'start_time', 'end_time', 'reason', 'created_at']
        read_only_fields = ['created_at']

    def validate(self, attrs):
        start, end = attrs.get('start_time'), attrs.get('end_time')
        if (start is None) != (end is None):
            raise serializers.ValidationError(
                {"start_time": "Give both a start and an end time, or neither "
                               "for a whole day."})
        if start is not None and end <= start:
            raise serializers.ValidationError(
                {"end_time": "The end time must be after the start time."})
        return attrs


class AppointmentSerializer(serializers.ModelSerializer):
    """The appointment as every portal reads it.

    ``date``/``time`` are kept as top-level fields even though the model now
    stores an instant range: they are what a person means by "when is it", and
    dropping them would have broken every existing client for no gain. They are
    derived from ``start_at`` in the project timezone, so there is still one
    source of truth.
    """
    patient = UserSerializer(read_only=True)
    provider = serializers.SerializerMethodField()
    service = ServiceSerializer(read_only=True)
    date = serializers.DateField(read_only=True)
    time = serializers.TimeField(read_only=True)
    end_time = serializers.TimeField(read_only=True)
    duration_minutes = serializers.IntegerField(read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Appointment
        fields = ['id', 'provider', 'patient', 'service', 'date', 'time',
                  'end_time', 'start_at', 'end_at', 'duration_minutes',
                  'reason', 'status', 'status_display', 'cancellation_reason',
                  'created_at', 'updated_at']

    def get_provider(self, obj):
        return provider_brief(obj.provider)


class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ['id', 'role', 'text', 'created_at']


# ---------------------------------------------------------------------------
# Write (input) serializers — centralised validation for every mutating call.
# ---------------------------------------------------------------------------
class _FutureSlotMixin:
    """Shared rule: a slot must be in the future and within the booking window.

    Used by both booking and rescheduling so the two cannot drift apart.
    """
    # A booking further out than this is almost certainly a typo in the year.
    MAX_DAYS_AHEAD = 365

    def validate(self, attrs):
        date, time = attrs["date"], attrs["time"]
        now = timezone.localtime()
        requested = timezone.make_aware(
            datetime.datetime.combine(date, time), now.tzinfo
        )

        if requested <= now:
            raise serializers.ValidationError(
                {"date": "Appointments must be scheduled in the future."}
            )
        if (date - now.date()).days > self.MAX_DAYS_AHEAD:
            raise serializers.ValidationError(
                {"date": f"Appointments cannot be booked more than "
                         f"{self.MAX_DAYS_AHEAD} days in advance."}
            )
        return attrs


class AppointmentCreateSerializer(_FutureSlotMixin, serializers.Serializer):
    """A booking request.

    ``provider_id`` is a *user* id and works for all three provider kinds.
    ``doctor_id`` is the pre-unification identifier (a ``Doctor`` row's pk) and
    is still accepted so existing clients keep working; the service maps it.
    Exactly one must be given — accepting both would leave which one wins
    undefined.
    """
    provider_id = serializers.IntegerField(min_value=1, required=False)
    doctor_id = serializers.IntegerField(min_value=1, required=False)
    service_id = serializers.IntegerField(min_value=1, required=False,
                                          allow_null=True)
    date = serializers.DateField()
    time = serializers.TimeField()
    reason = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if not attrs.get("provider_id") and not attrs.get("doctor_id"):
            raise serializers.ValidationError(
                {"provider_id": "Choose a provider to book with."})
        if attrs.get("provider_id") and attrs.get("doctor_id"):
            raise serializers.ValidationError(
                {"provider_id": "Give either provider_id or doctor_id, not both."})
        return attrs


class AppointmentRescheduleSerializer(_FutureSlotMixin, serializers.Serializer):
    date = serializers.DateField()
    time = serializers.TimeField()


class AppointmentCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, required=False,
                                   allow_blank=True, default="")


class AppointmentOutcomeSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[Appointment.COMPLETED, Appointment.NO_SHOW])


class ChatExchangeSerializer(serializers.Serializer):
    """One question/answer pair to append to the caller's own history."""
    prompt = serializers.CharField(max_length=8000)
    reply = serializers.CharField(max_length=8000, allow_blank=True)


class ChatAskSerializer(serializers.Serializer):
    """A question for the AI assistant.

    Blank is rejected here rather than in the pipeline so an empty submission
    costs a 400 instead of a provider call.
    """
    message = serializers.CharField(max_length=8000, trim_whitespace=True)


class HeartInputSerializer(serializers.Serializer):
    """Validates heart/BP inputs while preserving the previous default fallbacks."""
    age = serializers.IntegerField(min_value=1, max_value=120, required=False, default=40)
    height = serializers.FloatField(min_value=50, max_value=250, required=False, default=170.0)
    weight = serializers.FloatField(min_value=10, max_value=400, required=False, default=70.0)
    ap_hi = serializers.FloatField(min_value=50, max_value=300, required=False, default=120.0)
    ap_lo = serializers.FloatField(min_value=30, max_value=200, required=False, default=80.0)
    cholesterol = serializers.ChoiceField(choices=[1, 2, 3], required=False, default=1)
    gluc = serializers.ChoiceField(choices=[1, 2, 3], required=False, default=1)
    smoke = serializers.ChoiceField(choices=[0, 1], required=False, default=0)
    alco = serializers.ChoiceField(choices=[0, 1], required=False, default=0)

    def validate(self, attrs):
        """Systolic must exceed diastolic.

        Each field passed its own range check independently, so a swapped pair
        (e.g. 80/180) reached the model and produced a negative pulse pressure —
        a silently meaningless clinical result rather than an error.
        """
        ap_hi, ap_lo = attrs.get("ap_hi"), attrs.get("ap_lo")
        if ap_hi is not None and ap_lo is not None and ap_hi <= ap_lo:
            raise serializers.ValidationError({
                "ap_hi": "Systolic blood pressure (upper) must be greater than "
                         "diastolic (lower). Check the two values are not swapped."
            })
        return attrs


class DiabetesInputSerializer(serializers.Serializer):
    """Validates diabetes inputs while preserving the previous default fallbacks."""
    age = serializers.FloatField(min_value=0, max_value=120, required=False, default=40.0)
    bmi = serializers.FloatField(min_value=0, max_value=100, required=False, default=25.0)
    hba1c_level = serializers.FloatField(min_value=0, max_value=20, required=False, default=5.5)
    blood_glucose_level = serializers.FloatField(min_value=0, max_value=500, required=False, default=100.0)
    hypertension = serializers.ChoiceField(choices=[0, 1], required=False, default=0)
    heart_disease = serializers.ChoiceField(choices=[0, 1], required=False, default=0)
    gender = serializers.ChoiceField(choices=GENDER_CHOICES, required=False, default="Female")
    race = serializers.ChoiceField(choices=RACE_CHOICES, required=False, default="Other")
    smoking_history = serializers.ChoiceField(choices=SMOKING_CHOICES, required=False, default="Never")
