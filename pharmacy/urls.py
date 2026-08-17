"""Pharmacy routes.

Included under the same ``/api/`` prefix as the rest of the platform, following
the existing convention: nouns, trailing slashes, and actions as a sub-path
(``.../status/``) rather than a verb query parameter.

Prescriptions live under this prefix because this is the module that owns them,
the same way a doctor's imaging order lives under ``/api/radiology/orders/``.
"""
from django.urls import path

from .views import (
    DosageForms, Inventory, InventoryDetail, MedicationAvailability,
    MedicationDetail, MedicationRequestDetail, MedicationRequestStatus,
    MedicationRequests, Medications, Pharmacies, PrescribablePatients,
    PrescriptionDetail, PrescriptionPharmacies, PrescriptionStatus,
    Prescriptions,
)

app_name = "pharmacy"

urlpatterns = [
    # ---- Discovery ----
    path("pharmacy/dosage-forms/", DosageForms.as_view(), name="dosage-forms"),
    path("pharmacy/pharmacies/", Pharmacies.as_view(), name="pharmacies"),

    # ---- Medication catalogue ----
    path("pharmacy/medications/", Medications.as_view(), name="medications"),
    path("pharmacy/medications/<int:medication_id>/", MedicationDetail.as_view(),
         name="medication-detail"),

    # ---- Prescriptions ----
    path("pharmacy/prescriptions/", Prescriptions.as_view(),
         name="prescriptions"),
    path("pharmacy/prescriptions/<int:prescription_id>/",
         PrescriptionDetail.as_view(), name="prescription-detail"),
    path("pharmacy/prescriptions/<int:prescription_id>/status/",
         PrescriptionStatus.as_view(), name="prescription-status"),
    # "Find in pharmacies", answered per medication on the prescription.
    path("pharmacy/prescriptions/<int:prescription_id>/pharmacies/",
         PrescriptionPharmacies.as_view(), name="prescription-pharmacies"),
    path("pharmacy/prescribable-patients/", PrescribablePatients.as_view(),
         name="prescribable-patients"),

    # ---- Inventory (the caller's own shelf) ----
    path("pharmacy/inventory/", Inventory.as_view(), name="inventory"),
    path("pharmacy/inventory/<int:line_id>/", InventoryDetail.as_view(),
         name="inventory-detail"),

    # ---- Availability, read from inventory ----
    path("pharmacy/availability/", MedicationAvailability.as_view(),
         name="availability"),

    # ---- Medication requests ----
    path("pharmacy/requests/", MedicationRequests.as_view(), name="requests"),
    path("pharmacy/requests/<int:request_id>/",
         MedicationRequestDetail.as_view(), name="request-detail"),
    path("pharmacy/requests/<int:request_id>/status/",
         MedicationRequestStatus.as_view(), name="request-status"),
]
