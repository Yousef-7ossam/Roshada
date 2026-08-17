"""Pharmacy use-cases.

Every state change and every stock movement in the module lives here. Views
validate input and translate exceptions; no view assigns a status field, writes
a quantity or filters a queryset by hand. That is what makes "a request body
cannot set a status" and "stock only moves through reservation" structural facts
rather than conventions.

**The stock rules, in one place, because they are the part that must be right:**

* Submitting a request reserves nothing. A patient asking is not a promise.
* Confirming reserves, under ``SELECT ... FOR UPDATE`` on the inventory rows.
  Two pharmacists confirming at the same instant serialise; the second sees the
  first's reservation and is refused.
* Completing converts the reservation into a dispense: stock down, reservation
  down.
* Cancelling or rejecting releases whatever was held, exactly once —
  ``stock_reserved`` is what makes that idempotent.

The database backs all of it with check constraints, so a bug here surfaces as
an ``IntegrityError`` rather than as a negative shelf.
"""
import logging
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from accounts import roles
from accounts.services import role_of
from appointments.services import care
# The notification vocabulary only — a pure-Python module with no Django
# imports, so naming a type at module level is safe. The service itself is
# imported lazily where it is used, since it touches models.
from comms import types as notification_types

from . import dosage_forms
from .models import (
    Medication, MedicationRequest, MedicationRequestItem, PharmacyInventory,
    Prescription, PrescriptionItem,
)

logger = logging.getLogger("appointments")


class NotAuthorized(Exception):
    """The caller has no relationship to this record."""


class NotFound(Exception):
    """No such record — also raised instead of NotAuthorized where telling the
    two apart would confirm that an id exists."""


class InvalidTransition(Exception):
    """The requested status change is not permitted from the current state."""


class InvalidRequest(Exception):
    """The request cannot be built as asked (no items, unknown medication…)."""


class InsufficientStock(Exception):
    """The pharmacy cannot supply what was asked for.

    Carries the shortfall so the caller can say *which* line failed rather than
    only that something did.
    """

    def __init__(self, message, medication=None, available=None, requested=None):
        super().__init__(message)
        self.medication = medication
        self.available = available
        self.requested = requested


class DuplicateRequest(Exception):
    """This prescription line already has a live request somewhere."""


# ---------------------------------------------------------------------------
# Reading — every queryset is scoped, never filtered in the view
# ---------------------------------------------------------------------------
_PRESCRIPTION_RELATED = ("patient", "doctor")
_PRESCRIPTION_PREFETCH = ("items", "items__medication")

_INVENTORY_RELATED = ("medication", "pharmacy", "pharmacy__pharmacy_profile")

#: Wide on purpose: serialising a request reaches the pharmacy's display name
#: (on its profile), the patient, every line's medication, and the prescription
#: behind it. Doing those joins once here is the difference between 5 queries
#: and one per row.
_REQUEST_RELATED = ("patient", "pharmacy", "pharmacy__pharmacy_profile",
                    "prescription", "prescription__doctor")
_REQUEST_PREFETCH = ("items", "items__medication", "items__prescription_item")


def prescriptions_for(user):
    """The prescriptions this user may see, by role.

    **A pharmacy is not in this list, and that is the point.** a pharmacy receives only what it needs to fill the request in front
    of it. Giving it a prescription queryset would hand it every line the doctor
    wrote, including the ones the patient chose to fill elsewhere. What a
    pharmacy sees is reached through its own requests instead — see
    ``requests_for``.
    """
    role = role_of(user)
    queryset = (Prescription.objects
                .select_related(*_PRESCRIPTION_RELATED)
                .prefetch_related(*_PRESCRIPTION_PREFETCH))
    if role == roles.PATIENT:
        # Drafts are the doctor's working copy, not a clinical document.
        return queryset.filter(patient=user,
                               status__in=Prescription.VISIBLE_STATUSES)
    if role == roles.DOCTOR:
        # The ones this doctor wrote — not every prescription their patients
        # hold, which would let one clinician read another's prescribing.
        return queryset.filter(doctor=user)
    if role == roles.ADMIN:
        return queryset.none()   # administration does not include reading these
    return Prescription.objects.none()


