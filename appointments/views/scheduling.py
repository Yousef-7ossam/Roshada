"""Scheduling endpoints: providers, services, availability, slots, appointments.

One set of endpoints serves all three provider kinds. A laboratory reads its
queue from the same view a doctor does — the view filters on ``request.user``,
so what differs between them is the data, not the code path.
"""
import datetime
import logging

from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import (
    IsClinicalRole, IsDoctor, IsPatient, IsProvider,
)

from ..exceptions import api_error
from ..models import AvailabilityRule, Service, TimeOff
from ..serializers import (
    AppointmentCancelSerializer, AppointmentCreateSerializer,
    AppointmentOutcomeSerializer, AppointmentRescheduleSerializer,
    AppointmentSerializer, AvailabilityRuleSerializer, DoctorSerializer,
    ServiceSerializer, TimeOffSerializer,
)
from ..services import availability, scheduling

logger = logging.getLogger("appointments")


def _requested_date(request, default_to_today=True):
    """The ``?date=`` query parameter, or today."""
    raw = request.query_params.get("date")
    if raw:
        parsed = parse_date(raw)
        if parsed is None:
            return None
        return parsed
    return availability._local_date(availability.now()) if default_to_today else None


class DoctorList(APIView):
    """The doctor directory. Unchanged — still the doctor-only listing."""
    permission_classes = [AllowAny]

    def get(self, request):
        doctors = scheduling.list_available_doctors()
        return Response(DoctorSerializer(doctors, many=True).data,
                        status=status.HTTP_200_OK)


class ProviderList(APIView):
    """Every bookable provider, optionally of one kind.

    Public, like the doctor directory it generalises: choosing who to book with
    happens before there is anything private to protect.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        role = request.query_params.get("type") or request.query_params.get("role")
        if role and role not in scheduling.BOOKABLE_ROLES:
            return api_error(
                f"{role!r} is not a bookable provider type.",
                status.HTTP_400_BAD_REQUEST)
        return Response(scheduling.list_providers(role), status=status.HTTP_200_OK)


class ProviderServices(APIView):
    """The services one provider offers."""
    permission_classes = [AllowAny]

    def get(self, request, provider_id):
        try:
            provider = scheduling.resolve_provider(provider_id=provider_id)
        except scheduling.DoctorNotFound:
            return api_error("Provider not found", status.HTTP_404_NOT_FOUND)
        return Response(
            ServiceSerializer(scheduling.list_services(provider), many=True).data,
            status=status.HTTP_200_OK)


class ProviderAvailability(APIView):
    """A provider's published rules — what days and hours they open."""
    permission_classes = [AllowAny]

    def get(self, request, provider_id):
        try:
            provider = scheduling.resolve_provider(provider_id=provider_id)
        except scheduling.DoctorNotFound:
            return api_error("Provider not found", status.HTTP_404_NOT_FOUND)
        rules = (AvailabilityRule.objects
                 .filter(provider=provider, is_active=True)
                 .select_related('service'))
        return Response(AvailabilityRuleSerializer(rules, many=True).data,
                        status=status.HTTP_200_OK)


class AvailableSlots(APIView):
    """Bookable slots for one provider, one date, optionally one service.

    Requires authentication: the slot grid reveals a provider's booked periods,
    which is scheduling information about real people.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        provider_id = request.query_params.get("provider")
        if not provider_id:
            return api_error("A provider is required.",
                             status.HTTP_400_BAD_REQUEST)
        try:
            provider = scheduling.resolve_provider(provider_id=provider_id)
        except scheduling.DoctorNotFound:
            return api_error("Provider not found", status.HTTP_404_NOT_FOUND)

        date = _requested_date(request)
        if date is None:
            return api_error("Invalid date. Use YYYY-MM-DD.",
                             status.HTTP_400_BAD_REQUEST)

        try:
            service = scheduling.resolve_service(
                provider, request.query_params.get("service"))
        except scheduling.ServiceNotFound:
            return api_error("Service not found for this provider",
                             status.HTTP_404_NOT_FOUND)

        slots = availability.describe_slots(provider, date, service)
        return Response({
            "provider": provider.id,
            "service": service.id if service else None,
            "date": date.isoformat(),
            "publishes_availability": availability.has_rules(provider),
            "slots": slots,
            "available": [s for s in slots if s["available"]],
        }, status=status.HTTP_200_OK)


class AvailabilitySearch(APIView):
    """Find providers with free slots — the patient's 'when can I be seen'."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        role = request.query_params.get("type") or request.query_params.get("role")
        if role and role not in scheduling.BOOKABLE_ROLES:
            return api_error(f"{role!r} is not a bookable provider type.",
                             status.HTTP_400_BAD_REQUEST)
        date = _requested_date(request)
        if date is None:
            return api_error("Invalid date. Use YYYY-MM-DD.",
                             status.HTTP_400_BAD_REQUEST)
        return Response(
            scheduling.search_availability(
                role=role, service_name=request.query_params.get("service"),
                date=date),
            status=status.HTTP_200_OK)


