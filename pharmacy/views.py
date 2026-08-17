"""Pharmacy endpoints.

Thin, like the rest of the API: validate, delegate to ``services``, translate
the domain exception into the project's standard error envelope. No view assigns
a status, moves stock or filters a queryset by hand — all three come from the
service layer, which is the only place that knows the rules.
"""
import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts import roles
from accounts.permissions import IsDoctor, IsPatient, IsPharmacy
from appointments.exceptions import api_error

from . import dosage_forms, services
from .serializers import (
    InventorySerializer, InventoryUpdateSerializer, InventoryWriteSerializer,
    MedicationCreateSerializer, MedicationRequestSerializer,
    MedicationSerializer, PharmacyRequestSerializer,
    PrescriptionCreateSerializer, PrescriptionSerializer,
    PrescriptionStatusSerializer, RequestCreateSerializer,
    StatusChangeSerializer,
)

logger = logging.getLogger("appointments")


#: Domain exception -> HTTP status. ``NotFound`` also covers cross-boundary
#: access: answering 403 there would confirm the record exists, which is exactly
#: what someone probing ids wants to learn.
_STATUS_FOR = [
    (services.NotFound, status.HTTP_404_NOT_FOUND),
    (services.NotAuthorized, status.HTTP_403_FORBIDDEN),
    (services.InvalidTransition, status.HTTP_409_CONFLICT),
    (services.DuplicateRequest, status.HTTP_409_CONFLICT),
    # A stock shortfall is a conflict with the current state of the shelf, not
    # a malformed request: the same body would have succeeded a minute ago.
    (services.InsufficientStock, status.HTTP_409_CONFLICT),
    (services.InvalidRequest, status.HTTP_400_BAD_REQUEST),
]

_DEFAULT_MESSAGE = {status.HTTP_404_NOT_FOUND: "Not found",
                    status.HTTP_403_FORBIDDEN: "Not permitted"}


class _PharmacyView(APIView):
    """Base view that turns this module's domain exceptions into API errors.

    ``handle_exception`` is DRF's own hook and runs *inside* its dispatch, which
    is why the translation belongs here: an exception raised in a handler never
    escapes ``dispatch``, so wrapping the call from outside would only ever see
    the 500 the default handler had already produced.
    """

    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc):
        for exception_type, code in _STATUS_FOR:
            if isinstance(exc, exception_type):
                details = None
                if isinstance(exc, services.InsufficientStock):
                    details = {"medication": exc.medication,
                               "available": exc.available,
                               "requested": exc.requested}
                return api_error(str(exc) or _DEFAULT_MESSAGE.get(code, "Error"),
                                 code, details=details)
        return super().handle_exception(exc)


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------
class DosageForms(APIView):
    """The dosage-form vocabulary, so clients never hardcode it."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            [{"value": form, "label": dosage_forms.label(form)}
             for form in dosage_forms.ALL],
            status=status.HTTP_200_OK)


class Medications(_PharmacyView):
    """GET search the catalogue · POST add a product (doctors and pharmacies).

    Search is open to every authenticated role: a medication product is public
    reference data, and knowing that "Amoxicillin 500 mg capsule" exists
    discloses nothing about any patient.
    """

    def get(self, request):
        found = services.search_medications(request.query_params.get("q", ""))
        return Response(MedicationSerializer(found, many=True).data,
                        status=status.HTTP_200_OK)

    def post(self, request):
        serializer = MedicationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        medication, created = services.find_or_create_medication(
            request.user, **serializer.validated_data)
        return Response(
            MedicationSerializer(medication).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class MedicationDetail(_PharmacyView):
    def get(self, request, medication_id):
        medication = services.get_medication(medication_id)
        return Response(MedicationSerializer(medication).data,
                        status=status.HTTP_200_OK)


class Pharmacies(_PharmacyView):
    """Registered pharmacies that are open for business.

    Contact details and address only. There is no location/geospatial
    architecture in Roshada, so distance is not offered rather than approximated
    — a made-up "2.3 km away" is worse than no distance at all.
    """

    def get(self, request):
        from accounts.models import PharmacyProfile

        term = (request.query_params.get("q") or "").strip()
        queryset = PharmacyProfile.objects.filter(
            available=True).select_related("user")
        if term:
            queryset = queryset.filter(name__icontains=term)
        return Response([
            {"id": profile.user_id, "name": profile.name,
             "address": profile.address, "phone": profile.phone,
             "email": profile.email, "verified": profile.verified,
             "operating_hours": profile.operating_hours}
            for profile in queryset
        ], status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Prescriptions
# ---------------------------------------------------------------------------
class Prescriptions(_PharmacyView):
    """GET the caller's own prescriptions (scoped by role) · POST one (doctors)."""

    def get(self, request):
        queryset = services.prescriptions_for(request.user)
        state = request.query_params.get("status")
        if state:
            queryset = queryset.filter(status=state)
        return Response(PrescriptionSerializer(queryset, many=True).data,
                        status=status.HTTP_200_OK)

    def post(self, request):
        if not IsDoctor().has_permission(request, self):
            return api_error("Only doctors can write prescriptions.",
                             status.HTTP_403_FORBIDDEN)
        serializer = PrescriptionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        prescription = services.create_prescription(
            request.user, **serializer.validated_data)
        return Response(PrescriptionSerializer(prescription).data,
                        status=status.HTTP_201_CREATED)


class PrescriptionDetail(_PharmacyView):
    def get(self, request, prescription_id):
        prescription = services.get_prescription(request.user, prescription_id)
        return Response(PrescriptionSerializer(prescription).data,
                        status=status.HTTP_200_OK)


