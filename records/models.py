"""The Unified Medical Record: metadata only, by design.

**What this module deliberately does not contain.** There is no consultation,
lab, imaging, prescription, medication or appointment model here, and no copy
of one. The existing modules stay the source of truth; the medical record is a
reference layer over them (see :mod:`records.timeline`). There is likewise no
``MedicalRecordEntry`` table — a stored row describing an event would be a
second copy of that event's metadata, free to disagree with the source the
moment a report is released or a prescription cancelled.

So this file holds exactly one model, carrying only what the record itself
knows: whose it is, whether it is open, and when it was created. Everything a
reader actually sees is assembled at read time from the modules that own it.
"""
from django.contrib.auth.models import User
from django.db import models


class MedicalRecord(models.Model):
    """One patient's medical record.

    Created on first access rather than by a backfill migration: a record with
    no events is not a clinical artefact, and manufacturing one for every user
    on the platform — including doctors, pharmacies and administrators — would
    fill the table with rows that will never mean anything.

    ``status`` exists so a record can be archived without deleting anything;
    the source data is never touched by this model either way.
    """

    ACTIVE = "active"
    ARCHIVED = "archived"
    STATUS_CHOICES = [
        (ACTIVE, "Active"),
        (ARCHIVED, "Archived"),
    ]

    patient = models.OneToOneField(User, on_delete=models.CASCADE,
                                   related_name="medical_record")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES,
                              default=ACTIVE)
    #: A clinician-facing free-text note about the record as a whole — not
    #: about any one event, which belongs to the module that owns it. Blank
    #: unless someone authorized writes it.
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["status"]),
        ]

    def __str__(self):
        return f"Medical record for {self.patient.username}"

    @property
    def is_active(self):
        return self.status == self.ACTIVE