class CreateAppointment(APIView):
    permission_classes = [IsPatient]
    parser_classes = [JSONParser]

    def post(self, request):
        serializer = AppointmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            appointment = scheduling.create_appointment(
                request.user, **serializer.validated_data)
        except scheduling.DoctorNotFound:
            return api_error("Provider not found", status.HTTP_404_NOT_FOUND)
        except scheduling.ServiceNotFound:
            return api_error("Service not found for this provider",
                             status.HTTP_404_NOT_FOUND)
        except scheduling.DoctorNotAvailable:
            return api_error("This provider is not accepting bookings.",
                             status.HTTP_400_BAD_REQUEST)
        except scheduling.SlotTaken:
            # Ordered before OutsideAvailability, which it subclasses: the slot
            # is offered, somebody just got there first. That is a conflict.
            return api_error("That time slot is already taken. Please choose another.",
                             status.HTTP_409_CONFLICT)
        except (scheduling.ProviderUnavailable,
                scheduling.OutsideAvailability) as exc:
            return api_error(str(exc), status.HTTP_400_BAD_REQUEST)
        except scheduling.SlotUnavailable:
            return api_error("That time slot is already taken. Please choose another.",
                             status.HTTP_409_CONFLICT)

        logger.info("Appointment %s booked by %s with %s", appointment.id,
                    request.user.username, appointment.provider.username)
        return Response(AppointmentSerializer(appointment).data,
                        status=status.HTTP_201_CREATED)


class MyAppointments(APIView):
    # Not IsPatient, so a doctor viewing this sees an empty list rather than a
    # 403 — that preserves the existing frontend behaviour. Not bare
    # IsAuthenticated either: a pharmacy has no appointments of its own, and
    # answering it with [] would claim access the permission matrix withholds.
    permission_classes = [IsClinicalRole]

    def get(self, request):
        appointments = scheduling.list_patient_appointments(request.user)
        return Response(AppointmentSerializer(appointments, many=True).data,
                        status=status.HTTP_200_OK)


class ProviderAppointments(APIView):
    """Appointments booked with the authenticated provider.

    Scoped to ``request.user``, so one laboratory can never read another's
    queue — there is no id in the request to tamper with.
    """
    permission_classes = [IsProvider]

    def get(self, request):
        appointments = scheduling.list_provider_appointments(request.user)

        when = request.query_params.get("when")
        if when == "today":
            day = availability._local_date(availability.now())
            appointments = scheduling.provider_appointments_on(request.user, day)
        elif when == "upcoming":
            appointments = scheduling.upcoming_for_provider(request.user)

        return Response(AppointmentSerializer(appointments, many=True).data,
                        status=status.HTTP_200_OK)


class DoctorAppointments(ProviderAppointments):
    """The doctor-only alias, kept so existing clients and tests still work."""
    permission_classes = [IsDoctor]


class CancelAppointment(APIView):
    """Cancel a scheduled appointment. Either party to it may cancel."""
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def post(self, request, appointment_id):
        serializer = AppointmentCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            appointment = scheduling.cancel_appointment(
                request.user, appointment_id, serializer.validated_data["reason"])
        except scheduling.AppointmentNotFound:
            return api_error("Appointment not found", status.HTTP_404_NOT_FOUND)
        except scheduling.InvalidTransition as exc:
            return api_error(str(exc), status.HTTP_409_CONFLICT)

        logger.info("Appointment %s cancelled by %s", appointment.id, request.user.username)
        return Response(AppointmentSerializer(appointment).data,
                        status=status.HTTP_200_OK)


class RescheduleAppointment(APIView):
    """Move a scheduled appointment to another slot with the same provider."""
    permission_classes = [IsAuthenticated]
    parser_classes = [JSONParser]

    def post(self, request, appointment_id):
        serializer = AppointmentRescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            appointment = scheduling.reschedule_appointment(
                request.user, appointment_id, **serializer.validated_data)
        except scheduling.AppointmentNotFound:
            return api_error("Appointment not found", status.HTTP_404_NOT_FOUND)
        except scheduling.InvalidTransition as exc:
            return api_error(str(exc), status.HTTP_409_CONFLICT)
        except scheduling.SlotTaken:
            return api_error("That time slot is already taken. Please choose another.",
                             status.HTTP_409_CONFLICT)
        except (scheduling.ProviderUnavailable,
                scheduling.OutsideAvailability) as exc:
            return api_error(str(exc), status.HTTP_400_BAD_REQUEST)
        except scheduling.SlotUnavailable:
            return api_error("That time slot is already taken. Please choose another.",
                             status.HTTP_409_CONFLICT)

        logger.info("Appointment %s rescheduled by %s", appointment.id, request.user.username)
        return Response(AppointmentSerializer(appointment).data,
                        status=status.HTTP_200_OK)


