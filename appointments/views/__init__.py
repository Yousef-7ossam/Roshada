"""HTTP adapter layer.

Views are intentionally thin: they validate/parse input, delegate to a service,
and translate the result (or a domain exception) into an HTTP response. This
package re-exports every view so existing imports (``from .views import X``)
and URL wiring remain unchanged.

Authentication views live in the ``accounts`` app, which owns roles, profiles
and permissions; the paths they serve are unchanged.
"""
from .scheduling import (
    DoctorList, ProviderList, ProviderServices, ProviderAvailability,
    AvailableSlots, AvailabilitySearch, MyServices, MyServiceDetail,
    MyAvailability, MyAvailabilityDetail, MyTimeOff,
    CreateAppointment, MyAppointments, DoctorAppointments, ProviderAppointments,
    CancelAppointment, RescheduleAppointment, AppointmentOutcome,
)
from .ocr import OCRExtractID
from .health import HealthCheck
from .chat import ChatHistory, ChatExchange, ChatContext, ChatAsk, ChatStatus
from .dashboard import DashboardSummary

__all__ = [
    "DoctorList", "ProviderList", "ProviderServices", "ProviderAvailability",
    "AvailableSlots", "AvailabilitySearch", "MyServices", "MyServiceDetail",
    "MyAvailability", "MyAvailabilityDetail", "MyTimeOff",
    "CreateAppointment", "MyAppointments", "DoctorAppointments",
    "ProviderAppointments",
    "CancelAppointment", "RescheduleAppointment", "AppointmentOutcome",
    "OCRExtractID", "HealthCheck",
    "ChatHistory", "ChatExchange", "ChatContext", "ChatAsk", "ChatStatus",
    "DashboardSummary",
]