def patient_visible_prescriptions(patient):
    """Prescriptions this patient may read: issued, never a doctor's draft.

    The same gate ``prescriptions_for`` applies to a patient, expressed once
    here so the medical record can show a treating doctor exactly the layer the
    patient sees — and so no caller can widen it to include drafts.
    """
    return (Prescription.objects
            .filter(patient=patient,
                    status__in=Prescription.VISIBLE_STATUSES)
            .select_related(*_PRESCRIPTION_RELATED)
            .prefetch_related(*_PRESCRIPTION_PREFETCH))


def patient_medication_requests(patient):
    """This patient's own dispensing activity.

    Used only for the patient's own record. Dispensing is deliberately not part
    of what a treating doctor is shown — the pharmacy module does not route it
    back to the prescriber, and the medical record does not create a second
    route around that.
    """
    return (MedicationRequest.objects.filter(patient=patient)
            .select_related(*_REQUEST_RELATED)
            .prefetch_related(*_REQUEST_PREFETCH))


def get_prescription(user, prescription_id):
    prescription = prescriptions_for(user).filter(pk=prescription_id).first()
    if prescription is None:
        raise NotFound()
    return prescription


def inventory_for(pharmacy_user):
    """One pharmacy's own stock list. Never another's."""
    if role_of(pharmacy_user) != roles.PHARMACY:
        return PharmacyInventory.objects.none()
    return (PharmacyInventory.objects
            .select_related(*_INVENTORY_RELATED)
            .filter(pharmacy=pharmacy_user))


def requests_for(user):
    """Medication requests the user may see.

    The patient sees their own; the pharmacy sees the ones addressed to it.
    A doctor sees none: whether a patient filled a prescription is dispensing
    information, and the brief does not put it in the prescriber's hands.
    """
    role = role_of(user)
    queryset = (MedicationRequest.objects
                .select_related(*_REQUEST_RELATED)
                .prefetch_related(*_REQUEST_PREFETCH))
    if role == roles.PATIENT:
        return queryset.filter(patient=user)
    if role == roles.PHARMACY:
        return queryset.filter(pharmacy=user)
    return MedicationRequest.objects.none()


def get_request(user, request_id):
    found = requests_for(user).filter(pk=request_id).first()
    if found is None:
        raise NotFound()
    return found


# ---------------------------------------------------------------------------
# The medication catalogue
# ---------------------------------------------------------------------------
def search_medications(term="", limit=50, active_only=True):
    """Find products by name, generic name or code.

    Shared by the doctor writing a prescription and the pharmacy stocking a
    shelf, so both end up pointing at the same row rather than at two spellings
    of the same drug.
    """
    queryset = Medication.objects.all()
    if active_only:
        queryset = queryset.filter(is_active=True)
    term = (term or "").strip()
    if term:
        queryset = queryset.filter(Q(name__icontains=term)
                                   | Q(generic_name__icontains=term)
                                   | Q(code__iexact=term))
    return queryset[:limit]


def get_medication(medication_id):
    try:
        return Medication.objects.get(pk=int(medication_id))
    except (Medication.DoesNotExist, ValueError, TypeError):
        raise NotFound()


def find_or_create_medication(user, name, strength="", form=dosage_forms.TABLET,
                              generic_name="", description="", code=""):
    """Get the product with this identity, creating it only if it is new.

    Clinicians and pharmacies both need to add products the catalogue has not
    seen. Matching case-insensitively before creating is what stops that from
    fragmenting the catalogue — and the unique index means a race that gets past
    the check still cannot produce a duplicate.
    """
    if role_of(user) not in (roles.DOCTOR, roles.PHARMACY, roles.ADMIN):
        raise NotAuthorized("Your account cannot add medications to the catalog.")

    name = (name or "").strip()
    if not name:
        raise InvalidRequest("A medication needs a name.")
    if not dosage_forms.is_valid(form):
        raise InvalidRequest(f"{form!r} is not a known dosage form.")
    strength = (strength or "").strip()

    existing = Medication.objects.filter(name__iexact=name,
                                         strength__iexact=strength,
                                         form__iexact=form).first()
    if existing is not None:
        return existing, False

    medication = Medication.objects.create(
        name=name, strength=strength, form=form,
        generic_name=(generic_name or "").strip(),
        description=description or "", code=(code or "").strip())
    logger.info("Medication %s (%s) added by %s", medication.id,
                medication.label, user.username)
    return medication, True


