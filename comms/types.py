"""The notification vocabulary, and what each kind is *about*.

One list, shared by every module that raises a notification, by the API and by
the frontend filter. A type string invented at a call site is a notification
nobody can filter for, so this module is the only place they are declared.

Zero Django imports, deliberately — the same shape as ``accounts.roles`` and
``radiology.modalities``, so tests and the API can read the vocabulary without
importing the clinical modules that produce it.
"""

# ---- Appointments ---------------------------------------------------------
APPOINTMENT_CREATED = "appointment_created"
APPOINTMENT_CONFIRMED = "appointment_confirmed"
APPOINTMENT_CANCELLED = "appointment_cancelled"
APPOINTMENT_RESCHEDULED = "appointment_rescheduled"
APPOINTMENT_REMINDER = "appointment_reminder"
APPOINTMENT_COMPLETED = "appointment_completed"

# ---- Clinical -------------------------------------------------------------
PRESCRIPTION_CREATED = "prescription_created"
PRESCRIPTION_UPDATED = "prescription_updated"
LAB_RESULT_RELEASED = "lab_result_released"
RADIOLOGY_REPORT_RELEASED = "radiology_report_released"
IMAGING_ORDER_CREATED = "imaging_order_created"

# ---- Pharmacy -------------------------------------------------------------
PHARMACY_REQUEST_CREATED = "pharmacy_request_created"
PHARMACY_REQUEST_CONFIRMED = "pharmacy_request_confirmed"
PHARMACY_REQUEST_REJECTED = "pharmacy_request_rejected"
PHARMACY_ORDER_PREPARING = "pharmacy_order_preparing"
PHARMACY_ORDER_READY = "pharmacy_order_ready"
PHARMACY_ORDER_COMPLETED = "pharmacy_order_completed"
PHARMACY_REQUEST_CANCELLED = "pharmacy_request_cancelled"

# ---- Communication --------------------------------------------------------
MESSAGE_RECEIVED = "message_received"

# ---- Platform -------------------------------------------------------------
SYSTEM_NOTIFICATION = "system_notification"

LABELS = {
    APPOINTMENT_CREATED: "Appointment booked",
    APPOINTMENT_CONFIRMED: "Appointment confirmed",
    APPOINTMENT_CANCELLED: "Appointment cancelled",
    APPOINTMENT_RESCHEDULED: "Appointment rescheduled",
    APPOINTMENT_REMINDER: "Appointment reminder",
    APPOINTMENT_COMPLETED: "Visit completed",
    PRESCRIPTION_CREATED: "New prescription",
    PRESCRIPTION_UPDATED: "Prescription updated",
    LAB_RESULT_RELEASED: "Laboratory result available",
    RADIOLOGY_REPORT_RELEASED: "Radiology report available",
    IMAGING_ORDER_CREATED: "Imaging requested",
    PHARMACY_REQUEST_CREATED: "New medication request",
    PHARMACY_REQUEST_CONFIRMED: "Medication request confirmed",
    PHARMACY_REQUEST_REJECTED: "Medication request rejected",
    PHARMACY_ORDER_PREPARING: "Medication being prepared",
    PHARMACY_ORDER_READY: "Medication ready for pickup",
    PHARMACY_ORDER_COMPLETED: "Medication collected",
    PHARMACY_REQUEST_CANCELLED: "Medication request cancelled",
    MESSAGE_RECEIVED: "New message",
    SYSTEM_NOTIFICATION: "Roshada",
}

ALL = tuple(LABELS)
CHOICES = [(value, LABELS[value]) for value in ALL]

# ---------------------------------------------------------------------------
# Categories — the filter the notification centre offers.
#
# Grouped rather than one filter per type: a patient wants "anything about my
# medication", not seven checkboxes that each match one event.
# ---------------------------------------------------------------------------
APPOINTMENTS = "appointments"
MEDICAL = "medical"
PHARMACY = "pharmacy"
MESSAGES = "messages"
SYSTEM = "system"

CATEGORY_LABELS = {
    APPOINTMENTS: "Appointments",
    MEDICAL: "Medical",
    PHARMACY: "Pharmacy",
    MESSAGES: "Messages",
    SYSTEM: "System",
}
CATEGORIES = tuple(CATEGORY_LABELS)

CATEGORY_OF = {
    APPOINTMENT_CREATED: APPOINTMENTS,
    APPOINTMENT_CONFIRMED: APPOINTMENTS,
    APPOINTMENT_CANCELLED: APPOINTMENTS,
    APPOINTMENT_RESCHEDULED: APPOINTMENTS,
    APPOINTMENT_REMINDER: APPOINTMENTS,
    APPOINTMENT_COMPLETED: APPOINTMENTS,
    PRESCRIPTION_CREATED: MEDICAL,
    PRESCRIPTION_UPDATED: MEDICAL,
    LAB_RESULT_RELEASED: MEDICAL,
    RADIOLOGY_REPORT_RELEASED: MEDICAL,
    IMAGING_ORDER_CREATED: MEDICAL,
    PHARMACY_REQUEST_CREATED: PHARMACY,
    PHARMACY_REQUEST_CONFIRMED: PHARMACY,
    PHARMACY_REQUEST_REJECTED: PHARMACY,
    PHARMACY_ORDER_PREPARING: PHARMACY,
    PHARMACY_ORDER_READY: PHARMACY,
    PHARMACY_ORDER_COMPLETED: PHARMACY,
    PHARMACY_REQUEST_CANCELLED: PHARMACY,
    MESSAGE_RECEIVED: MESSAGES,
    SYSTEM_NOTIFICATION: SYSTEM,
}

#: Types no module can raise yet. ``LAB_RESULT_RELEASED`` is declared because
#: the vocabulary should describe the platform, but Roshada has no Laboratory
#: module — so it is reported as unavailable rather than offered as a filter
#: that can only ever return nothing. The same honesty the medical record and
#: the dashboards already apply.
UNPRODUCIBLE = frozenset({LAB_RESULT_RELEASED})


def label(notification_type):
    return LABELS.get(notification_type,
                      (notification_type or "").replace("_", " ").capitalize())


def category_of(notification_type):
    return CATEGORY_OF.get(notification_type, SYSTEM)


def category_label(category):
    return CATEGORY_LABELS.get(category, (category or "").title())


def is_valid(notification_type):
    return notification_type in LABELS


def types_in(category):
    return tuple(value for value in ALL if CATEGORY_OF.get(value) == category)
