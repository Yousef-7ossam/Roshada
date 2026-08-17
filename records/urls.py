"""Unified Medical Record routes.

Included under the same ``/api/`` prefix as the rest of the platform, following
the existing convention: nouns, trailing slashes, and the caller's own resource
reachable without an id in the URL — the same shape ``/api/me/services/`` and
``/api/appointments/mine/`` already use, and the reason a patient never has to
put their own id in a path that could be edited.
"""
from django.urls import path

from .views import (
    EventTypes, MyMedicalRecord, MyPatients, MyTimeline, PatientMedicalRecord,
    PatientTimeline,
)

app_name = "records"

urlpatterns = [
    # ---- Vocabulary ----
    path("records/types/", EventTypes.as_view(), name="types"),

    # ---- The caller's own record ----
    path("records/me/", MyMedicalRecord.as_view(), name="my-record"),
    path("records/me/timeline/", MyTimeline.as_view(), name="my-timeline"),

    # ---- A patient's record, for a doctor who treats them ----
    path("records/patients/", MyPatients.as_view(), name="my-patients"),
    path("records/patients/<int:patient_id>/", PatientMedicalRecord.as_view(),
         name="patient-record"),
    path("records/patients/<int:patient_id>/timeline/",
         PatientTimeline.as_view(), name="patient-timeline"),
]
