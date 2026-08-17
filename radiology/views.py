"""Radiology endpoints.

Thin, like the rest of the API: validate, delegate to ``services``, translate
the domain exception into the project's standard error envelope. No view assigns
a status field or filters a queryset by hand — both come from the service layer,
which is the only place that knows the rules.
"""
import logging

from django.http import FileResponse
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import HasRole, IsDoctor, IsPatient
from accounts import roles
from appointments.exceptions import api_error
from appointments.validators import validate_image_upload

from . import modalities, services
from .models import ImagingFile
from .serializers import (
    BookOrderSerializer, ExaminationSerializer, ImagingFileSerializer,
    ImagingOrderCreateSerializer, ImagingOrderSerializer,
    RadiologyReportSerializer, ReportDraftSerializer, SelfBookSerializer,
    StatusChangeSerializer,
)

logger = logging.getLogger("appointments")

IsRadiologyCentre = HasRole.of(
    roles.RADIOLOGY,
    message="Only radiology centre accounts can access this resource.")


#: Domain exception -> HTTP status. ``NotFound`` also covers cross-boundary
#: access: answering 403 there would confirm the record exists, which is exactly
#: what someone probing ids wants to learn.
_STATUS_FOR = [
    (services.NotFound, status.HTTP_404_NOT_FOUND),
    (services.NotAuthorized, status.HTTP_403_FORBIDDEN),
    (services.InvalidTransition, status.HTTP_409_CONFLICT),
    (services.OrderNotBookable, status.HTTP_400_BAD_REQUEST),
    (services.ServiceMismatch, status.HTTP_400_BAD_REQUEST),
]

_DEFAULT_MESSAGE = {status.HTTP_404_NOT_FOUND: "Not found",
                    status.HTTP_403_FORBIDDEN: "Not permitted"}


class _RadiologyView(APIView):
    """Base view that turns this module's domain exceptions into API errors.

    ``handle_exception`` is DRF's own hook and runs *inside* its dispatch, which
    is why the translation belongs here: an exception raised in a handler never
    escapes ``dispatch``, so wrapping the call from outside would only ever see
    the 500 the default handler had already produced.
    """

    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def handle_exception(self, exc):
        for exception_type, code in _STATUS_FOR:
            if isinstance(exc, exception_type):
                return api_error(str(exc) or _DEFAULT_MESSAGE.get(code, "Error"),
                                 code)
        return super().handle_exception(exc)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
