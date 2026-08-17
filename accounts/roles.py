"""The six Roshada roles, and what each of them may do.

This module is the single source of truth for the role vocabulary and the
permission matrix. It deliberately contains no Django imports, so it can be read
by the API layer, the test suite and the admin "Permissions" screen without any
of them re-deriving the rules — a matrix that lives in three places is a matrix
that disagrees with itself.

Roles are *what a user is*; capabilities are *what that role may do*. Views ask
for a capability rather than naming roles inline, so widening a permission is a
one-line change here instead of a search across the view layer.
"""

PATIENT = "patient"
DOCTOR = "doctor"
LABORATORY = "laboratory"
RADIOLOGY = "radiology"
PHARMACY = "pharmacy"
ADMIN = "admin"

#: Declaration order is display order (patient-facing roles first, admin last).
ALL_ROLES = (PATIENT, DOCTOR, LABORATORY, RADIOLOGY, PHARMACY, ADMIN)

LABELS = {
    PATIENT: "Patient",
    DOCTOR: "Doctor",
    LABORATORY: "Laboratory",
    RADIOLOGY: "Radiology Center",
    PHARMACY: "Pharmacy",
    ADMIN: "Administrator",
}

CHOICES = [(role, LABELS[role]) for role in ALL_ROLES]

#: Roles that may be created through public registration. ``ADMIN`` is absent by
#: design: an account that can read the whole platform must not be obtainable by
#: anyone who can reach the signup form. Administrators are created through
#: Django's own ``createsuperuser`` / admin site.
SELF_SERVICE_ROLES = (PATIENT, DOCTOR, LABORATORY, RADIOLOGY, PHARMACY)

#: Roles that operate a facility rather than being an individual. They share one
#: profile shape (see :class:`accounts.models.FacilityProfile`).
FACILITY_ROLES = (LABORATORY, RADIOLOGY, PHARMACY)

#: Roles whose portal includes an AI surface. Everything else is refused at the
#: API, not merely hidden in the navigation.
AI_ROLES = (PATIENT, DOCTOR)

#: Roles that own a schedule and can be booked by a patient. Pharmacy is
#: deliberately absent: dispensing is not an appointment, and whether it ever
#: becomes one is its own decision, not a side effect of this list.
BOOKABLE_ROLES = (DOCTOR, LABORATORY, RADIOLOGY)


# ---------------------------------------------------------------------------
# Capabilities
#
# Each constant is the permission a view asks for. Capabilities in
# PLANNED_CAPABILITIES have no endpoint yet — the Lab/Radiology/Pharmacy domains
# are a later task — but they are declared here so the matrix describes the
# platform rather than only the parts already built. The admin Permissions
# screen renders them as "planned" so nobody mistakes a design note for a
# shipped guarantee.
# ---------------------------------------------------------------------------
PROFILE_MANAGE = "profile.manage"
APPOINTMENTS_BOOK = "appointments.book"
APPOINTMENTS_SCHEDULE = "appointments.schedule"
AI_ASSISTANT = "ai.assistant"
ADMIN_PLATFORM = "admin.platform"

# Declared now, enforced when the domain lands.
LAB_ORDERS = "lab.orders"
LAB_RESULTS = "lab.results"
RADIOLOGY_ORDERS = "radiology.orders"
RADIOLOGY_REPORTS = "radiology.reports"
PHARMACY_PRESCRIPTIONS = "pharmacy.prescriptions"
PHARMACY_INVENTORY = "pharmacy.inventory"
MEDICAL_RECORDS_OWN = "records.own"
MEDICAL_RECORDS_ASSIGNED = "records.assigned"

#: Capabilities leave this set as their domain lands: radiology's two went when
#: the Radiology module shipped, pharmacy's two when the Pharmacy module did.
#: Laboratory remains declared-but-unbuilt.
PLANNED_CAPABILITIES = frozenset({
    LAB_ORDERS, LAB_RESULTS,
})

#: role -> the capabilities that role holds.
#:
#: Note what is *absent*: no role except ADMIN carries ADMIN_PLATFORM, and no
#: facility role carries an AI capability. A capability is granted by listing it,
#: never by defaulting.
PERMISSIONS = {
    PATIENT: frozenset({
        PROFILE_MANAGE, APPOINTMENTS_BOOK, AI_ASSISTANT,
        MEDICAL_RECORDS_OWN,
    }),
    DOCTOR: frozenset({
        PROFILE_MANAGE, APPOINTMENTS_SCHEDULE, AI_ASSISTANT,
        MEDICAL_RECORDS_ASSIGNED, LAB_ORDERS, RADIOLOGY_ORDERS,
        # Doctors write prescriptions; pharmacies fill them. The same shape as
        # LAB_ORDERS and RADIOLOGY_ORDERS, which both sides also hold.
        PHARMACY_PRESCRIPTIONS,
    }),
    LABORATORY: frozenset({
        PROFILE_MANAGE, LAB_ORDERS, LAB_RESULTS,
    }),
    RADIOLOGY: frozenset({
        PROFILE_MANAGE, RADIOLOGY_ORDERS, RADIOLOGY_REPORTS,
    }),
    PHARMACY: frozenset({
        PROFILE_MANAGE, PHARMACY_PRESCRIPTIONS, PHARMACY_INVENTORY,
    }),
    ADMIN: frozenset({
        PROFILE_MANAGE, ADMIN_PLATFORM,
    }),
}

#: Every capability any role holds — used by the tests to catch a typo'd
#: constant, which would otherwise silently grant nothing.
ALL_CAPABILITIES = frozenset().union(*PERMISSIONS.values())


def label(role):
    return LABELS.get(role, (role or "").title())


def capabilities(role):
    """The capability set for a role. Unknown roles hold nothing."""
    return PERMISSIONS.get(role, frozenset())


def has_capability(role, capability):
    return capability in capabilities(role)


def is_facility(role):
    return role in FACILITY_ROLES
