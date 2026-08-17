"""Identity: the role attached to a user, and the facility profiles.

**Why an attached model rather than a custom user model.** Roshada already has
26 applied migrations and live data against ``django.contrib.auth.User``.
Swapping ``AUTH_USER_MODEL`` at this point is the one route to "unified
authentication" that cannot preserve that data, so the role lives in a
``OneToOneField`` beside the user instead. Authentication itself is untouched —
there is still exactly one credential store, one token model and one login
endpoint for all six roles.

The layering is:

    User  ->  UserAccount (role, status)  ->  role profile  ->  capabilities

``UserAccount`` is the authoritative answer to "what is this user?". The profile
models carry the domain detail for that role and are looked up *through* the
role, never used to infer it.
"""
from django.contrib.auth.models import User
from django.db import models

from . import roles


class UserAccount(models.Model):
    """The role and lifecycle state of one user.

    Before this model, a user's role was inferred from which profile row
    happened to exist (``hasattr(user, 'doctor_profile')``). That could not
    express an administrator, could not express a suspended account, and
    silently defaulted anyone without a profile to "patient" — a fail-open
    default in an application that stores medical records.
    """

    ACTIVE = "active"
    PENDING = "pending"
    SUSPENDED = "suspended"
    STATUS_CHOICES = [
        (ACTIVE, "Active"),
        (PENDING, "Pending review"),
        (SUSPENDED, "Suspended"),
    ]

    user = models.OneToOneField(User, on_delete=models.CASCADE,
                                related_name="account")
    role = models.CharField(max_length=20, choices=roles.CHOICES)
    #: Only ACTIVE accounts may authenticate; see ``services.login_user``.
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default=ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__username"]
        indexes = [
            models.Index(fields=["role"]),
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"{self.user.username} ({self.role})"

    @property
    def can_authenticate(self):
        return self.status == self.ACTIVE

    @property
    def capabilities(self):
        return roles.capabilities(self.role)

    def has_capability(self, capability):
        return roles.has_capability(self.role, capability)


class FacilityProfile(models.Model):
    """Fields shared by every organisation-style provider.

    Laboratories, radiology centres and pharmacies are the same *kind* of thing
    — a verified place with an address, a service list and opening hours — so
    the shared shape is declared once. They are separate concrete tables rather
    than one table with a type column because their domains diverge from here:
    a lab gains samples and results, a pharmacy gains inventory. Splitting later
    would be a data migration; starting split costs nothing.
    """

    name = models.CharField(max_length=200)
    #: Regulatory licence / registration number. Blank until the operator
    #: supplies it — registration must not be blocked on paperwork.
    license_number = models.CharField(max_length=60, blank=True, default="")
    phone = models.CharField(max_length=30, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    address = models.TextField(blank=True, default="")
    #: Free text for now. It becomes a catalogue relation when the Laboratory /
    #: Radiology / Pharmacy domains land; modelling that relation before those
    #: tables exist would be guessing at their shape.
    services = models.TextField(blank=True, default="")
    operating_hours = models.CharField(max_length=200, blank=True, default="")
    #: Accepting work right now (the facility's own switch).
    available = models.BooleanField(default=True)
    #: Checked by an administrator. False means the facility can sign in and
    #: complete its profile but must not be offered to patients — which is why
    #: this is separate from ``UserAccount.status``.
    verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["name"]

    def __str__(self):
        return self.name


class LaboratoryProfile(FacilityProfile):
    user = models.OneToOneField(User, on_delete=models.CASCADE,
                                related_name="laboratory_profile")

    class Meta(FacilityProfile.Meta):
        abstract = False
        verbose_name = "laboratory profile"


class RadiologyProfile(FacilityProfile):
    user = models.OneToOneField(User, on_delete=models.CASCADE,
                                related_name="radiology_profile")

    class Meta(FacilityProfile.Meta):
        abstract = False
        verbose_name = "radiology center profile"


class PharmacyProfile(FacilityProfile):
    user = models.OneToOneField(User, on_delete=models.CASCADE,
                                related_name="pharmacy_profile")

    class Meta(FacilityProfile.Meta):
        abstract = False
        verbose_name = "pharmacy profile"


#: role -> the model that stores that role's detail. Patient and doctor profiles
#: stay in ``appointments`` (they are referenced by the scheduling engine);
#: they are resolved lazily in ``services`` to keep this module import-light.
FACILITY_PROFILE_MODELS = {
    roles.LABORATORY: LaboratoryProfile,
    roles.RADIOLOGY: RadiologyProfile,
    roles.PHARMACY: PharmacyProfile,
}