# ---------------------------------------------------------------------------
# Doctor: prescribing
# ---------------------------------------------------------------------------
def create_prescription(doctor, patient_id, items, diagnosis="", notes="",
                        issue=True):
    """Write a prescription for a patient this doctor actually treats.

    The care-relationship rule is the platform's existing one, reused rather
    than restated — the same call radiology makes before letting a doctor order
    imaging — so "my patient" cannot come to mean something looser here than it
    does elsewhere in the product.

    Items are created in the same transaction as the prescription: a
    prescription with no medications on it is not a half-saved record, it is a
    record that should never have existed.
    """
    if role_of(doctor) != roles.DOCTOR:
        raise NotAuthorized("Only doctors can write prescriptions.")
    if not items:
        raise InvalidRequest("A prescription needs at least one medication.")

    try:
        patient = User.objects.get(pk=int(patient_id))
    except (User.DoesNotExist, ValueError, TypeError):
        raise NotFound()
    if role_of(patient) != roles.PATIENT:
        raise NotFound()
    if not care.treats_patient(doctor, patient):
        raise NotAuthorized(
            "You can only prescribe for patients you have appointments with.")

    with transaction.atomic():
        prescription = Prescription.objects.create(
            patient=patient, doctor=doctor, diagnosis=diagnosis, notes=notes,
            status=Prescription.ISSUED if issue else Prescription.DRAFT,
            issued_at=timezone.now() if issue else None)

        seen = set()
        for entry in items:
            medication = get_medication(entry.get("medication_id"))
            if medication.id in seen:
                raise InvalidRequest(
                    f"{medication.label} is listed twice on this prescription.")
            seen.add(medication.id)
            PrescriptionItem.objects.create(
                prescription=prescription, medication=medication,
                dosage=entry.get("dosage", "") or "",
                frequency=entry.get("frequency", "") or "",
                duration=entry.get("duration", "") or "",
                quantity=max(int(entry.get("quantity") or 1), 1),
                instructions=entry.get("instructions", "") or "",
                notes=entry.get("notes", "") or "")

    if prescription.status == Prescription.ISSUED:
        _announce_prescription(prescription)

    logger.info("Prescription %s (%s items) written by %s for %s",
                prescription.id, len(items), doctor.username, patient.username)
    return prescription


def _announce_prescription(prescription):
    """Tell the patient a prescription is waiting for them.

    Only on *issue*, never on save. Notifying about a draft would tell a
    patient that a prescription exists which the queryset then refuses to show
    them — and would leak that their doctor is drafting something.

    The body names no medication: it says a prescription was issued and links
    to it. What is on it stays behind the prescription endpoint.
    """
    from comms import notifications

    notifications.notify(
        prescription.patient, notification_types.PRESCRIPTION_CREATED,
        "New prescription",
        f"{notifications.display_name(prescription.doctor)} issued a new "
        f"prescription for you.",
        source="pharmacy.Prescription", reference=prescription.id)


def transition_prescription(user, prescription_id, to_status, reason=""):
    """Issue or cancel a prescription. The prescribing doctor only."""
    if role_of(user) != roles.DOCTOR:
        raise NotAuthorized("Only the prescribing doctor can change a prescription.")
    prescription = prescriptions_for(user).filter(pk=prescription_id).first()
    if prescription is None:
        raise NotFound()
    if not prescription.can_transition_to(to_status):
        raise InvalidTransition(
            f"A {prescription.get_status_display().lower()} prescription cannot "
            f"become {to_status}.")

    prescription.status = to_status
    fields = ["status", "updated_at"]
    if to_status == Prescription.ISSUED:
        prescription.issued_at = timezone.now()
        fields.append("issued_at")
    if to_status == Prescription.CANCELLED:
        prescription.cancellation_reason = reason or ""
        fields.append("cancellation_reason")
    prescription.save(update_fields=fields)

    if to_status == Prescription.ISSUED:
        # A draft becoming real is the same event as one written already
        # issued, so it raises the same notification.
        _announce_prescription(prescription)
    elif to_status == Prescription.CANCELLED:
        from comms import notifications
        from comms import types as notification_types
        notifications.notify(
            prescription.patient, notification_types.PRESCRIPTION_UPDATED,
            "Prescription cancelled",
            f"{notifications.display_name(prescription.doctor)} cancelled a "
            f"prescription. Do not fill it.",
            source="pharmacy.Prescription", reference=prescription.id)

    logger.info("Prescription %s -> %s", prescription.id, to_status)
    return prescription


