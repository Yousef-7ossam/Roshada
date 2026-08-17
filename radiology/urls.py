"""Radiology routes.

Included under the same ``/api/`` prefix as the rest of the platform, following
the existing convention: nouns, trailing slashes, and actions as a sub-path
(``.../cancel/``) rather than a verb query parameter.
"""
from django.urls import path

from .views import (
    BookImagingOrder, CancelImagingOrder, CentersOffering, ExaminationDetail,
    ExaminationFiles, ExaminationReport, ExaminationStatus, Examinations,
    ImagingFileDownload, ImagingOrderDetail, ImagingOrders, Modalities,
    ReportStatus, Reports, SelfBookImaging,
)

app_name = "radiology"

urlpatterns = [
    # ---- Discovery ----
    path("radiology/modalities/", Modalities.as_view(), name="modalities"),
    path("radiology/centers/", CentersOffering.as_view(), name="centers"),

    # ---- Imaging orders ----
    path("radiology/orders/", ImagingOrders.as_view(), name="orders"),
    path("radiology/orders/<int:order_id>/", ImagingOrderDetail.as_view(),
         name="order-detail"),
    path("radiology/orders/<int:order_id>/cancel/", CancelImagingOrder.as_view(),
         name="order-cancel"),
    path("radiology/orders/<int:order_id>/book/", BookImagingOrder.as_view(),
         name="order-book"),
    # A booking with no referral. Separate endpoint because it is a different
    # clinical event, not a variant of fulfilling a doctor's order.
    path("radiology/book/", SelfBookImaging.as_view(), name="self-book"),

    # ---- Examinations ----
    path("radiology/examinations/", Examinations.as_view(), name="examinations"),
    path("radiology/examinations/<int:examination_id>/",
         ExaminationDetail.as_view(), name="examination-detail"),
    path("radiology/examinations/<int:examination_id>/status/",
         ExaminationStatus.as_view(), name="examination-status"),

    # ---- Files (metadata list + upload; bytes via the download route) ----
    path("radiology/examinations/<int:examination_id>/files/",
         ExaminationFiles.as_view(), name="examination-files"),
    path("radiology/files/<int:file_id>/download/",
         ImagingFileDownload.as_view(), name="file-download"),

    # ---- Reports ----
    path("radiology/reports/", Reports.as_view(), name="reports"),
    path("radiology/examinations/<int:examination_id>/report/",
         ExaminationReport.as_view(), name="examination-report"),
    path("radiology/reports/<int:report_id>/status/", ReportStatus.as_view(),
         name="report-status"),
]
