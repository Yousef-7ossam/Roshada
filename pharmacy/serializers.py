"""Pharmacy input validation and output shapes.

Two responsibilities, kept apart the way the rest of the API keeps them: the
``*Serializer`` classes named for a write validate what a client may send, and
the read serializers decide what each role is allowed to be told.

The read side is where prescription scoping lives. A pharmacy and a patient
looking at the
same request do not see the same document — the pharmacy is shown the lines it
was asked to fill and a reference to the prescription behind them, never the
rest of what the doctor wrote.
"""
from rest_framework import serializers

from . import dosage_forms
from .models import (
    Medication, MedicationRequest, MedicationRequestItem, PharmacyInventory,
    Prescription, PrescriptionItem,
)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
class MedicationSerializer(serializers.ModelSerializer):
    label = serializers.CharField(read_only=True)
    form_label = serializers.CharField(read_only=True)

    class Meta:
        model = Medication
        fields = ["id", "name", "generic_name", "strength", "form",
                  "form_label", "label", "description", "code", "is_active"]


class MedicationCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=200)
    strength = serializers.CharField(max_length=60, required=False,
                                     allow_blank=True, default="")
    form = serializers.ChoiceField(choices=dosage_forms.ALL,
                                   default=dosage_forms.TABLET)
    generic_name = serializers.CharField(max_length=200, required=False,
                                         allow_blank=True, default="")
    description = serializers.CharField(required=False, allow_blank=True,
                                        default="")
    code = serializers.CharField(max_length=60, required=False,
                                 allow_blank=True, default="")


# ---------------------------------------------------------------------------
# Prescriptions
# ---------------------------------------------------------------------------
class PrescriptionItemSerializer(serializers.ModelSerializer):
    medication = MedicationSerializer(read_only=True)

    class Meta:
        model = PrescriptionItem
        fields = ["id", "medication", "dosage", "frequency", "duration",
                  "quantity", "instructions", "notes"]


class PrescriptionSerializer(serializers.ModelSerializer):
    items = PrescriptionItemSerializer(many=True, read_only=True)
    patient_name = serializers.SerializerMethodField()
    doctor_name = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display",
                                         read_only=True)

    class Meta:
        model = Prescription
        fields = ["id", "patient", "patient_name", "doctor", "doctor_name",
                  "diagnosis", "notes", "status", "status_label", "issued_at",
                  "cancellation_reason", "created_at", "updated_at", "items"]

    def get_patient_name(self, obj):
        return obj.patient.get_full_name() or obj.patient.username

    def get_doctor_name(self, obj):
        if obj.doctor is None:
            return None
        return obj.doctor.get_full_name() or obj.doctor.username