class Modalities(APIView):
    """The imaging vocabulary, so clients never hardcode it."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            [{"value": m, "label": modalities.label(m)} for m in modalities.ALL],
            status=status.HTTP_200_OK)


class CentersOffering(APIView):
    """Radiology centres that actually offer the requested modality."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        modality = request.query_params.get("modality")
        if modality and not modalities.is_valid(modality):
            return api_error(f"{modality!r} is not a known imaging modality.",
                             status.HTTP_400_BAD_REQUEST)

        from appointments.serializers import ServiceSerializer
        return Response([
            {**center,
             "services": ServiceSerializer(center["services"], many=True).data}
            for center in services.centers_offering(modality)
        ], status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Imaging orders
# ---------------------------------------------------------------------------
class ImagingOrders(_RadiologyView):
    """GET the caller's own orders (scoped by role) · POST a new one (doctors)."""

    def get(self, request):
        queryset = services.orders_for(request.user)
        state = request.query_params.get("status")
        if state:
            queryset = queryset.filter(status=state)
        return Response(ImagingOrderSerializer(queryset, many=True).data,
                        status=status.HTTP_200_OK)

    def post(self, request):
        if not IsDoctor().has_permission(request, self):
            return api_error("Only doctors can create imaging orders.",
                             status.HTTP_403_FORBIDDEN)
        serializer = ImagingOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        order = services.create_order(request.user, **serializer.validated_data)
        return Response(ImagingOrderSerializer(order).data,
                        status=status.HTTP_201_CREATED)


class ImagingOrderDetail(_RadiologyView):
    def get(self, request, order_id):
        order = services.get_order(request.user, order_id)
        return Response(ImagingOrderSerializer(order).data,
                        status=status.HTTP_200_OK)


class CancelImagingOrder(_RadiologyView):
    def post(self, request, order_id):
        order = services.cancel_order(
            request.user, order_id, request.data.get("reason", ""))
        return Response(ImagingOrderSerializer(order).data,
                        status=status.HTTP_200_OK)


class BookImagingOrder(_RadiologyView):
    """Book an appointment that fulfils an existing order.

    Attaches the booking to the order rather than creating a second one — the
    engine does the scheduling, this only links the clinical request to it.
    """
    permission_classes = [IsPatient]

    def post(self, request, order_id):
        serializer = BookOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order, appointment, examination = services.book_for_order(
                request.user, order_id, **serializer.validated_data)
        except Exception as exc:
            from appointments.services import scheduling as engine
            if isinstance(exc, engine.SlotTaken):
                return api_error("That time slot is already taken. "
                                 "Please choose another.",
                                 status.HTTP_409_CONFLICT)
            if isinstance(exc, (engine.OutsideAvailability,
                                engine.ProviderUnavailable)):
                return api_error(str(exc), status.HTTP_400_BAD_REQUEST)
            if isinstance(exc, engine.SlotUnavailable):
                return api_error("That time slot is already taken.",
                                 status.HTTP_409_CONFLICT)
            raise

        return Response({
            "order": ImagingOrderSerializer(order).data,
            "examination": ExaminationSerializer(
                examination, context=self._context(request)).data,
            "appointment_id": appointment.id,
        }, status=status.HTTP_201_CREATED)

    def _context(self, request):
        return {"visible_report_ids": set(
            services.reports_for(request.user).values_list("id", flat=True))}


class SelfBookImaging(_RadiologyView):
    """A patient books imaging with no referral.

    Still recorded as an order so the study has a clinical record — with no
    doctor attached, which keeps it honestly distinguishable from a referral.
    """
    permission_classes = [IsPatient]

    def post(self, request):
        serializer = SelfBookSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order, appointment, examination = services.self_book(
                request.user, **serializer.validated_data)
        except Exception as exc:
            from appointments.services import scheduling as engine
            if isinstance(exc, engine.SlotTaken):
                return api_error("That time slot is already taken.",
                                 status.HTTP_409_CONFLICT)
            if isinstance(exc, (engine.OutsideAvailability,
                                engine.ProviderUnavailable)):
                return api_error(str(exc), status.HTTP_400_BAD_REQUEST)
            raise
        return Response({
            "order": ImagingOrderSerializer(order).data,
            "appointment_id": appointment.id,
            "examination_id": examination.id,
        }, status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Examinations
# ---------------------------------------------------------------------------
class Examinations(_RadiologyView):
    def get(self, request):
        queryset = services.examinations_for(request.user)
        state = request.query_params.get("status")
        if state:
            queryset = queryset.filter(status=state)
        if request.query_params.get("when") == "today":
            from django.utils import timezone
            queryset = queryset.filter(
                appointment__start_at__date=timezone.localtime().date())
        return Response(
            ExaminationSerializer(queryset, many=True,
                                  context=self._context(request)).data,
            status=status.HTTP_200_OK)

    def _context(self, request):
        return {"visible_report_ids": set(
            services.reports_for(request.user).values_list("id", flat=True))}


class ExaminationDetail(_RadiologyView):
    def get(self, request, examination_id):
        examination = services.get_examination(request.user, examination_id)
        context = {"visible_report_ids": set(
            services.reports_for(request.user).values_list("id", flat=True))}
        return Response(
            ExaminationSerializer(examination, context=context).data,
            status=status.HTTP_200_OK)


class ExaminationStatus(_RadiologyView):
    """Check in, start, complete — the centre's own studies only."""
    permission_classes = [IsRadiologyCentre]

    def post(self, request, examination_id):
        serializer = StatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        examination = services.advance_examination(
            request.user, examination_id, serializer.validated_data["status"])
        context = {"visible_report_ids": set(
            services.reports_for(request.user).values_list("id", flat=True))}
        return Response(
            ExaminationSerializer(examination, context=context).data,
            status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
class ExaminationFiles(_RadiologyView):
    """List an examination's files (metadata) · POST a new one (centre only)."""
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request, examination_id):
        examination = services.get_examination(request.user, examination_id)
        stored = services.files_for(request.user).filter(examination=examination)
        return Response(ImagingFileSerializer(stored, many=True).data,
                        status=status.HTTP_200_OK)

    def post(self, request, examination_id):
        if not IsRadiologyCentre().has_permission(request, self):
            return api_error("Only the radiology centre can attach imaging.",
                             status.HTTP_403_FORBIDDEN)
        uploaded = request.FILES.get("file")
        # Reuses the platform's existing upload validation: size, extension,
        # declared type, and an authoritative decode.
        uploaded = validate_image_upload(uploaded)

        stored = services.attach_file(
            request.user, examination_id, uploaded,
            kind=request.data.get("kind", ImagingFile.IMAGE),
            description=request.data.get("description", ""),
            study_uid=request.data.get("study_uid", ""),
            series_uid=request.data.get("series_uid", ""),
            instance_uid=request.data.get("instance_uid", ""))
        return Response(ImagingFileSerializer(stored).data,
                        status=status.HTTP_201_CREATED)


class ImagingFileDownload(_RadiologyView):
    """Stream one imaging file, after proving the caller may read it.

    This is the only route to the bytes. ``MEDIA_URL`` is not wired into the
    URLconf, so there is no path that skips this check — which is the whole
    reason the files are not served statically.
    """

    def get(self, request, file_id):
        stored = services.get_file(request.user, file_id)
        logger.info("Imaging file %s served to %s (%s)", stored.id,
                    request.user.username, request.user.id)
        response = FileResponse(stored.file.open("rb"),
                                content_type=stored.content_type
                                or "application/octet-stream")
        # `attachment` rather than inline: never render patient imaging in the
        # browsing context of whatever page requested it.
        response["Content-Disposition"] = (
            f'attachment; filename="{stored.original_name or stored.file.name}"')
        response["X-Content-Type-Options"] = "nosniff"
        return response


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
class Reports(_RadiologyView):
    def get(self, request):
        queryset = services.reports_for(request.user)
        return Response(RadiologyReportSerializer(queryset, many=True).data,
                        status=status.HTTP_200_OK)


class ExaminationReport(_RadiologyView):
    """Write or update the draft for one of the centre's completed studies."""
    permission_classes = [IsRadiologyCentre]

    def post(self, request, examination_id):
        serializer = ReportDraftSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = services.draft_report(request.user, examination_id,
                                       **serializer.validated_data)
        return Response(RadiologyReportSerializer(report).data,
                        status=status.HTTP_200_OK)


class ReportStatus(_RadiologyView):
    """Submit for review, verify, release — authorized radiology users only."""
    permission_classes = [IsRadiologyCentre]

    def post(self, request, report_id):
        serializer = StatusChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        report = services.transition_report(
            request.user, report_id, serializer.validated_data["status"])
        return Response(RadiologyReportSerializer(report).data,
                        status=status.HTTP_200_OK)
