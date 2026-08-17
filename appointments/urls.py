from django.urls import path

from .views import (
    DoctorList, ProviderList, ProviderServices, ProviderAvailability,
    AvailableSlots, AvailabilitySearch, MyServices, MyServiceDetail,
    MyAvailability, MyAvailabilityDetail, MyTimeOff,
    CreateAppointment, MyAppointments, DoctorAppointments, ProviderAppointments,
    CancelAppointment, RescheduleAppointment, AppointmentOutcome,
    OCRExtractID, HealthCheck,
    ChatHistory, ChatExchange, ChatContext, ChatAsk, ChatStatus,
    DashboardSummary,
)

app_name = "appointments"

# Authentication (signup / login / logout / profile) is routed by
# ``accounts.urls`` under the same /api/ prefix.
urlpatterns = [
    # ---- Health / readiness (unauthenticated, un-throttled) ----
    path("health/", HealthCheck.as_view(), name="health"),

    # ---- Dashboard ----
    path("dashboard/summary/", DashboardSummary.as_view(), name="dashboard-summary"),

    # ---- Providers, services and availability (the scheduling engine) ----
    path("doctors/", DoctorList.as_view(), name="doctor-list"),
    path("providers/", ProviderList.as_view(), name="provider-list"),
    path("providers/<int:provider_id>/services/",
         ProviderServices.as_view(), name="provider-services"),
    path("providers/<int:provider_id>/availability/",
         ProviderAvailability.as_view(), name="provider-availability"),
    path("slots/", AvailableSlots.as_view(), name="available-slots"),
    path("availability/search/", AvailabilitySearch.as_view(),
         name="availability-search"),

    # ---- Provider self-management (always the caller's own schedule) ----
    path("me/services/", MyServices.as_view(), name="my-services"),
    path("me/services/<int:service_id>/", MyServiceDetail.as_view(),
         name="my-service-detail"),
    path("me/availability/", MyAvailability.as_view(), name="my-availability"),
    path("me/availability/<int:rule_id>/", MyAvailabilityDetail.as_view(),
         name="my-availability-detail"),
    path("me/time-off/", MyTimeOff.as_view(), name="my-time-off"),

    # ---- Appointments ----
    path("appointment/create/", CreateAppointment.as_view(), name="appointment-create"),
    path("appointments/mine/", MyAppointments.as_view(), name="appointments-mine"),
    path("appointments/provider/", ProviderAppointments.as_view(),
         name="appointments-provider"),
    # The doctor-only alias, kept so existing clients keep working.
    path("appointments/doctor/", DoctorAppointments.as_view(), name="appointments-doctor"),
    path("appointments/<int:appointment_id>/cancel/",
         CancelAppointment.as_view(), name="appointment-cancel"),
    path("appointments/<int:appointment_id>/reschedule/",
         RescheduleAppointment.as_view(), name="appointment-reschedule"),
    path("appointments/<int:appointment_id>/outcome/",
         AppointmentOutcome.as_view(), name="appointment-outcome"),

    # ---- AI assistant ----
    path("chat/ask/", ChatAsk.as_view(), name="chat-ask"),
    path("chat/status/", ChatStatus.as_view(), name="chat-status"),

    # ---- AI assistant chat history (per user) ----
    path("chat/history/", ChatHistory.as_view(), name="chat-history"),
    path("chat/messages/", ChatExchange.as_view(), name="chat-messages"),
    path("chat/context/", ChatContext.as_view(), name="chat-context"),

    # ---- OCR ----
    path("ocr/extract-id/", OCRExtractID.as_view(), name="ocr-extract-id"),
]
