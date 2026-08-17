"""Authorization primitives.

Two kinds of check, and the distinction matters:

* :class:`HasRole` — "is the caller this kind of user?" Used where a whole
  endpoint belongs to one portal.
* :class:`HasCapability` — "is the caller allowed to do this?" Preferred, because
  the answer lives in :mod:`accounts.roles` and widening it later is one line
  there rather than an edit to every view that named a role inline.

Neither is sufficient on its own. Authorization also means *which rows* a caller
may see, and that is enforced by scoping querysets to ``request.user`` in the
service layer — a role check alone would still let one patient read another
patient's records by changing an id in the URL.
"""
from rest_framework.permissions import BasePermission

from . import roles
from .services import role_of


class _AuthenticatedCheck(BasePermission):
    """Shared guard: every check below implies authentication."""

    def _user(self, request):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return None
        return user


class HasRole(_AuthenticatedCheck):
    """Grant access to an explicit set of roles.

    Subclass and set ``allowed_roles``, or build one inline with
    ``HasRole.of(roles.PHARMACY)``.
    """

    allowed_roles = ()
    message = "Your account type cannot access this resource."

    def has_permission(self, request, view):
        user = self._user(request)
        return bool(user) and role_of(user) in self.allowed_roles

    @classmethod
    def of(cls, *allowed, message=None):
        """Build a permission class for ``allowed`` roles."""
        label = "".join(role.title() for role in allowed)
        return type(f"Is{label}", (cls,), {
            "allowed_roles": tuple(allowed),
            "message": message or cls.message,
        })


class HasCapability(_AuthenticatedCheck):
    """Grant access to any role holding ``capability``."""

    capability = None
    message = "Your account is not permitted to perform this action."

    def has_permission(self, request, view):
        user = self._user(request)
        if not user:
            return False
        if self.capability is None:      # misconfiguration, not a grant
            return False
        return roles.has_capability(role_of(user), self.capability)

    @classmethod
    def of(cls, capability, message=None):
        return type("Can" + capability.replace(".", "_").title(), (cls,), {
            "capability": capability,
            "message": message or cls.message,
        })


# ---------------------------------------------------------------------------
# The concrete classes the views use
# ---------------------------------------------------------------------------
IsPatient = HasRole.of(roles.PATIENT,
                       message="Only patient accounts can access this resource.")
IsDoctor = HasRole.of(roles.DOCTOR,
                      message="Only doctor accounts can access this resource.")
IsLaboratory = HasRole.of(roles.LABORATORY,
                          message="Only laboratory accounts can access this resource.")
IsRadiology = HasRole.of(roles.RADIOLOGY,
                         message="Only radiology accounts can access this resource.")
IsPharmacy = HasRole.of(roles.PHARMACY,
                        message="Only pharmacy accounts can access this resource.")
IsPlatformAdmin = HasRole.of(roles.ADMIN,
                             message="Administrator access is required.")

#: Any facility operator (laboratory, radiology centre or pharmacy).
IsFacility = HasRole.of(*roles.FACILITY_ROLES,
                        message="Only provider accounts can access this resource.")

#: Any role that owns a bookable schedule. Patients book; these three are
#: booked, and each sees only its own queue because every provider view is
#: scoped to request.user rather than to an id in the request.
IsProvider = HasRole.of(*roles.BOOKABLE_ROLES,
                        message="Only provider accounts can manage a schedule.")

#: The two roles that participate in a consultation. Used by endpoints that are
#: scoped to the caller's own clinical record and would return an empty list to
#: anybody else: a pharmacy has no appointment history, so answering 200 with []
#: would contradict the published permission matrix for no benefit.
IsClinicalRole = HasRole.of(
    roles.PATIENT, roles.DOCTOR,
    message="Only patient and doctor accounts can access clinical records.")

#: The AI surfaces. A role check rather than a bare ``IsAuthenticated``: the
#: assistant reads the caller's clinical context to answer, so it must only be
#: reachable by the roles whose own record that is.
CanUseAI = HasCapability.of(
    roles.AI_ASSISTANT,
    message="The AI assistant is available to patient and doctor accounts.")