class PrescriptionStatus(_PharmacyView):
    """Issue or cancel — the prescribing doctor only."""
    permission_classes = [IsDoctor]

    def post(self, request, prescription_id):
        serializer = PrescriptionStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        prescription = services.transition_prescription(
            request.user, prescription_id,
            serializer.validated_data["status"],
            serializer.validated_data.get("reason", ""))
        return Response(PrescriptionSerializer(prescription).data,
                        status=status.HTTP_200_OK)


class PrescriptionPharmacies(_PharmacyView):
    """Where every medication on this prescription can be filled.

    One answer per line, because a prescription is not assumed to be fillable
    at a single pharmacy.
    """

    def get(self, request, prescription_id):
        return Response(
            services.availability_for_prescription(request.user,
                                                   prescription_id),
            status=status.HTTP_200_OK)


class PrescribablePatients(_PharmacyView):
    """The patients this doctor may prescribe for."""
    permission_classes = [IsDoctor]

    def get(self, request):
        return Response([
            {"id": patient.id,
             "name": patient.get_full_name() or patient.username,
             "username": patient.username}
            for patient in services.prescribable_patients(request.user)
        ], status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Inventory — a pharmacy's own shelf, never another's
# ---------------------------------------------------------------------------
class Inventory(_PharmacyView):
    """GET this pharmacy's stock · POST add or restock a line."""
    permission_classes = [IsPharmacy]

    def get(self, request):
        queryset = services.search_inventory(
            request.user,
            term=request.query_params.get("q", ""),
            availability=request.query_params.get("availability"))
        return Response(InventorySerializer(queryset, many=True).data,
                        status=status.HTTP_200_OK)

    def post(self, request):
        serializer = InventoryWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        # request.user, never a pharmacy id from the body: which shelf is being
        # written to is not the client's to choose.
        line, created = services.upsert_inventory(request.user,
                                                  **serializer.validated_data)
        return Response(
            InventorySerializer(line).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class InventoryDetail(_PharmacyView):
    """Update or read one stock line the caller owns."""
    permission_classes = [IsPharmacy]

    def get(self, request, line_id):
        line = services.get_inventory_line(request.user, line_id)
        return Response(InventorySerializer(line).data,
                        status=status.HTTP_200_OK)

    def patch(self, request, line_id):
        # Scoped read first: another pharmacy's line id is 'not found', so the
        # endpoint cannot be used to discover that it exists.
        line = services.get_inventory_line(request.user, line_id)
        serializer = InventoryUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated, _created = services.upsert_inventory(
            request.user, line.medication_id, **serializer.validated_data)
        return Response(InventorySerializer(updated).data,
                        status=status.HTTP_200_OK)


class MedicationAvailability(_PharmacyView):
    """Which pharmacies have a medication, right now.

    The patient-facing availability search: usable on its own, and
    it creates nothing — searching for a medication is not prescribing it.
    """

    def get(self, request):
        medication_id = request.query_params.get("medication")
        if not medication_id:
            return api_error("Give a medication to search for.",
                             status.HTTP_400_BAD_REQUEST)
        try:
            quantity = int(request.query_params.get("quantity") or 1)
        except (TypeError, ValueError):
            return api_error("Quantity must be a number.",
                             status.HTTP_400_BAD_REQUEST)

        include_out = request.query_params.get("include_out_of_stock", "true")
        return Response(
            services.pharmacies_with(
                medication_id, quantity,
                include_out_of_stock=include_out.lower() != "false"),
            status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Medication requests
# ---------------------------------------------------------------------------
def _request_serializer_for(user):
    """Which view of a request this caller is entitled to.

    The pharmacy's serializer is the narrower one — same records, less of the
    prescription behind them.
    """
    from accounts.services import role_of

    if role_of(user) == roles.PHARMACY:
        return PharmacyRequestSerializer
    return MedicationRequestSerializer


class MedicationRequests(_PharmacyView):
    """GET the caller's requests (scoped by role) · POST a new one (patients)."""

    def get(self, request):
        queryset = services.requests_for(request.user)
        state = request.query_params.get("status")
        if state == "open":
            from .models import MedicationRequest
            queryset = queryset.filter(
                status__in=MedicationRequest.OPEN_STATUSES)
        elif state:
            queryset = queryset.filter(status=state)
        serializer = _request_serializer_for(request.user)
        return Response(serializer(queryset, many=True).data,
                        status=status.HTTP_200_OK)

    def post(self, request):
        if not IsPatient().has_permission(request, self):
            return api_error("Only patients can request medication.",
                             status.HTTP_403_FORBIDDEN)
        serializer = RequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        created = services.create_request(request.user,
                                          **serializer.validated_data)
        return Response(MedicationRequestSerializer(created).data,
                        status=status.HTTP_201_CREATED)


class MedicationRequestDetail(_PharmacyView):
    def get(self, request, request_id):
        found = services.get_request(request.user, request_id)
        serializer = _request_serializer_for(request.user)
        return Response(serializer(found).data, status=status.HTTP_200_OK)


class MedicationRequestStatus(_PharmacyView):
    """Confirm, reject, prepare, mark ready, complete — or cancel.

    Both parties post here. Which statuses each may ask for is decided in the
    service layer from the caller's role, not from anything in the body.
    """

    def post(self, request, request_id):
        serializer = StatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        updated = services.transition_request(
            request.user, request_id,
            serializer.validated_data["status"],
            reason=serializer.validated_data.get("reason", ""),
            note=serializer.validated_data.get("note", ""))
        response_serializer = _request_serializer_for(request.user)
        return Response(response_serializer(updated).data,
                        status=status.HTTP_200_OK)
