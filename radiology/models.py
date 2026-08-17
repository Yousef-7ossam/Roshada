"""The Radiology domain: orders, examinations, files and reports.

**What this module deliberately does not contain.** There is no radiology
centre model — a centre is a user with the ``radiology`` role and an
``accounts.RadiologyProfile``. There is no imaging-service model —
an imaging service is an ``appointments.Service`` whose ``category`` is a
modality. There is no radiology appointment and no radiology slot — booking,
availability, rescheduling and double-booking protection all belong to the
unified appointment engine, and this module attaches to its result.

What is genuinely new is the clinical workflow the engine knows nothing about:

    ImagingOrder  ->  Examination  ->  ImagingFile
                            |
                            +------->  RadiologyReport

The dependency points one way, radiology -> appointments. The engine has no
import of this module; it calls out through the callback registry in
``appointments.services.scheduling``, which radiology registers at startup.
"""
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

from appointments.models import Appointment, Service

from . import modalities


def imaging_upload_path(instance, filename):
    """Where an uploaded study is written under MEDIA_ROOT.

    Partitioned by examination so one study's files stay together, and the path
    contains no patient name — a directory listing must not itself be a
    disclosure.
    """
    return f"imaging/examination_{instance.examination_id}/{filename}"


class ImagingOrder(models.Model):
    """A doctor's clinical request for a study.

    The order records *what* was asked for and *why*, not where it will happen:
    a doctor orders "MRI Brain", and the patient chooses a centre that offers
    MRI when they book. That is why the order carries ``modality`` and
    ``study_name`` rather than a ``Service`` foreign key — a Service belongs to
    one centre, so referencing one here would silently make the doctor pick the
    centre too.

    There is deliberately **no appointment column**. The booking hangs off the
    examination; a second link here would be a second answer to "when is this
    happening" that could disagree.
    """

    ORDERED = "ordered"
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    REPORT_PENDING = "report_pending"
    REPORTED = "reported"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (ORDERED, "Ordered"),
        (SCHEDULED, "Scheduled"),
        (IN_PROGRESS, "In progress"),
        (REPORT_PENDING, "Report pending"),
        (REPORTED, "Reported"),
        (CANCELLED, "Cancelled"),
    ]
    #: States from which a patient may still book (or re-book) the study.
    BOOKABLE_STATUSES = (ORDERED,)

    patient = models.ForeignKey(User, on_delete=models.CASCADE,
                                related_name="imaging_orders")
    #: The requesting doctor. Kept on delete so the clinical record survives a
    #: departing clinician; the order is still the patient's.
    doctor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                               blank=True, related_name="ordered_imaging")
    modality = models.CharField(max_length=20, choices=modalities.CHOICES)
    #: What was requested, e.g. "MRI Brain with contrast".
    study_name = models.CharField(max_length=200)
    #: Why. The clinical justification a radiologist reads before reporting.
    clinical_indication = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default=ORDERED)
    cancellation_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["patient", "-created_at"]),
            models.Index(fields=["doctor", "-created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["modality"]),
        ]

    def __str__(self):
        return f"{self.study_name} for {self.patient.username} [{self.status}]"

    @property
    def is_bookable(self):
        return self.status in self.BOOKABLE_STATUSES

    @property
    def is_self_requested(self):
        """No doctor: the patient booked imaging themselves.

        Kept distinguishable on purpose — presenting a self-booked study as a
        doctor's clinical order would be inventing a referral.
        """
        return self.doctor_id is None

    @property
    def modality_label(self):
        return modalities.label(self.modality)


class Examination(models.Model):
    """The imaging procedure itself: check-in, acquisition, completion.

    Separate from the appointment because they answer different questions. The
    appointment is a reserved period and can be cancelled without anything
    happening; the examination is what the centre actually did, and it is what
    files and a report attach to.

    Patient, centre and service are reached *through* the appointment and order
    rather than copied here, so there is exactly one answer to each.
    """

    SCHEDULED = "scheduled"
    CHECKED_IN = "checked_in"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (SCHEDULED, "Scheduled"),
        (CHECKED_IN, "Checked in"),
        (IN_PROGRESS, "In progress"),
        (COMPLETED, "Completed"),
        (CANCELLED, "Cancelled"),
    ]

    #: The only transitions the workflow permits. Enforced in the service layer
    #: so a status can never be set to an arbitrary value from a request body.
    TRANSITIONS = {
        SCHEDULED: (CHECKED_IN, CANCELLED),
        CHECKED_IN: (IN_PROGRESS, CANCELLED),
        IN_PROGRESS: (COMPLETED, CANCELLED),
        COMPLETED: (),
        CANCELLED: (),
    }

    #: One examination per booking. Non-null: an examination that is not going
    #: to happen at a specific time is not an examination.
    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE,
                                       related_name="examination")
    #: Null for a self-service booking. a patient may
    #: book imaging without a referral, and that must not be dressed up as one.
    order = models.ForeignKey(ImagingOrder, on_delete=models.SET_NULL,
                              null=True, blank=True,
                              related_name="examinations")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default=SCHEDULED)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    #: The centre user who performed it, recorded when the study completes.
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                     blank=True,
                                     related_name="performed_examinations")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["order"]),
        ]

    def __str__(self):
        return f"Examination {self.pk} [{self.status}]"

    # -- Derived, never stored twice ----------------------------------------
    @property
    def patient(self):
        return self.appointment.patient

    @property
    def center(self):
        """The radiology centre's user — the appointment's provider."""
        return self.appointment.provider

    @property
    def service(self):
        return self.appointment.service

    @property
    def scheduled_for(self):
        return self.appointment.start_at

    def can_transition_to(self, status):
        return status in self.TRANSITIONS.get(self.status, ())