def prescribable_patients(doctor):
    """Patients this doctor may prescribe for — those they actually treat."""
    from appointments.models import Appointment

    patient_ids = (Appointment.objects.filter(provider=doctor)
                   .values_list("patient_id", flat=True).distinct())
    return (User.objects.filter(pk__in=patient_ids)
            .filter(Q(account__role=roles.PATIENT) | Q(account__isnull=True))
            .order_by("first_name", "username"))


# ---------------------------------------------------------------------------
# Pharmacy: inventory
# ---------------------------------------------------------------------------
def upsert_inventory(pharmacy_user, medication_id, quantity=None, price=None,
                     is_active=None, low_stock_threshold=None):
    """Add a medication to this pharmacy's shelf, or restock an existing line.

    Upsert rather than create-then-fail: the unique constraint means a second
    "add" of the same product is not a new row, and the operator who pressed
    "Add" meant "I now hold this many", not "reject my edit".

    ``quantity`` is set, not incremented — it is a stock count, and a pharmacy
    counting its shelf reports a total. ``reserved`` is never touched here: only
    the request lifecycle moves it, so a restock can never quietly cancel a
    promise already made to a patient.
    """
    if role_of(pharmacy_user) != roles.PHARMACY:
        raise NotAuthorized("Only pharmacy accounts can manage inventory.")

    medication = get_medication(medication_id)

    with transaction.atomic():
        line, created = (PharmacyInventory.objects
                         .select_for_update()
                         .get_or_create(pharmacy=pharmacy_user,
                                        medication=medication))
        fields = ["updated_at"]
        if quantity is not None:
            new_quantity = int(quantity)
            if new_quantity < 0:
                raise InvalidRequest("Stock cannot be negative.")
            if new_quantity < line.reserved:
                # The shelf cannot hold less than what is already promised —
                # the database would refuse it, so refuse it with an
                # explanation rather than an IntegrityError.
                raise InsufficientStock(
                    f"{line.reserved} unit(s) are reserved for confirmed "
                    f"requests, so stock cannot be set below that.",
                    medication=medication.label, available=line.reserved,
                    requested=new_quantity)
            line.quantity = new_quantity
            fields.append("quantity")
        if price is not None:
            line.price = price
            fields.append("price")
        if is_active is not None:
            line.is_active = bool(is_active)
            fields.append("is_active")
        if low_stock_threshold is not None:
            line.low_stock_threshold = max(int(low_stock_threshold), 0)
            fields.append("low_stock_threshold")
        line.save(update_fields=fields)

    logger.info("Inventory %s %s for %s: qty=%s", line.id,
                "created" if created else "updated", pharmacy_user.username,
                line.quantity)
    return line, created


def get_inventory_line(pharmacy_user, line_id):
    line = inventory_for(pharmacy_user).filter(pk=line_id).first()
    if line is None:
        raise NotFound()
    return line


def search_inventory(pharmacy_user, term="", availability=None):
    """The pharmacy's own stock, filtered the way its UI offers.

    ``availability`` is ``"in_stock"`` / ``"out_of_stock"`` / ``None``, and is
    evaluated against *available* quantity rather than raw stock — a shelf whose
    every unit is reserved has nothing to offer.
    """
    queryset = inventory_for(pharmacy_user)
    term = (term or "").strip()
    if term:
        queryset = queryset.filter(Q(medication__name__icontains=term)
                                   | Q(medication__generic_name__icontains=term))
    if availability == "in_stock":
        queryset = queryset.filter(is_active=True, quantity__gt=F("reserved"))
    elif availability == "out_of_stock":
        queryset = queryset.filter(Q(quantity__lte=F("reserved"))
                                   | Q(is_active=False))
    return queryset


