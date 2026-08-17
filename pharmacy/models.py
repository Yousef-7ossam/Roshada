"""The Pharmacy domain: medications, prescriptions, inventory and requests.

**What this module deliberately does not contain.** There is no pharmacy model —
a pharmacy is a user with the ``pharmacy`` role and an
``accounts.PharmacyProfile``, exactly as a radiology centre is. There is no
second user, patient or doctor model. There is no appointment and no slot:
dispensing is not a booked period, which is why ``pharmacy`` is absent from
``roles.BOOKABLE_ROLES`` and why this module never touches the scheduling
engine.

What is genuinely new is the medication supply chain, which nothing in the
platform modelled before::

    Prescription  ->  PrescriptionItem  ->  Medication
                                              |
                                              v
                      PharmacyInventory  <----+
                              ^
                              |
    MedicationRequest  ->  MedicationRequestItem

``Medication`` is the join. It is a normalized product identity rather than a
string on each side, because matching "what the doctor wrote" against "what a
pharmacy stocks" by free text is matching ``"Amoxicillin 500mg"`` against
``"amoxicillin 500 mg"`` and finding nothing.

**Stock is two numbers, not one.** ``quantity`` is what is on the shelf;
``reserved`` is the part of it already committed to confirmed requests. What a
patient can be offered is the difference. Both are guarded by database check
constraints, so overselling is refused by PostgreSQL and not merely by the
service layer that is supposed to call it correctly.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import models
from django.db.models.functions import Lower

from . import dosage_forms


class Medication(models.Model):
    """A medication product: what is prescribed, and what is stocked.

    One row is one identity — "Amoxicillin 500 mg capsule" — shared by every
    prescription that names it and every pharmacy that stocks it. That shared
    identity is the whole point: it is what makes "find this in pharmacies" a
    foreign-key lookup instead of a text search that silently misses matches.

    The uniqueness index is case-insensitive on all three identifying columns.
    Without that, the first doctor to type "amoxicillin" would create a second
    product that no pharmacy stocking "Amoxicillin" appears to carry.
    """

    #: The name a prescriber writes: a brand name, or the generic if unbranded.
    name = models.CharField(max_length=200)
    #: The active ingredient. Blank when the name already is the generic.
    generic_name = models.CharField(max_length=200, blank=True, default="")
    #: As printed on the pack, e.g. "500 mg". Free text on purpose: strengths
    #: are written in units this product has no business normalising ("5 mg/mL",
    #: "2.5%"), and parsing them into a number would lose information.
    strength = models.CharField(max_length=60, blank=True, default="")
    form = models.CharField(max_length=20, choices=dosage_forms.CHOICES,
                            default=dosage_forms.TABLET)
    description = models.TextField(blank=True, default="")
    #: An external catalogue code (ATC, national registration number) when one
    #: is known. Declared so a future formulary import has somewhere to put it;
    #: nothing in this phase reads it.
    code = models.CharField(max_length=60, blank=True, default="")
    #: Withdrawn products are deactivated, never deleted — prescriptions and
    #: dispensing history must keep saying what was actually prescribed.
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "strength"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"), Lower("strength"), Lower("form"),
                name="unique_medication_identity"),
        ]
        indexes = [
            models.Index(fields=["name"]),
            models.Index(fields=["generic_name"]),
            models.Index(fields=["is_active"]),
        ]

    def __str__(self):
        return self.label

    @property
    def label(self):
        """"Amoxicillin 500 mg" — how the product is named everywhere in the UI."""
        return f"{self.name} {self.strength}".strip()

    @property
    def form_label(self):
        return dosage_forms.label(self.form)


class Prescription(models.Model):
    """A doctor's medication order for a patient.

    Structured, not a text blob: the items are rows, and each row points at a
    ``Medication``. That is what lets the patient press "Find in pharmacies" on
    one line of the prescription and get a real answer.

    A prescription is invisible to the patient until it is issued. The gate is
    the same shape as radiology's report release — a clinician's unfinished
    working copy is not a clinical document, and the rule is enforced in the
    queryset rather than in a template.
    """

    DRAFT = "draft"
    ISSUED = "issued"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (ISSUED, "Issued"),
        (CANCELLED, "Cancelled"),
    ]
    #: The only statuses anybody other than the prescribing doctor may read.
    VISIBLE_STATUSES = (ISSUED, CANCELLED)

    TRANSITIONS = {
        DRAFT: (ISSUED, CANCELLED),
        ISSUED: (CANCELLED,),
        CANCELLED: (),
    }

    patient = models.ForeignKey(User, on_delete=models.CASCADE,
                                related_name="prescriptions")
    #: Kept on delete so the record survives a departing clinician; the
    #: prescription is still the patient's.
    doctor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                               blank=True, related_name="written_prescriptions")
    #: Why it was written. The clinical context a pharmacist may need.
    diagnosis = models.CharField(max_length=200, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default=DRAFT)
    issued_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["patient", "-created_at"]),
            models.Index(fields=["doctor", "-created_at"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Prescription {self.pk} for {self.patient.username} [{self.status}]"

    @property
    def is_issued(self):
        return self.status == self.ISSUED

    def can_transition_to(self, status):
        return status in self.TRANSITIONS.get(self.status, ())


class PrescriptionItem(models.Model):
    """One prescribed medication, with how it is to be taken.

    The dosing fields are free text, deliberately. Roshada records what the
    doctor prescribed; turning "3 times/day for 7 days" into a structured
    schedule would mean inventing a clinical model — and any part of it the
    system got wrong would be a dosing error with the platform's name on it.
    """

    prescription = models.ForeignKey(Prescription, on_delete=models.CASCADE,
                                     related_name="items")
    #: PROTECT: a medication that has been prescribed cannot be deleted out from
    #: under the prescription that names it.
    medication = models.ForeignKey(Medication, on_delete=models.PROTECT,
                                   related_name="prescribed_as")
    dosage = models.CharField(max_length=120, blank=True, default="")
    frequency = models.CharField(max_length=120, blank=True, default="")
    duration = models.CharField(max_length=120, blank=True, default="")
    #: Units to dispense. The one dosing field that is a number, because it is
    #: the one the pharmacy counts out and the one stock is checked against.
    quantity = models.PositiveIntegerField(default=1)
    instructions = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["prescription", "medication"],
                                    name="unique_medication_per_prescription"),
            models.CheckConstraint(condition=models.Q(quantity__gte=1),
                                   name="prescription_item_quantity_positive"),
        ]
        indexes = [
            models.Index(fields=["medication"]),
        ]

    def __str__(self):
        return f"{self.medication.label} x{self.quantity}"


class PharmacyInventory(models.Model):
    """What one pharmacy holds of one medication, and at what price.

    ``quantity`` is stock on hand. ``reserved`` is the part of it already
    promised to requests the pharmacy has confirmed but not yet handed over.
    Everything a patient is offered is computed from the difference, so a
    confirmed request cannot be promised twice.

    The check constraints are the actual guarantee. A service layer that forgot
    to re-read the row under a lock would produce a negative balance; the
    database refuses it instead, which is what makes "cannot be oversold" a
    property of the data rather than of the code path that happened to run.
    """

    #: The pharmacy's *user*, not a profile row — identity lives on the user
    #: everywhere in Roshada, and a second provider registry could disagree.
    pharmacy = models.ForeignKey(User, on_delete=models.CASCADE,
                                 related_name="pharmacy_inventory")
    medication = models.ForeignKey(Medication, on_delete=models.PROTECT,
                                   related_name="stocked_by")
    quantity = models.PositiveIntegerField(default=0)
    reserved = models.PositiveIntegerField(default=0)
    #: Each pharmacy sets its own. Null means "not priced" and is shown as
    #: such — it is not the same claim as a price of zero.
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True,
                                blank=True)
    #: The pharmacy's own switch. A deactivated line keeps its history and its
    #: stock figure but is not offered to patients.
    is_active = models.BooleanField(default=True)
    #: At or below this, the line is reported as low stock on the dashboard.
    #: Zero means the pharmacy has not set a threshold.
    low_stock_threshold = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["medication__name"]
        verbose_name_plural = "pharmacy inventory"
        constraints = [
            # One line per product per pharmacy. Not a partial index on
            # is_active: deactivating is a flag, not a delete, so a "duplicate"
            # would be a second row that still has to be reconciled by hand.
            models.UniqueConstraint(fields=["pharmacy", "medication"],
                                    name="unique_medication_per_pharmacy"),
            models.CheckConstraint(
                condition=models.Q(reserved__lte=models.F("quantity")),
                name="inventory_reserved_within_stock"),
        ]
        indexes = [
            models.Index(fields=["medication", "is_active"]),
            models.Index(fields=["pharmacy", "is_active"]),
        ]

    def __str__(self):
        return f"{self.medication.label} @ {self.pharmacy.username}: {self.quantity}"

    # -- Derived, never stored twice ----------------------------------------
    @property
    def available_quantity(self):
        """What can still be promised. Never the raw stock figure."""
        return max(self.quantity - self.reserved, 0)

    @property
    def in_stock(self):
        return self.is_active and self.available_quantity > 0

    @property
    def is_low_stock(self):
        return (self.is_active and self.low_stock_threshold > 0
                and self.available_quantity <= self.low_stock_threshold)

    def can_supply(self, units):
        return self.is_active and self.available_quantity >= units


class MedicationRequest(models.Model):
    """A patient asking one pharmacy to put medication aside for them.

    Pickup only. There is no delivery model and no payment model, because the
    platform has neither — a status of ``ready`` means "waiting at the counter",
    which is a claim the system can actually stand behind.

    ``prescription`` is nullable: a patient may request an over-the-counter
    medication they found by search. Recording that as if a doctor had
    prescribed it would be inventing a prescription, the same way a self-booked
    imaging study is recorded with no doctor rather than with a made-up one.
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    PREPARING = "preparing"
    READY = "ready"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    STATUS_CHOICES = [
        (PENDING, "Pending"),
        (CONFIRMED, "Confirmed"),
        (PREPARING, "Preparing"),
        (READY, "Ready for pickup"),
        (COMPLETED, "Completed"),
        (CANCELLED, "Cancelled"),
        (REJECTED, "Rejected"),
    ]

    #: States in which the request is still live, and so still holds stock or
    #: still blocks a second request for the same prescription line.
    OPEN_STATUSES = (PENDING, CONFIRMED, PREPARING, READY)
    #: States from which nothing further can happen.
    TERMINAL_STATUSES = (COMPLETED, CANCELLED, REJECTED)

    #: The only transitions the workflow permits. Enforced in the service layer,
    #: so no request body can set a status directly.
    TRANSITIONS = {
        PENDING: (CONFIRMED, REJECTED, CANCELLED),
        CONFIRMED: (PREPARING, READY, CANCELLED),
        PREPARING: (READY, CANCELLED),
        READY: (COMPLETED, CANCELLED),
        COMPLETED: (),
        CANCELLED: (),
        REJECTED: (),
    }

    #: Who may move the request into each status. The patient may withdraw;
    #: everything else is the pharmacy's to decide.
    PATIENT_STATUSES = (CANCELLED,)
    PHARMACY_STATUSES = (CONFIRMED, PREPARING, READY, COMPLETED, REJECTED,
                         CANCELLED)

    patient = models.ForeignKey(User, on_delete=models.CASCADE,
                                related_name="medication_requests")
    #: The pharmacy's user. The request belongs to exactly one pharmacy: a
    #: prescription whose medications are split across two pharmacies becomes
    #: two requests, which is why this is a plain FK and not a many-to-many.
    pharmacy = models.ForeignKey(User, on_delete=models.CASCADE,
                                 related_name="incoming_medication_requests")
    prescription = models.ForeignKey(Prescription, on_delete=models.SET_NULL,
                                     null=True, blank=True,
                                     related_name="requests")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default=PENDING)
    #: True while inventory is held for this request. Stored rather than
    #: derived from the status so releasing stock is idempotent: a second
    #: cancellation cannot give the units back twice.
    stock_reserved = models.BooleanField(default=False)
    note = models.CharField(max_length=255, blank=True, default="")
    pharmacy_note = models.CharField(max_length=255, blank=True, default="")
    cancellation_reason = models.CharField(max_length=255, blank=True,
                                           default="")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["patient", "-created_at"]),
            models.Index(fields=["pharmacy", "-created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["prescription"]),
        ]

    def __str__(self):
        return f"Request {self.pk} to {self.pharmacy.username} [{self.status}]"

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    def can_transition_to(self, status):
        return status in self.TRANSITIONS.get(self.status, ())

    @property
    def total_price(self):
        """Sum of the priced lines, or None when nothing on it is priced.

        None rather than zero: "we did not record a price" and "this costs
        nothing" are different claims, and only one of them is true.
        """
        priced = [item.line_total for item in self.items.all()
                  if item.line_total is not None]
        return sum(priced, Decimal("0.00")) if priced else None


class MedicationRequestItem(models.Model):
    """One medication on a request, with the price agreed at request time.

    ``unit_price`` is a snapshot, not a live lookup. A pharmacy that reprices
    its shelf must not silently reprice a request a patient has already placed.
    """

    request = models.ForeignKey(MedicationRequest, on_delete=models.CASCADE,
                                related_name="items")
    #: The prescription line this fulfils, when there is one. SET_NULL rather
    #: than CASCADE: the request still happened even if the prescription is
    #: later withdrawn.
    prescription_item = models.ForeignKey(PrescriptionItem,
                                          on_delete=models.SET_NULL, null=True,
                                          blank=True,
                                          related_name="requested_as")
    medication = models.ForeignKey(Medication, on_delete=models.PROTECT,
                                   related_name="requested_as")
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, null=True,
                                     blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["request", "medication"],
                                    name="unique_medication_per_request"),
            models.CheckConstraint(condition=models.Q(quantity__gte=1),
                                   name="request_item_quantity_positive"),
        ]
        indexes = [
            models.Index(fields=["medication"]),
        ]

    def __str__(self):
        return f"{self.medication.label} x{self.quantity}"

    @property
    def line_total(self):
        if self.unit_price is None:
            return None
        return self.unit_price * self.quantity
