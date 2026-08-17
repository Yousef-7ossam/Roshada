"""Unified Medical Record endpoints.

Read-only, deliberately. the record is a viewing and aggregation
layer, so there is no endpoint here that writes to a lab result, a report, a
prescription or an appointment — the modules that own them are the only route
to changing them, and each still applies its own permission check when a reader
opens one.

Thin like the rest of the API: parse, delegate to ``services``, translate the
domain exception into the project's standard error envelope.
"""
import datetime
import logging

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsDoctor
from appointments.exceptions import api_error

from . import services, timeline

logger = logging.getLogger("appointments")

#: Ceiling on one page. A medical history can be long; an unbounded ``limit``
#: would let one request ask for all of it, which is the cost this bound exists
#: to avoid.
MAX_LIMIT = 100
DEFAULT_LIMIT = 25


class _RecordsView(APIView):
    """Base view that turns this module's exceptions into API errors.

    ``handle_exception`` is DRF's own hook and runs inside its dispatch, which
    is why the translation belongs here rather than around the call.
    """

    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc):
        if isinstance(exc, services.NotFound):
            # 404 rather than 403 for a record the caller may not see:
            # answering "forbidden" confirms the record exists.
            return api_error("No medical record found.",
                             status.HTTP_404_NOT_FOUND)
        return super().handle_exception(exc)


def _parse_date(value, end_of_day=False):
    """A YYYY-MM-DD filter bound, as an aware datetime, or None."""
    if not value:
        return None
    try:
        day = datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{value!r} is not a date in YYYY-MM-DD form.")
    moment = datetime.datetime.combine(
        day, datetime.time.max if end_of_day else datetime.time.min)
    return timezone.make_aware(moment, timezone.get_current_timezone())


def _paging(request):
    try:
        limit = int(request.query_params.get("limit") or DEFAULT_LIMIT)
        offset = int(request.query_params.get("offset") or 0)
    except (TypeError, ValueError):
        raise ValueError("limit and offset must be whole numbers.")
    return max(min(limit, MAX_LIMIT), 1), max(offset, 0)


def _types(request):
    """The requested event types, rejecting anything not in the vocabulary.

    An unknown type is an error rather than an empty result: silently returning
    nothing for a typo'd filter reads as "this patient has no lab results".
    """
    raw = request.query_params.get("type") or request.query_params.get("types")
    if not raw:
        return None
    wanted = [value.strip() for value in raw.split(",") if value.strip()]
    if not wanted or "all" in wanted:
        return None
    unknown = [value for value in wanted if not timeline.is_valid(value)]
    if unknown:
        raise ValueError(f"Unknown record type(s): {', '.join(unknown)}.")
    return wanted


class EventTypes(APIView):
    """The timeline vocabulary, so no client hardcodes it."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        unavailable = set(services.unavailable_types())
        return Response([
            {"value": event_type, "label": timeline.label(event_type),
             # Honest about what the platform cannot yet produce, rather than
             # offering a filter that can only ever return nothing.
             "available": event_type not in unavailable}
            for event_type in timeline.ALL_TYPES
        ], status=status.HTTP_200_OK)


class MyMedicalRecord(_RecordsView):
    """The caller's own record overview."""

    def get(self, request):
        patient, record = services.open_record(request.user)
        return Response(services.overview(request.user, patient, record),
                        status=status.HTTP_200_OK)


class MyTimeline(_RecordsView):
    """The caller's own medical timeline, filtered and paginated."""

    def get(self, request):
        patient, _record = services.open_record(request.user)
        return _timeline_response(request, request.user, patient)


class PatientMedicalRecord(_RecordsView):
    """One patient's record, for a doctor who treats them."""
    permission_classes = [IsDoctor]

    def get(self, request, patient_id):
        patient, record = services.open_record(request.user, patient_id)
        return Response(services.overview(request.user, patient, record),
                        status=status.HTTP_200_OK)


class PatientTimeline(_RecordsView):
    permission_classes = [IsDoctor]

    def get(self, request, patient_id):
        patient, _record = services.open_record(request.user, patient_id)
        return _timeline_response(request, request.user, patient)


class MyPatients(_RecordsView):
    """Patients whose records this doctor may open."""
    permission_classes = [IsDoctor]

    def get(self, request):
        return Response([
            {"id": patient.id,
             "name": patient.get_full_name() or patient.username,
             "username": patient.username}
            for patient in services.patients_for(request.user)
        ], status=status.HTTP_200_OK)


def _timeline_response(request, viewer, patient):
    try:
        limit, offset = _paging(request)
        types = _types(request)
        since = _parse_date(request.query_params.get("from"))
        until = _parse_date(request.query_params.get("to"), end_of_day=True)
    except ValueError as exc:
        return api_error(str(exc), status.HTTP_400_BAD_REQUEST)

    return Response(
        services.timeline_for(viewer, patient, types=types, since=since,
                              until=until,
                              search=request.query_params.get("q", ""),
                              limit=limit, offset=offset),
        status=status.HTTP_200_OK)