class PrescriptionItemInputSerializer(serializers.Serializer):
    medication_id = serializers.IntegerField()
    dosage = serializers.CharField(max_length=120, required=False,
                                   allow_blank=True, default="")
    frequency = serializers.CharField(max_length=120, required=False,
                                      allow_blank=True, default="")
    duration = serializers.CharField(max_length=120, required=False,
                                     allow_blank=True, default="")
    quantity = serializers.IntegerField(min_value=1, default=1)
    instructions = serializers.CharField(max_length=255, required=False,
                                         allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class PrescriptionCreateSerializer(serializers.Serializer):
    patient_id = serializers.IntegerField()
    items = PrescriptionItemInputSerializer(many=True, allow_empty=False)
    diagnosis = serializers.CharField(max_length=200, required=False,
                                      allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    #: Default true: the everyday action is writing a prescription and handing
    #: it to the patient. Saving a draft is the deliberate exception.
    issue = serializers.BooleanField(default=True)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
class InventorySerializer(serializers.ModelSerializer):
    """The pharmacy's own view of a stock line — the full picture.

    Raw stock and reservations appear here because this is the owner reading
    their own shelf. What a *patient* is shown is built by
    ``services.pharmacies_with`` and deliberately carries neither.
    """
    medication = MedicationSerializer(read_only=True)
    available_quantity = serializers.IntegerField(read_only=True)
    in_stock = serializers.BooleanField(read_only=True)
    is_low_stock = serializers.BooleanField(read_only=True)

    class Meta:
        model = PharmacyInventory
        fields = ["id", "medication", "quantity", "reserved",
                  "available_quantity", "in_stock", "is_low_stock", "price",
                  "is_active", "low_stock_threshold", "updated_at"]


class InventoryWriteSerializer(serializers.Serializer):
    """Add or restock one line. Every field optional except which product."""
    medication_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=0, required=False)
    price = serializers.DecimalField(max_digits=10, decimal_places=2,
                                     required=False, allow_null=True)
    is_active = serializers.BooleanField(required=False)
    low_stock_threshold = serializers.IntegerField(min_value=0, required=False)


class InventoryUpdateSerializer(serializers.Serializer):
    """Update an existing line. The product is in the URL, not the body —
    changing which medication a stock line refers to is not an edit."""
    quantity = serializers.IntegerField(min_value=0, required=False)
    price = serializers.DecimalField(max_digits=10, decimal_places=2,
                                     required=False, allow_null=True)
    is_active = serializers.BooleanField(required=False)
    low_stock_threshold = serializers.IntegerField(min_value=0, required=False)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
class MedicationRequestItemSerializer(serializers.ModelSerializer):
    medication = MedicationSerializer(read_only=True)
    line_total = serializers.DecimalField(max_digits=12, decimal_places=2,
                                          read_only=True)
    dosage = serializers.SerializerMethodField()
    frequency = serializers.SerializerMethodField()
    duration = serializers.SerializerMethodField()
    instructions = serializers.SerializerMethodField()

    class Meta:
        model = MedicationRequestItem
        fields = ["id", "medication", "quantity", "unit_price", "line_total",
                  "prescription_item", "dosage", "frequency", "duration",
                  "instructions"]

    # The dosing detail travels with the line rather than with the
    # prescription, so a pharmacy can label the pack correctly without being
    # handed the prescription it came from.
    def _from_item(self, obj, field):
        item = obj.prescription_item
        return getattr(item, field, "") if item is not None else ""

    def get_dosage(self, obj):
        return self._from_item(obj, "dosage")

    def get_frequency(self, obj):
        return self._from_item(obj, "frequency")

    def get_duration(self, obj):
        return self._from_item(obj, "duration")

    def get_instructions(self, obj):
        return self._from_item(obj, "instructions")


class MedicationRequestSerializer(serializers.ModelSerializer):
    """One request, as its patient reads it."""

    items = MedicationRequestItemSerializer(many=True, read_only=True)
    pharmacy_name = serializers.SerializerMethodField()
    patient_name = serializers.SerializerMethodField()
    status_label = serializers.CharField(source="get_status_display",
                                         read_only=True)
    total_price = serializers.DecimalField(max_digits=12, decimal_places=2,
                                           read_only=True)

    class Meta:
        model = MedicationRequest
        fields = ["id", "patient", "patient_name", "pharmacy", "pharmacy_name",
                  "prescription", "status", "status_label", "note",
                  "pharmacy_note", "cancellation_reason", "total_price",
                  "confirmed_at", "ready_at", "completed_at", "created_at",
                  "updated_at", "items"]

    def get_pharmacy_name(self, obj):
        profile = getattr(obj.pharmacy, "pharmacy_profile", None)
        if profile is not None and profile.name:
            return profile.name
        return obj.pharmacy.get_full_name() or obj.pharmacy.username

    def get_patient_name(self, obj):
        return obj.patient.get_full_name() or obj.patient.username


class PharmacyRequestSerializer(MedicationRequestSerializer):
    """The same request, as the *pharmacy* filling it is allowed to read it.

    Minimum necessary, in code. The pharmacy needs the patient's name, the lines
    it was asked to fill and enough of a reference to check the prescription is
    real —
    which is a number and a date, not its contents. It gets no diagnosis, no
    prescriber's notes, and none of the prescription's other medications.
    """

    prescription_reference = serializers.SerializerMethodField()

    class Meta(MedicationRequestSerializer.Meta):
        fields = MedicationRequestSerializer.Meta.fields + [
            "prescription_reference"]

    def get_prescription_reference(self, obj):
        prescription = obj.prescription
        if prescription is None:
            # An over-the-counter request. Saying so is the honest answer;
            # inventing a prescription reference would not be.
            return None
        doctor = prescription.doctor
        return {
            "id": prescription.id,
            "issued_at": (prescription.issued_at.isoformat()
                          if prescription.issued_at else None),
            "prescribed_by": (doctor.get_full_name() or doctor.username
                              if doctor else None),
            "status": prescription.status,
        }


class RequestItemInputSerializer(serializers.Serializer):
    """One line of a new request.

    Either a prescription line (which carries its own medication and quantity)
    or a bare medication for an over-the-counter request. Validated together
    rather than field by field, so "neither" is rejected here instead of
    surfacing as a confusing error deeper in.
    """
    prescription_item_id = serializers.IntegerField(required=False,
                                                    allow_null=True)
    medication_id = serializers.IntegerField(required=False, allow_null=True)
    quantity = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        if not attrs.get("prescription_item_id") and not attrs.get("medication_id"):
            raise serializers.ValidationError(
                "Give either a prescription line or a medication.")
        return attrs


class RequestCreateSerializer(serializers.Serializer):
    pharmacy_id = serializers.IntegerField()
    items = RequestItemInputSerializer(many=True, allow_empty=False)
    prescription_id = serializers.IntegerField(required=False, allow_null=True)
    note = serializers.CharField(max_length=255, required=False,
                                 allow_blank=True, default="")


class StatusChangeSerializer(serializers.Serializer):
    """Ask for a status. Whether it is *permitted* is the service layer's
    answer — the choices here only reject values that are not statuses at all."""
    status = serializers.ChoiceField(
        choices=[choice[0] for choice in MedicationRequest.STATUS_CHOICES])
    reason = serializers.CharField(max_length=255, required=False,
                                   allow_blank=True, default="")
    note = serializers.CharField(max_length=255, required=False,
                                 allow_blank=True, default="")


class PrescriptionStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=[choice[0] for choice in Prescription.STATUS_CHOICES])
    reason = serializers.CharField(max_length=255, required=False,
                                   allow_blank=True, default="")