class ImagingFile(models.Model):
    """One stored image or supporting document for an examination.

    **The bytes live on disk; PostgreSQL holds the reference and the metadata.**
    Nothing serves these files statically — ``MEDIA_URL`` is deliberately not
    routed — so the only way to read one is the endpoint that first checks the
    caller's relationship to the study.

    The DICOM identifiers are recorded when a source provides them and are
    otherwise blank. They exist so a future PACS integration has somewhere to
    put them without a migration; nothing in this phase reads them.
    """

    IMAGE = "image"
    DOCUMENT = "document"
    KIND_CHOICES = [(IMAGE, "Image"), (DOCUMENT, "Document")]

    examination = models.ForeignKey(Examination, on_delete=models.CASCADE,
                                    related_name="files")
    file = models.FileField(upload_to=imaging_upload_path)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default=IMAGE)
    #: The name the uploader's filesystem used. Kept separately because the
    #: stored name may be made unique by the storage backend.
    original_name = models.CharField(max_length=255, blank=True, default="")
    content_type = models.CharField(max_length=100, blank=True, default="")
    size_bytes = models.PositiveIntegerField(default=0)
    description = models.CharField(max_length=200, blank=True, default="")

    # ---- DICOM readiness. Declared, unread, not implemented in this phase. ----
    modality_code = models.CharField(max_length=16, blank=True, default="")
    study_uid = models.CharField(max_length=128, blank=True, default="")
    series_uid = models.CharField(max_length=128, blank=True, default="")
    instance_uid = models.CharField(max_length=128, blank=True, default="")

    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name="uploaded_imaging")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["uploaded_at"]
        indexes = [
            models.Index(fields=["examination", "uploaded_at"]),
            models.Index(fields=["study_uid"]),
        ]

    def __str__(self):
        return self.original_name or f"file {self.pk}"


class RadiologyReport(models.Model):
    """The radiologist's read of an examination.

    ``findings`` and ``impression`` are kept as separate fields rather than one
    blob: they are the two halves a clinician actually distinguishes, and
    keeping them apart is what will let a future AI layer explain an impression
    without being handed the whole document as undifferentiated text.

    A patient sees a report only once it is ``released``. That gate is enforced
    in the queryset, not in the template.
    """

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    VERIFIED = "verified"
    RELEASED = "released"
    CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (DRAFT, "Draft"),
        (PENDING_REVIEW, "Pending review"),
        (VERIFIED, "Verified"),
        (RELEASED, "Released"),
        (CANCELLED, "Cancelled"),
    ]
    #: The only status a patient may read.
    PATIENT_VISIBLE_STATUSES = (RELEASED,)

    TRANSITIONS = {
        DRAFT: (PENDING_REVIEW, CANCELLED),
        PENDING_REVIEW: (VERIFIED, DRAFT, CANCELLED),
        VERIFIED: (RELEASED, DRAFT, CANCELLED),
        RELEASED: (),
        CANCELLED: (),
    }

    examination = models.OneToOneField(Examination, on_delete=models.CASCADE,
                                       related_name="report")
    #: The centre user who wrote it. A centre is one account today; per-user
    #: radiologist identities are a future extension of the role system.
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                               blank=True, related_name="authored_reports")
    findings = models.TextField(blank=True, default="")
    impression = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default=DRAFT)
    report_date = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name="verified_reports")
    verified_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["-released_at"]),
        ]

    def __str__(self):
        return f"Report for examination {self.examination_id} [{self.status}]"

    @property
    def is_visible_to_patient(self):
        return self.status in self.PATIENT_VISIBLE_STATUSES

    def can_transition_to(self, status):
        return status in self.TRANSITIONS.get(self.status, ())

    def touch_report_date(self):
        if self.report_date is None:
            self.report_date = timezone.now()


#: Imaging services are ordinary appointment services whose category is a
#: modality. Exposed here so radiology code never has to know that detail.
def imaging_services(center=None):
    queryset = Service.objects.filter(category__in=modalities.ALL,
                                      is_active=True)
    return queryset.filter(provider=center) if center is not None else queryset