# ---------------------------------------------------------------------------
# Availability — the real answer, read from inventory
# ---------------------------------------------------------------------------
def pharmacies_with(medication_id, quantity=1, include_out_of_stock=True):
    """Which pharmacies can supply this medication, right now.

    Read from ``PharmacyInventory`` and from nothing else: there is no fallback
    list, no cached figure and no default "probably available". A pharmacy that
    has never stocked the product does not appear at all; one that stocks it and
    has none left appears as out of stock, because "we don't have it" is an
    answer a patient needs.

    Only pharmacies whose profile says they are open for business are listed —
    the same ``available`` switch every other facility honours.
    """
    from accounts.models import PharmacyProfile

    medication = get_medication(medication_id)
    units = max(int(quantity or 1), 1)

    lines = (PharmacyInventory.objects
             .select_related("pharmacy")
             .filter(medication=medication, is_active=True))

    profiles = {
        profile.user_id: profile
        for profile in PharmacyProfile.objects
        .filter(available=True,
                user_id__in=lines.values_list("pharmacy_id", flat=True))
        .select_related("user")
    }

    results = []
    for line in lines:
        profile = profiles.get(line.pharmacy_id)
        if profile is None:
            # No profile, or not accepting work. Not an error — just not on
            # offer, the same way an unavailable lab is not offered a booking.
            continue
        available = line.available_quantity
        can_supply = available >= units
        if not can_supply and not include_out_of_stock:
            continue
        results.append({
            "pharmacy_id": line.pharmacy_id,
            "name": profile.name or line.pharmacy.username,
            "address": profile.address,
            "phone": profile.phone,
            "verified": profile.verified,
            "medication_id": medication.id,
            "medication": medication.label,
            # Availability is a computed answer, never a stored flag.
            "available": can_supply,
            # The number a patient can actually be promised — not the shelf
            # total, which would include units already reserved for someone
            # else. Internal figures (raw stock, reservations) stay internal.
            "quantity_available": available,
            "price": line.price,
            "requested_quantity": units,
        })

    # Available first, then cheapest, then by name — the order a patient
    # comparing pharmacies actually wants. Unpriced lines sort after priced
    # ones rather than as if they were free.
    def sort_key(entry):
        price = entry["price"]
        return (not entry["available"],
                (1, Decimal("0")) if price is None else (0, price),
                entry["name"].lower())

    return sorted(results, key=sort_key)


def availability_for_prescription(user, prescription_id):
    """Per-item availability for a whole prescription.

    a prescription is not assumed to be fillable at one pharmacy.
    This answers each line independently, which is what lets the UI show that
    two of three medications are at Pharmacy A and the third only at B.
    """
    prescription = get_prescription(user, prescription_id)
    return [
        {
            "item_id": item.id,
            "medication_id": item.medication_id,
            "medication": item.medication.label,
            "quantity": item.quantity,
            "pharmacies": pharmacies_with(item.medication_id, item.quantity),
        }
        for item in prescription.items.select_related("medication")
    ]


# ---------------------------------------------------------------------------
# Patient: requesting
# ---------------------------------------------------------------------------
def _open_request_for_item(prescription_item_id):
    return (MedicationRequestItem.objects
            .filter(prescription_item_id=prescription_item_id,
                    request__status__in=MedicationRequest.OPEN_STATUSES)
            .select_related("request").first())


