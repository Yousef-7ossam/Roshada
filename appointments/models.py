from django.contrib.auth.models import User
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import F, Func, Q, Value
from django.utils import timezone


class Doctor(models.Model):
    """A doctor's professional profile.

    The role itself lives in ``accounts.UserAccount``; this holds the clinical
    detail. ``license_number``, ``phone`` and ``clinic`` are blank by default so
    an existing doctor is never blocked by paperwork they have not supplied yet.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True,
                                related_name='doctor_profile')
    name = models.CharField(max_length=100)
    specialization = models.CharField(max_length=200)
    license_number = models.CharField(max_length=60, blank=True, default="")
    phone = models.CharField(max_length=30, blank=True, default="")
    clinic = models.CharField(max_length=200, blank=True, default="")
    available = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['available']),
        ]

    def __str__(self):
        return self.name


class PatientProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE,
                                related_name='patient_profile')
    age = models.PositiveIntegerField(default=0)
    #: Recorded separately from ``age`` rather than replacing it: ``age`` is a
    #: a required demographic on the patient profile and every existing row has one,
    #: while date of birth is optional and only supplied going forward.
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, blank=True, default="")
    phone = models.CharField(max_length=30, blank=True, default="")
    address = models.TextField(blank=True, default="")
    medical_history = models.TextField(blank=True, default="")

    def __str__(self):
        return self.user.username


class Service(models.Model):
    """Something a provider can be booked for.

    One model covers a doctor's consultation, a laboratory's blood test and a
    radiology centre's MRI, because from the scheduling engine's point of view
    they are the same thing: a named unit of work with a duration, offered by
    one provider. What differs is the vocabulary, and that lives in ``name``.

    ``provider`` is the provider's *user*, not a profile row. Identity already
    lives on the user (``accounts.UserAccount`` holds the role), so pointing at
    a profile would mean a second provider registry that could disagree with the
    first.
    """
    provider = models.ForeignKey(User, on_delete=models.CASCADE,
                                 related_name='services')
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    #: Free-form grouping, set by the provider. Radiology constrains it to an
    #: imaging modality (``radiology.modalities``), which is what lets a
    #: doctor's order for an MRI be matched to the centres that offer one.
    #: Blank for providers that do not group their services.
    category = models.CharField(max_length=40, blank=True, default="")
    #: How long one booking occupies. Drives slot length when this service is
    #: booked, so an MRI can be 60 minutes while an X-ray is 15.
    duration_minutes = models.PositiveIntegerField(default=30)
    #: e.g. "Fast for 8 hours beforehand." Blank for most consultations.
    preparation = models.TextField(blank=True, default="")
    #: Withdrawn services are deactivated, never deleted — see the PROTECT on
    #: Appointment.service. A booked history must keep saying what was booked.
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        constraints = [
            models.UniqueConstraint(fields=['provider', 'name'],
                                    name='unique_service_name_per_provider'),
        ]
        indexes = [
            models.Index(fields=['provider', 'is_active']),
        ]

    def __str__(self):
        return f"{self.name} ({self.provider.username})"


class AvailabilityRule(models.Model):
    """When a provider can be booked.

    A rule is either **weekly** (``weekday`` set) or a **one-off override** for
    a single date (``date`` set) — exactly one of the two, enforced by a check
    constraint. That is the whole recurrence model, deliberately: anything more
    (RRULE, effective ranges) is a guess at requirements that do not exist yet.

    ``service`` narrows the rule to one service; left null it applies to all of
    the provider's services. A laboratory can therefore open 07:00–11:00 for
    blood tests and 08:00–12:00 for Vitamin D, while a doctor keeps one rule for
    everything they do.
    """
    MONDAY = 0
    WEEKDAY_CHOICES = [
        (0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'),
        (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday'),
    ]

    provider = models.ForeignKey(User, on_delete=models.CASCADE,
                                 related_name='availability_rules')
    service = models.ForeignKey(Service, on_delete=models.CASCADE, null=True,
                                blank=True, related_name='availability_rules')
    #: Python's ``date.weekday()`` numbering (Monday = 0), so no conversion is
    #: needed when a rule is matched against a calendar date.
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAY_CHOICES,
                                               null=True, blank=True)
    #: Set instead of ``weekday`` to open a single specific date.
    date = models.DateField(null=True, blank=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    #: Slot length when no service is chosen. A booked service overrides it with
    #: its own duration — the two must never both decide, or a 60-minute MRI
    #: would be offered on a 30-minute grid.
    slot_minutes = models.PositiveIntegerField(default=30)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['weekday', 'date', 'start_time']
        indexes = [
            models.Index(fields=['provider', 'is_active']),
            models.Index(fields=['provider', 'weekday']),
            models.Index(fields=['provider', 'date']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_time__gt=F('start_time')),
                name='availability_ends_after_it_starts'),
            models.CheckConstraint(
                # Exactly one of weekday / date. A rule with both would be
                # ambiguous; one with neither would never match anything.
                condition=(Q(weekday__isnull=False, date__isnull=True)
                           | Q(weekday__isnull=True, date__isnull=False)),
                name='availability_is_weekly_or_dated_not_both'),
            models.CheckConstraint(
                condition=Q(slot_minutes__gte=5),
                name='availability_slot_is_at_least_five_minutes'),
        ]

    def __str__(self):
        when = self.date.isoformat() if self.date else self.get_weekday_display()
        return f"{self.provider.username} {when} {self.start_time}-{self.end_time}"

    @property
    def is_dated(self):
        return self.date is not None


class TimeOff(models.Model):
    """A period a provider cannot be booked, overriding availability.

    Covers lunch, maintenance windows and leave with one shape: a date, and
    optionally a time range within it. Both times null means the whole day —
    which is what "Dr. Omar is on leave on the 10th" actually means, and saves
    callers from writing 00:00–23:59.
    """
    provider = models.ForeignKey(User, on_delete=models.CASCADE,
                                 related_name='time_off')
    date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    reason = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date', 'start_time']
        verbose_name_plural = 'time off'
        indexes = [
            models.Index(fields=['provider', 'date']),
        ]
        constraints = [
            models.CheckConstraint(
                # Both set (a window) or both null (the whole day). One of each
                # would be an open-ended block with no defined meaning.
                condition=(Q(start_time__isnull=True, end_time__isnull=True)
                           | Q(start_time__isnull=False, end_time__isnull=False)),
                name='time_off_window_is_complete_or_absent'),
            models.CheckConstraint(
                condition=(Q(start_time__isnull=True)
                           | Q(end_time__gt=F('start_time'))),
                name='time_off_ends_after_it_starts'),
        ]

    def __str__(self):
        if self.start_time is None:
            return f"{self.provider.username} off {self.date}"
        return f"{self.provider.username} off {self.date} {self.start_time}-{self.end_time}"

    @property
    def is_all_day(self):
        return self.start_time is None


class Appointment(models.Model):
    """A booked visit with any provider — doctor, laboratory or radiology centre.

    Appointments have a lifecycle rather than being immutable: without a status
    a mistaken booking could never be undone, and its slot stayed blocked for
    everyone else forever.

    **Why the provider is a user and the time is a datetime range.** This model
    used to carry ``doctor``/``date``/``time``, which could express exactly one
    kind of booking at exactly one grid position. A laboratory could not be
    booked at all, and a 60-minute MRI at 10:00 did not conflict with a
    30-minute one at 10:30 because only the start instant was compared. Storing
    ``start_at``/``end_at`` as aware datetimes makes the occupied period
    explicit, which is what lets the database itself refuse overlaps.
    """
    SCHEDULED = 'scheduled'
    CANCELLED = 'cancelled'
    COMPLETED = 'completed'
    NO_SHOW = 'no_show'
    STATUS_CHOICES = [
        (SCHEDULED, 'Scheduled'),
        (CANCELLED, 'Cancelled'),
        (COMPLETED, 'Completed'),
        (NO_SHOW, 'No show'),
    ]
    #: States that still occupy the provider's slot. A booking is confirmed the
    #: moment it is made; Roshada has no pending-approval step, so 'scheduled'
    #: is what other systems would call CONFIRMED.
    ACTIVE_STATUSES = (SCHEDULED,)

    provider = models.ForeignKey(User, on_delete=models.CASCADE,
                                 related_name='provider_appointments')
    patient = models.ForeignKey(User, on_delete=models.CASCADE,
                                related_name='patient_appointments')
    #: PROTECT, not SET_NULL: a laboratory reading its queue must always be able
    #: to see which test was booked. Services are deactivated, never deleted.
    service = models.ForeignKey(Service, on_delete=models.PROTECT, null=True,
                                blank=True, related_name='appointments')
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    reason = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default=SCHEDULED)
    cancellation_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_at']
        indexes = [
            models.Index(fields=['patient', 'start_at']),
            models.Index(fields=['provider', 'start_at']),
            models.Index(fields=['status']),
            models.Index(fields=['service']),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_at__gt=F('start_at')),
                name='appointment_ends_after_it_starts'),
            # The double-booking guarantee, enforced by PostgreSQL rather than
            # by application code: no two *scheduled* appointments for the same
            # provider may occupy overlapping periods. '[)' bounds are what make
            # back-to-back slots legal — 10:00-10:30 and 10:30-11:00 touch but
            # do not overlap. Because it is an index, this holds against any
            # writer, including a direct ORM call that skips the service layer.
            ExclusionConstraint(
                name='no_overlapping_provider_appointments',
                expressions=[
                    ('provider', RangeOperators.EQUAL),
                    (Func(F('start_at'), F('end_at'), Value('[)'),
                          function='tstzrange',
                          output_field=DateTimeRangeField()),
                     RangeOperators.OVERLAPS),
                ],
                condition=Q(status='scheduled'),
            ),
        ]

    def __str__(self):
        return f"{self.patient.username} -> {self.provider.username}"

    @property
    def is_active(self):
        return self.status in self.ACTIVE_STATUSES

    # -- Local-time views of the stored instants -----------------------------
    # Stored UTC, displayed in the project timezone. Exposed as properties so
    # serializers and templates never do the conversion themselves and drift.
    @property
    def local_start(self):
        return timezone.localtime(self.start_at)

    @property
    def local_end(self):
        return timezone.localtime(self.end_at)

    @property
    def date(self):
        return self.local_start.date()

    @property
    def time(self):
        return self.local_start.time()

    @property
    def end_time(self):
        return self.local_end.time()

    @property
    def duration_minutes(self):
        return int((self.end_at - self.start_at).total_seconds() // 60)


class Document(models.Model):
    """A source document in the retrieval corpus.

    The corpus lives in the application database rather than a separate vector
    service: it is small (medical reference material, not web scale), it needs
    the same backup and access story as the rest of the data, and keeping it
    here means metadata filtering is an ORM query rather than a second query
    language. See ``services.rag.store`` for the search implementation and the
    point at which that trade stops holding.
    """
    TEXT = 'text'
    MARKDOWN = 'markdown'
    SOURCE_TYPES = [(TEXT, 'Plain text'), (MARKDOWN, 'Markdown')]

    # -- Processing lifecycle (the Knowledge Base workflow) -----------------
    UPLOADED = 'uploaded'
    PROCESSING = 'processing'
    PROCESSED = 'processed'
    FAILED = 'failed'
    ARCHIVED = 'archived'
    STATUS_CHOICES = [
        (UPLOADED, 'Uploaded'),
        (PROCESSING, 'Processing'),
        (PROCESSED, 'Processed'),
        (FAILED, 'Failed'),
        (ARCHIVED, 'Archived'),
    ]
    #: The only status whose chunks may be retrieved. A document that failed,
    #: is still processing or has been archived is excluded — applied in the
    #: queryset, never left to a caller.
    RETRIEVABLE_STATUSES = (PROCESSED,)

    GUIDELINE = 'guideline'
    LEAFLET = 'leaflet'
    REFERENCE = 'reference'
    ARTICLE = 'article'
    OTHER = 'other'
    DOCUMENT_TYPES = [
        (GUIDELINE, 'Clinical guideline'),
        (LEAFLET, 'Patient leaflet'),
        (REFERENCE, 'Reference material'),
        (ARTICLE, 'Article'),
        (OTHER, 'Other'),
    ]

    title = models.CharField(max_length=300)
    #: Stable *identity* string for re-ingestion — a file path, URL or slug.
    #: Not unique on its own any more: a document's versions share it, and
    #: which one is live is decided by ``is_active`` below.
    source = models.CharField(max_length=500)
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPES,
                                   default=TEXT)
    #: The organisation this came from. Named ``knowledge_source`` because
    #: ``source`` above already means the identity string — two concepts, two
    #: fields. Retrieval is gated on this source's verification status.
    #: PROTECT: a source with material in the corpus is archived, not deleted.
    knowledge_source = models.ForeignKey(
        'knowledge.KnowledgeSource', on_delete=models.PROTECT, null=True,
        blank=True, related_name='documents')
    document_type = models.CharField(max_length=30, choices=DOCUMENT_TYPES,
                                     default=REFERENCE)
    language = models.CharField(max_length=20, blank=True, default="en")
    description = models.TextField(blank=True, default="")
    url = models.URLField(max_length=500, blank=True, default="")

    # -- Versioning ---------------------------------------------------------
    #: Version within one ``source`` identity, starting at 1.
    version = models.PositiveIntegerField(default=1)
    #: Exactly one version per source identity is live. Guaranteed by a partial
    #: unique index below rather than by the service that is supposed to
    #: maintain it — superseded guidance that is still retrievable is worse
    #: than none at all.
    is_active = models.BooleanField(default=True)
    #: The version this one replaced. SET_NULL so history survives even if an
    #: old row is eventually purged.
    supersedes = models.ForeignKey('self', on_delete=models.SET_NULL,
                                   null=True, blank=True,
                                   related_name='superseded_by')

    publication_date = models.DateField(null=True, blank=True)
    review_date = models.DateField(null=True, blank=True)

    # -- Processing state ---------------------------------------------------
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default=UPLOADED)
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    #: Why processing failed, already sanitised — see
    #: ``knowledge.services.safe_error``. Provider exceptions can carry
    #: endpoint and credential detail, and this column is shown in the admin UI.
    error_message = models.CharField(max_length=500, blank=True, default="")

    #: The uploaded file, when the document came from one. Stored under
    #: MEDIA_ROOT, which is deliberately not routed — the bytes are reachable
    #: only through an endpoint that checks the caller first.
    file = models.FileField(upload_to='knowledge/documents/', null=True,
                            blank=True)

    #: sha256 of the cleaned text. Re-ingesting unchanged content is a no-op,
    #: and identical content arriving under a new name is detectable.
    checksum = models.CharField(max_length=64, blank=True, default="")
    #: Corpus-level facets to filter on (audience, language, speciality…).
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['title']
        constraints = [
            models.UniqueConstraint(
                fields=['source', 'version'],
                name='unique_document_version'),
            # One live version per identity, enforced by PostgreSQL. This is
            # the guarantee behind "retrieval never returns superseded
            # guidance": even a bug in the versioning service cannot produce
            # two active rows for one source.
            models.UniqueConstraint(
                fields=['source'], condition=models.Q(is_active=True),
                name='one_active_version_per_source'),
        ]
        indexes = [
            models.Index(fields=['source_type']),
            models.Index(fields=['checksum']),
            models.Index(fields=['status']),
            models.Index(fields=['knowledge_source', 'status']),
            models.Index(fields=['source', '-version']),
        ]

    def __str__(self):
        return f"{self.title} v{self.version}"

    @property
    def is_retrievable(self):
        """Indexed *and* allowed to answer: live version, processed, approved."""
        return bool(
            self.is_active
            and self.status in self.RETRIEVABLE_STATUSES
            and self.knowledge_source is not None
            and self.knowledge_source.is_approved)

    @property
    def provenance(self):
        """Where a retrieved passage came from, all the way up to the source.

        Assembled here so every consumer cites a document the same way, and so
        a chunk can never be shown without the trail that makes it checkable.
        """
        source = self.knowledge_source
        return {
            "document_id": self.id,
            "document_title": self.title,
            "document_version": self.version,
            "document_type": self.document_type,
            "identity": self.source,
            "language": self.language,
            "url": self.url,
            "publication_date": (self.publication_date.isoformat()
                                 if self.publication_date else None),
            "source_id": source.id if source else None,
            "source_name": source.name if source else "",
            "source_organization": source.organization if source else "",
            "source_url": source.url if source else "",
            "verification_status": (source.verification_status if source
                                    else "unverified"),
        }


class DocumentChunk(models.Model):
    """One retrievable passage, with its embedding.

    The embedding is stored as raw little-endian float32 bytes (PostgreSQL
    ``bytea``) rather than a float array: it is compact, exact, and keeps the
    column independent of any vector extension, so adopting ``pgvector`` later
    is an additive migration rather than a change of representation.

    ``embedder``/``embedding_model``/``dimension`` record which vector space the
    embedding belongs to. Without them, an index built with one embedder and
    queried with another returns confident nonsense: the vectors are comparable
    arithmetically but mean nothing to each other. Search restricts itself to
    the active space, and a corpus that exists only in a *different* space is
    reported as "reindex required" rather than as "no results".
    """
    document = models.ForeignKey(Document, on_delete=models.CASCADE,
                                 related_name='chunks')
    #: Position within the document, so retrieved passages can be cited in order.
    ordinal = models.PositiveIntegerField(default=0)
    text = models.TextField()
    #: Heading path this passage sits under, e.g. "Hypertension > Treatment".
    section = models.CharField(max_length=300, blank=True, default="")

    embedder = models.CharField(max_length=40)
    embedding_model = models.CharField(max_length=100)
    dimension = models.PositiveIntegerField()
    #: Unit-normalised float32 vector. Normalised at write time so scoring at
    #: query time is a plain dot product.
    embedding = models.BinaryField()

    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['document_id', 'ordinal']
        constraints = [
            models.UniqueConstraint(
                fields=['document', 'ordinal', 'embedder', 'embedding_model'],
                name='unique_chunk_per_embedding_space'),
        ]
        indexes = [
            models.Index(fields=['embedder', 'embedding_model', 'dimension'],
                         name='chunk_space_idx'),
            models.Index(fields=['document', 'ordinal']),
            models.Index(fields=['section']),
        ]

    def __str__(self):
        return f"{self.document_id}#{self.ordinal} {self.text[:40]}"


class ChatMessage(models.Model):
    """One turn of a patient's AI-assistant conversation.

    Chat history was previously appended to a single shared JSONL file with no
    identity attached, so every signed-in user could read every other user's
    medical questions. Storing turns per user behind the API is what makes the
    history private, durable and device-independent.
    """
    USER = 'user'
    ASSISTANT = 'assistant'
    ROLE_CHOICES = [(USER, 'User'), (ASSISTANT, 'Assistant')]

    user = models.ForeignKey(User, on_delete=models.CASCADE,
                             related_name='chat_messages')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES)
    text = models.TextField()
    #: An action the assistant proposed on this turn and is waiting for the
    #: person to agree to — ``{"tool": ..., "arguments": {...}}``.
    #:
    #: Stored because "ask before you book" only means something if the
    #: agreement can be checked against what was actually proposed. A model that
    #: describes one appointment and then books another is the failure this
    #: prevents: the write is matched against this row, not against whatever the
    #: model says it offered. Never shown to the user; it is the same text they
    #: already read, in a form the server can compare.
    pending_action = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'id']
        indexes = [
            models.Index(fields=['user', 'created_at']),
        ]

    def __str__(self):
        return f"{self.user.username} [{self.role}] {self.text[:40]}"
