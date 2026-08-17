"""The Medical Knowledge Base: who a document came from, and whether we trust it.

**This app owns exactly one model, on purpose.** Roshada already has a complete
retrieval corpus — ``appointments.Document`` and ``appointments.DocumentChunk``,
built and searched by ``appointments.services.rag``. Creating a second document
or chunk table here would be the duplicate implementation the brief forbids, so
what this app adds is the thing that genuinely did not exist: an *organisational*
source with a verification workflow, and the governance layer around it.

The relationship is the same one the rest of the platform already uses:
``knowledge`` is a domain module over the ``rag`` engine, exactly as
``radiology`` is a domain module over the scheduling engine. The engine keeps
its models; the domain adds the workflow the engine has no opinion about.

**A note on the word "source".** ``Document.source`` was already taken — it is
the stable *identity string* used to re-ingest a document (a path, URL or slug).
The organisation a document came from is a different concept, so it is
``Document.knowledge_source``, a foreign key to the model below. Two meanings,
two fields, no overloading.
"""
from django.db import models


class KnowledgeSource(models.Model):
    """An organisation whose material may enter the corpus.

    Verification is the whole point of this model. Retrieval is gated on it:
    material from a source that has not been approved is indexed but never
    returned, so a document can be prepared, processed and reviewed before it
    can influence anything a patient is ever shown.

    Rejected and archived sources stay in the table rather than being deleted —
    "we looked at this and said no" is information worth keeping, and deleting
    it invites someone to add the same source again next month.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    STATUS_CHOICES = [
        (PENDING, "Pending review"),
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
        (ARCHIVED, "Archived"),
    ]
    #: The only status whose material may be retrieved. Everything else —
    #: pending, rejected, archived — is excluded, and that exclusion is applied
    #: in the queryset rather than in a caller that might forget.
    RETRIEVABLE_STATUSES = (APPROVED,)

    #: The transitions the workflow permits. An administrator may re-approve an
    #: archived source (material comes back into use) but a rejected one has to
    #: go back through review.
    TRANSITIONS = {
        PENDING: (APPROVED, REJECTED),
        APPROVED: (ARCHIVED, REJECTED),
        REJECTED: (PENDING,),
        ARCHIVED: (APPROVED, PENDING),
    }

    GUIDELINE = "guideline"
    GOVERNMENT = "government"
    JOURNAL = "journal"
    TEXTBOOK = "textbook"
    INTERNAL = "internal"
    OTHER = "other"
    SOURCE_TYPE_CHOICES = [
        (GUIDELINE, "Clinical guideline body"),
        (GOVERNMENT, "Government / health authority"),
        (JOURNAL, "Peer-reviewed journal"),
        (TEXTBOOK, "Textbook / reference work"),
        (INTERNAL, "Internal Roshada material"),
        (OTHER, "Other"),
    ]

    name = models.CharField(max_length=200, unique=True)
    organization = models.CharField(max_length=200, blank=True, default="")
    source_type = models.CharField(max_length=30, choices=SOURCE_TYPE_CHOICES,
                                   default=OTHER)
    url = models.URLField(max_length=500, blank=True, default="")
    description = models.TextField(blank=True, default="")
    verification_status = models.CharField(max_length=20,
                                           choices=STATUS_CHOICES,
                                           default=PENDING)
    #: Why an administrator approved or rejected it. Kept because a source's
    #: trustworthiness is a judgement, and a judgement with no reasoning behind
    #: it cannot be reviewed later.
    review_notes = models.TextField(blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    #: BCP-47-ish tag. Free text rather than choices: the corpus already handles
    #: Arabic and English, and constraining this would only block the third one.
    language = models.CharField(max_length=20, blank=True, default="en")
    specialty = models.CharField(max_length=120, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["verification_status"]),
            models.Index(fields=["specialty"]),
            models.Index(fields=["language"]),
        ]

    def __str__(self):
        return f"{self.name} [{self.verification_status}]"

    @property
    def is_approved(self):
        return self.verification_status in self.RETRIEVABLE_STATUSES

    def can_transition_to(self, status):
        return status in self.TRANSITIONS.get(self.verification_status, ())

    @property
    def citation(self):
        """How this source is named when a retrieved passage cites it."""
        if self.organization and self.organization != self.name:
            return f"{self.name} ({self.organization})"
        return self.name