def create_request(patient, pharmacy_id, items, prescription_id=None, note=""):
    """A patient asks one pharmacy to put medication aside.

    **Nothing is reserved here.** A submitted request is a question, and the
    pharmacy answers it by confirming — which is the point at which stock is
    actually held. Availability is still checked now, so a patient is not left
    waiting on a request that was never fillable, but that check is advisory and
    the authoritative one happens under a lock at confirmation.
    """
    if role_of(patient) != roles.PATIENT:
        raise NotAuthorized("Only patients can request medication.")
    if not items:
        raise InvalidRequest("A request needs at least one medication.")

    try:
        pharmacy = User.objects.get(pk=int(pharmacy_id))
    except (User.DoesNotExist, ValueError, TypeError):
        raise NotFound()
    if role_of(pharmacy) != roles.PHARMACY:
        raise NotFound()

    prescription = None
    if prescription_id:
        # Scoped through prescriptions_for: another patient's prescription id
        # is simply not found, so a request cannot be attached to it.
        prescription = get_prescription(patient, prescription_id)
        if prescription.status != Prescription.ISSUED:
            raise InvalidRequest(
                "Only an issued prescription can be taken to a pharmacy.")

    resolved = []
    seen = set()
    for entry in items:
        prescription_item = None
        item_id = entry.get("prescription_item_id")
        if item_id:
            if prescription is None:
                raise InvalidRequest(
                    "A prescription line was given without its prescription.")
            prescription_item = prescription.items.filter(pk=item_id).first()
            if prescription_item is None:
                raise NotFound()
            # One live request per prescription line. Enforced here rather than
            # by a constraint: the rule spans the item row and its parent's
            # status, which a single partial index cannot express.
            existing = _open_request_for_item(prescription_item.id)
            if existing is not None:
                raise DuplicateRequest(
                    f"{prescription_item.medication.label} is already on an "
                    f"open request (#{existing.request_id}).")
            medication = prescription_item.medication
            quantity = int(entry.get("quantity") or prescription_item.quantity)
        else:
            medication = get_medication(entry.get("medication_id"))
            quantity = int(entry.get("quantity") or 1)

        if quantity < 1:
            raise InvalidRequest("Quantity must be at least one unit.")
        if medication.id in seen:
            raise InvalidRequest(
                f"{medication.label} is listed twice on this request.")
        seen.add(medication.id)

        line = (PharmacyInventory.objects
                .filter(pharmacy=pharmacy, medication=medication,
                        is_active=True).first())
        if line is None:
            raise InvalidRequest(
                f"This pharmacy does not stock {medication.label}.")
        if not line.can_supply(quantity):
            raise InsufficientStock(
                f"{medication.label}: only {line.available_quantity} unit(s) "
                f"available.", medication=medication.label,
                available=line.available_quantity, requested=quantity)
        resolved.append((medication, quantity, line.price, prescription_item))

    with transaction.atomic():
        request = MedicationRequest.objects.create(
            patient=patient, pharmacy=pharmacy, prescription=prescription,
            note=note or "")
        for medication, quantity, price, prescription_item in resolved:
            MedicationRequestItem.objects.create(
                request=request, medication=medication, quantity=quantity,
                # Snapshot: repricing the shelf must not reprice a request
                # already placed.
                unit_price=price, prescription_item=prescription_item)

    from comms import notifications
    notifications.notify(
        pharmacy, notification_types.PHARMACY_REQUEST_CREATED,
        "New medication request",
        f"{notifications.patient_name(patient)} sent a request with "
        f"{len(resolved)} item(s).",
        source="pharmacy.MedicationRequest", reference=request.id)

    logger.info("Medication request %s created by %s at pharmacy %s",
                request.id, patient.username, pharmacy.username)
    return request


# ---------------------------------------------------------------------------
# The request lifecycle, and the stock that moves with it
# ---------------------------------------------------------------------------
def _locked_lines(request):
    """The pharmacy's inventory rows for this request, locked and ordered.

    Locked with ``select_for_update`` so two concurrent confirmations serialise
    instead of both reading the same free balance. Ordered by primary key
    because two requests holding overlapping medications must take their locks
    in the same sequence — unordered locking is how deadlocks are written.
    """
    medication_ids = list(request.items.values_list("medication_id", flat=True))
    lines = (PharmacyInventory.objects
             .select_for_update()
             .filter(pharmacy=request.pharmacy,
                     medication_id__in=medication_ids)
             .order_by("pk"))
    return {line.medication_id: line for line in lines}


def _reserve(request):
    """Hold stock for a confirmed request, or refuse the whole request.

    All-or-nothing: a request half-reserved would be a promise the pharmacy
    cannot keep on the other half. The caller runs this inside a transaction, so
    raising rolls back every line already touched.
    """
    lines = _locked_lines(request)
    for item in request.items.select_related("medication"):
        line = lines.get(item.medication_id)
        if line is None or not line.is_active:
            raise InsufficientStock(
                f"{item.medication.label} is no longer stocked here.",
                medication=item.medication.label, available=0,
                requested=item.quantity)
        # Re-read under the lock: whatever was available when the patient
        # submitted is not evidence of what is available now.
        if line.available_quantity < item.quantity:
            raise InsufficientStock(
                f"{item.medication.label}: only {line.available_quantity} "
                f"unit(s) left, {item.quantity} requested.",
                medication=item.medication.label,
                available=line.available_quantity, requested=item.quantity)
        line.reserved = line.reserved + item.quantity
        line.save(update_fields=["reserved", "updated_at"])


