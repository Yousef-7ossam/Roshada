"""Django admin registrations.

Administrators are created and managed through Django's own admin — the task's
"existing secure administrative mechanism" — so the role and facility records
are registered here rather than being reachable only through the API. Nothing
clinical is exposed: these are account and facility records, not medical data.
"""
from django.contrib import admin

from .models import (
    LaboratoryProfile, PharmacyProfile, RadiologyProfile, UserAccount,
)


@admin.register(UserAccount)
class UserAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "status", "created_at")
    list_filter = ("role", "status")
    search_fields = ("user__username", "user__first_name", "user__email")
    readonly_fields = ("created_at", "updated_at")
    autocomplete_fields = ()


class _FacilityAdmin(admin.ModelAdmin):
    """Shared admin for the three facility profiles.

    ``verified`` is the switch an administrator actually operates, so it is in
    the list view and editable there — verifying a batch of facilities should
    not mean opening a form per row.
    """
    list_display = ("name", "user", "phone", "available", "verified")
    list_filter = ("verified", "available")
    list_editable = ("verified",)
    search_fields = ("name", "user__username", "phone", "email")
    readonly_fields = ("created_at", "updated_at")


@admin.register(LaboratoryProfile)
class LaboratoryProfileAdmin(_FacilityAdmin):
    pass


@admin.register(RadiologyProfile)
class RadiologyProfileAdmin(_FacilityAdmin):
    pass


@admin.register(PharmacyProfile)
class PharmacyProfileAdmin(_FacilityAdmin):
    pass