class AppointmentOutcome(APIView):
    """Provider closes out a visit as completed or no-show."""
    permission_classes = [IsProvider]
    parser_classes = [JSONParser]

    def post(self, request, appointment_id):
        serializer = AppointmentOutcomeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            appointment = scheduling.set_outcome(
                request.user, appointment_id, serializer.validated_data["status"])
        except scheduling.AppointmentNotFound:
            return api_error("Appointment not found", status.HTTP_404_NOT_FOUND)
        except scheduling.InvalidTransition as exc:
            return api_error(str(exc), status.HTTP_409_CONFLICT)

        return Response(AppointmentSerializer(appointment).data,
                        status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Provider self-management
#
# Every view below reads and writes ``request.user``'s own rows only. There is
# no provider id in any of these requests, which is what makes cross-provider
# tampering impossible rather than merely checked.
# ---------------------------------------------------------------------------
class MyServices(APIView):
    """The caller's own service catalogue."""
    permission_classes = [IsProvider]
    parser_classes = [JSONParser]

    def get(self, request):
        services = Service.objects.filter(provider=request.user)
        return Response(ServiceSerializer(services, many=True).data,
                        status=status.HTTP_200_OK)

    def post(self, request):
        serializer = ServiceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if Service.objects.filter(provider=request.user,
                                  name__iexact=serializer.validated_data["name"]
                                  ).exists():
            return api_error("You already offer a service with that name.",
                             status.HTTP_400_BAD_REQUEST)
        service = serializer.save(provider=request.user)
        return Response(ServiceSerializer(service).data,
                        status=status.HTTP_201_CREATED)


class MyServiceDetail(APIView):
    permission_classes = [IsProvider]
    parser_classes = [JSONParser]

    def _own(self, request, service_id):
        return Service.objects.filter(pk=service_id, provider=request.user).first()

    def patch(self, request, service_id):
        service = self._own(request, service_id)
        if service is None:
            return api_error("Service not found", status.HTTP_404_NOT_FOUND)
        serializer = ServiceSerializer(service, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request, service_id):
        """Deactivate rather than delete.

        Appointments reference the service with PROTECT, so a real delete would
        fail the moment anyone had booked it — and erasing what a patient was
        booked for is not something a catalogue edit should do.
        """
        service = self._own(request, service_id)
        if service is None:
            return api_error("Service not found", status.HTTP_404_NOT_FOUND)
        service.is_active = False
        service.save(update_fields=["is_active", "updated_at"])
        return Response({"message": "Service withdrawn", "id": service.id},
                        status=status.HTTP_200_OK)


class MyAvailability(APIView):
    """The caller's own availability rules."""
    permission_classes = [IsProvider]
    parser_classes = [JSONParser]

    def get(self, request):
        rules = (AvailabilityRule.objects.filter(provider=request.user)
                 .select_related('service'))
        return Response(AvailabilityRuleSerializer(rules, many=True).data,
                        status=status.HTTP_200_OK)

    def post(self, request):
        serializer = AvailabilityRuleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        service = serializer.validated_data.get("service")
        if service is not None and service.provider_id != request.user.id:
            return api_error("That service belongs to another provider.",
                             status.HTTP_404_NOT_FOUND)
        rule = serializer.save(provider=request.user)
        return Response(AvailabilityRuleSerializer(rule).data,
                        status=status.HTTP_201_CREATED)


class MyAvailabilityDetail(APIView):
    permission_classes = [IsProvider]
    parser_classes = [JSONParser]

    def delete(self, request, rule_id):
        rule = AvailabilityRule.objects.filter(pk=rule_id,
                                               provider=request.user).first()
        if rule is None:
            return api_error("Availability rule not found",
                             status.HTTP_404_NOT_FOUND)
        rule.delete()
        return Response({"message": "Availability removed", "id": rule_id},
                        status=status.HTTP_200_OK)


class MyTimeOff(APIView):
    """Blocked periods — lunch, maintenance, leave."""
    permission_classes = [IsProvider]
    parser_classes = [JSONParser]

    def get(self, request):
        today = availability._local_date(availability.now())
        entries = TimeOff.objects.filter(provider=request.user,
                                         date__gte=today - datetime.timedelta(days=30))
        return Response(TimeOffSerializer(entries, many=True).data,
                        status=status.HTTP_200_OK)

    def post(self, request):
        serializer = TimeOffSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        entry = serializer.save(provider=request.user)
        return Response(TimeOffSerializer(entry).data,
                        status=status.HTTP_201_CREATED)

    def delete(self, request):
        entry_id = request.query_params.get("id")
        entry = TimeOff.objects.filter(pk=entry_id, provider=request.user).first()
        if entry is None:
            return api_error("Time off not found", status.HTTP_404_NOT_FOUND)
        entry.delete()
        return Response({"message": "Time off removed"}, status=status.HTTP_200_OK)