def _release(request):
    """Give reserved stock back. Only ever called when it is actually held."""
    lines = _locked_lines(request)
    for item in request.items.all():
        line = lines.get(item.medication_id)
        if line is None:
            continue
        line.reserved = max(line.reserved - item.quantity, 0)
        line.save(update_fields=["reserved", "updated_at"])


def _dispense(request):
    """Hand the medication over: stock down, reservation down, together."""
    lines = _locked_lines(request)
    for item in request.items.all():
        line = lines.get(item.medication_id)
        if line is None:
            continue
        line.quantity = max(line.quantity - item.quantity, 0)
        line.reserved = max(line.reserved - item.quantity, 0)
        line.save(update_fields=["quantity", "reserved", "updated_at"])


#: Status -> the timestamp column it stamps.
_TIMESTAMP_FOR = {
    MedicationRequest.CONFIRMED: "confirmed_at",
    MedicationRequest.READY: "ready_at",
    MedicationRequest.COMPLETED: "completed_at",
}


def transition_request(user, request_id, to_status, reason="", note=""):
    """Move a request one legal step, moving stock with it.

    Who may ask for which status is decided here from the caller's *role*, not
    from anything in the request body: a patient may withdraw, and the rest of
    the workflow belongs to the pharmacy holding the request.
    """
    role = role_of(user)
    if role not in (roles.PATIENT, roles.PHARMACY):
        raise NotAuthorized("Your account cannot update medication requests.")

    # Scoped: another patient's or another pharmacy's request is not found.
    found = requests_for(user).filter(pk=request_id).first()
    if found is None:
        raise NotFound()

    permitted = (MedicationRequest.PATIENT_STATUSES if role == roles.PATIENT
                 else MedicationRequest.PHARMACY_STATUSES)
    if to_status not in permitted:
        raise NotAuthorized(
            f"A {roles.label(role).lower()} account cannot set a request to "
            f"{to_status}.")

    with transaction.atomic():
        # Re-read under a row lock. Between the scoped read above and here, the
        # other party may have moved it — cancelling a request the pharmacy just
        # completed must fail, not silently unwind a dispense.
        request = (MedicationRequest.objects
                   .select_for_update()
                   .select_related("patient", "pharmacy")
                   .get(pk=found.pk))
        if not request.can_transition_to(to_status):
            raise InvalidTransition(
                f"A {request.get_status_display().lower()} request cannot "
                f"become {to_status}.")

        fields = ["status", "updated_at"]
        if to_status == MedicationRequest.CONFIRMED:
            _reserve(request)
            request.stock_reserved = True
            fields.append("stock_reserved")
        elif to_status == MedicationRequest.COMPLETED:
            if request.stock_reserved:
                _dispense(request)
                request.stock_reserved = False
                fields.append("stock_reserved")
        elif to_status in (MedicationRequest.CANCELLED,
                           MedicationRequest.REJECTED):
            if request.stock_reserved:
                _release(request)
                request.stock_reserved = False
                fields.append("stock_reserved")

        request.status = to_status
        stamp = _TIMESTAMP_FOR.get(to_status)
        if stamp:
            setattr(request, stamp, timezone.now())
            fields.append(stamp)
        if reason:
            request.cancellation_reason = reason[:255]
            fields.append("cancellation_reason")
        if note:
            request.pharmacy_note = note[:255]
            fields.append("pharmacy_note")
        request.save(update_fields=fields)
        _announce_request_status(request, actor=user)

    logger.info("Medication request %s -> %s (by %s)", request.id, to_status,
                user.username)
    return get_request(user, request.id)


#: Request status -> what the *patient* is told. The pharmacy set the status
#: itself, so it is not notified about its own action; the one exception is a
#: patient cancelling, which the pharmacy does need to hear about.
_PATIENT_NOTICE = {
    MedicationRequest.CONFIRMED: (
        notification_types.PHARMACY_REQUEST_CONFIRMED,
        "Medication request confirmed",
        "{pharmacy} confirmed your request."),
    MedicationRequest.REJECTED: (
        notification_types.PHARMACY_REQUEST_REJECTED,
        "Medication request rejected",
        "{pharmacy} could not fill your request."),
    MedicationRequest.PREPARING: (
        notification_types.PHARMACY_ORDER_PREPARING,
        "Medication being prepared",
        "{pharmacy} is preparing your medication."),
    MedicationRequest.READY: (
        notification_types.PHARMACY_ORDER_READY,
        "Medication ready for pickup",
        "Your medication is ready to collect at {pharmacy}."),
    MedicationRequest.COMPLETED: (
        notification_types.PHARMACY_ORDER_COMPLETED,
        "Medication collected",
        "Your medication was collected from {pharmacy}."),
}


def _announce_request_status(request, actor):
    """Tell whichever party did *not* make the change."""
    from comms import notifications

    source, reference = "pharmacy.MedicationRequest", request.id
    pharmacy_name = notifications.display_name(request.pharmacy)

    if request.status == MedicationRequest.CANCELLED:
        # Whoever cancelled knows; the other side needs to be told.
        recipient = (request.pharmacy if actor.pk == request.patient_id
                     else request.patient)
        notifications.notify(
            recipient, notification_types.PHARMACY_REQUEST_CANCELLED,
            "Medication request cancelled",
            (f"{notifications.patient_name(request.patient)} cancelled their "
             f"request." if recipient.pk == request.pharmacy_id
             else f"{pharmacy_name} cancelled your request."),
            source=source, reference=reference)
        return

    notice = _PATIENT_NOTICE.get(request.status)
    if notice is None:
        return
    notification_type, title, template = notice
    notifications.notify(
        request.patient, notification_type, title,
        template.format(pharmacy=pharmacy_name),
        source=source, reference=reference)


# ---------------------------------------------------------------------------
# Dashboards
# ---------------------------------------------------------------------------
def pharmacy_counts(pharmacy_user):
    """Real figures for the pharmacy dashboard. Nothing invented.

    Every number is a query. Where a figure genuinely cannot be produced it is
    left out rather than defaulted, so the dashboard's own "not tracked yet"
    machinery keeps working.
    """
    today = timezone.localtime().date()
    requests = MedicationRequest.objects.filter(pharmacy=pharmacy_user)
    stock = PharmacyInventory.objects.filter(pharmacy=pharmacy_user,
                                             is_active=True)

    by_status = {row["status"]: row["count"] for row in
                 requests.values("status").annotate(count=Count("id"))}

    low_stock = stock.filter(low_stock_threshold__gt=0).filter(
        quantity__lte=F("reserved") + F("low_stock_threshold")).count()

    return {
        # Named to match the tiles the facility dashboard already renders, so
        # 'pending_orders' stops being an unsupported metric for pharmacies.
        "pending_orders": by_status.get(MedicationRequest.PENDING, 0),
        "in_progress": (by_status.get(MedicationRequest.CONFIRMED, 0)
                        + by_status.get(MedicationRequest.PREPARING, 0)),
        "results_ready": by_status.get(MedicationRequest.READY, 0),
        "orders_today": requests.filter(created_at__date=today).count(),
        "completed_orders": by_status.get(MedicationRequest.COMPLETED, 0),
        "inventory_items": stock.count(),
        "out_of_stock_items": stock.filter(quantity__lte=F("reserved")).count(),
        "low_stock_items": low_stock,
        "units_in_stock": stock.aggregate(total=Sum("quantity"))["total"] or 0,
    }


def patient_pharmacy_summary(patient):
    """The patient dashboard's pharmacy block."""
    prescriptions = prescriptions_for(patient)
    requests = MedicationRequest.objects.filter(patient=patient)
    return {
        "active_prescriptions": prescriptions.filter(
            status=Prescription.ISSUED).count(),
        "prescribed_medications": PrescriptionItem.objects.filter(
            prescription__patient=patient,
            prescription__status=Prescription.ISSUED).count(),
        "open_requests": requests.filter(
            status__in=MedicationRequest.OPEN_STATUSES).count(),
        "requests_ready": requests.filter(
            status=MedicationRequest.READY).count(),
    }


def doctor_pharmacy_summary(doctor):
    """The doctor dashboard's prescribing block.

    Counts of their own prescribing only. Whether a patient collected the
    medication is dispensing information the brief does not route back to the
    prescriber, so no figure here reports it.
    """
    written = Prescription.objects.filter(doctor=doctor)
    return {
        "prescriptions_written": written.count(),
        "prescriptions_issued": written.filter(
            status=Prescription.ISSUED).count(),
        "prescriptions_draft": written.filter(
            status=Prescription.DRAFT).count(),
    }
