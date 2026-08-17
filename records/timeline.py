"""The reference layer: one medical event, and the registry that collects them.

**Why this is a registry and not a table.** The design calls for a
``MedicalRecordEntry`` layer *if the existing architecture does not already
provide an appropriate aggregation mechanism*. It does: Roshada already
aggregates across domains through contributor registries — the dashboard's
``contribute`` and the scheduling engine's booking callbacks. This module is
that same mechanism applied to medical history, so a domain module publishes
its events without this package importing it.

The layer is real; it is simply not a second copy of the data. A stored entry
row carrying type/date/title/status would be duplicated metadata that drifts:
a radiology report released tomorrow leaves yesterday's row saying "draft", and
the row still has to re-check the source's permissions at read time — so it
buys nothing on the security side while creating exactly the disagreement
between record and source that this layer exists to prevent.

Every contributor is handed the **viewer** and the **subject**, and is expected
to build its entries from its own role-scoped queryset. That is what makes
"the medical record cannot bypass a source module's permissions" structural
rather than a promise: an unreleased report is not filtered out of the
timeline, it is never fetched.
"""
import heapq
from dataclasses import dataclass, field
from datetime import datetime

#: The event vocabulary. Declared here so the API, the tests and the frontend
#: filter agree on one spelling — a type string invented at a call site is a
#: filter that silently matches nothing.
CONSULTATION = "consultation"
APPOINTMENT = "appointment"
LAB_RESULT = "lab_result"
RADIOLOGY_ORDER = "radiology_order"
RADIOLOGY_REPORT = "radiology_report"
PRESCRIPTION = "prescription"
MEDICATION_ORDER = "medication_order"

#: Declaration order is display order in the filter.
ALL_TYPES = (CONSULTATION, APPOINTMENT, LAB_RESULT, RADIOLOGY_ORDER,
             RADIOLOGY_REPORT, PRESCRIPTION, MEDICATION_ORDER)

TYPE_LABELS = {
    CONSULTATION: "Consultation",
    APPOINTMENT: "Appointment",
    LAB_RESULT: "Laboratory result",
    RADIOLOGY_ORDER: "Imaging order",
    RADIOLOGY_REPORT: "Radiology report",
    PRESCRIPTION: "Prescription",
    MEDICATION_ORDER: "Medication order",
}

#: Event type -> the portal page that already knows how to show it in full.
#: "View details" hands off to the module that owns the record rather than
#: re-rendering it here, so the detail view keeps the source's own
#: authorization instead of a copy of it.
TYPE_DESTINATIONS = {
    CONSULTATION: "My Appointments",
    APPOINTMENT: "My Appointments",
    LAB_RESULT: "Laboratory",
    RADIOLOGY_ORDER: "Radiology",
    RADIOLOGY_REPORT: "Radiology",
    PRESCRIPTION: "Prescriptions",
    MEDICATION_ORDER: "Pharmacy",
}


def label(event_type):
    return TYPE_LABELS.get(event_type, (event_type or "").replace("_", " ").title())


def is_valid(event_type):
    return event_type in TYPE_LABELS


@dataclass(frozen=True)
class Entry:
    """One event on the medical timeline: a *reference*, not a copy.

    Carries only what a timeline row shows — when, what kind, a title, who
    provided it, and its status. The clinical content stays in the source
    module, reached through ``source`` + ``reference`` when the reader opens it,
    where that module's own permission check runs again.
    """

    type: str
    #: The sort key. Always the source record's own real timestamp — a released
    #: report's release time, a prescription's issue time — never "now" and
    #: never a substitute.
    at: datetime
    title: str
    #: The app label and object id of the record this refers to. Enough to open
    #: it through the owning module; not enough to read it without that
    #: module's permission check.
    source: str
    reference: int
    status: str = ""
    status_label: str = ""
    provider: str = ""
    detail: str = ""
    #: Extra fields a specific type needs. Kept small on purpose — anything
    #: large belongs in the source, not on a timeline row.
    extra: dict = field(default_factory=dict)

    @property
    def type_label(self):
        return label(self.type)

    def as_dict(self):
        return {
            "type": self.type,
            "type_label": self.type_label,
            "date": self.at.isoformat(),
            "title": self.title,
            "status": self.status,
            "status_label": self.status_label or self.status,
            "provider": self.provider,
            "detail": self.detail,
            # The source module and record id, so a client can open it through
            # the owning module. No internal patient/provider ids are exposed.
            "source": self.source,
            "reference": self.reference,
            "destination": TYPE_DESTINATIONS.get(self.type),
            **({"extra": self.extra} if self.extra else {}),
        }

    def matches(self, term):
        """Case-insensitive match over the fields a person would search."""
        term = (term or "").strip().lower()
        if not term:
            return True
        return any(term in (value or "").lower()
                   for value in (self.title, self.provider, self.detail,
                                 self.type_label, self.status_label))


# ---------------------------------------------------------------------------
# The source registry
#
# A contributor is ``fn(viewer, patient, limit) -> iterable[Entry]``. It must
# build its entries from a queryset already scoped to ``viewer`` — the registry
# does no permission checking of its own, deliberately: a filter here would be
# a second, weaker copy of a rule the source module already enforces.
# ---------------------------------------------------------------------------
_SOURCES = []


def source(callback):
    """Register a timeline contributor. Returns it, so it can be used bare."""
    if callback not in _SOURCES:
        _SOURCES.append(callback)
    return callback


def registered():
    """The contributors currently registered, for tests and diagnostics."""
    return tuple(_SOURCES)


def collect(viewer, patient, limit=200):
    """Every entry every source will show *this viewer* about *this patient*.

    A contributor that raises is not allowed to blank the timeline — one broken
    module must not make a patient's history look empty, which in a medical UI
    reads as "nothing ever happened".
    """
    import logging

    logger = logging.getLogger("appointments")
    streams = []
    for contributor in _SOURCES:
        try:
            entries = list(contributor(viewer, patient, limit) or ())
        except Exception:                              # noqa: BLE001
            logger.exception("Timeline source %s failed",
                             getattr(contributor, "__name__", contributor))
            continue
        # Newest first within each stream, so the merge below is a simple
        # k-way merge rather than a full sort of everything.
        entries.sort(key=lambda entry: entry.at, reverse=True)
        streams.append(entries)
    return streams


def build(viewer, patient, types=None, since=None, until=None, search="",
          limit=25, offset=0):
    """The merged, filtered, paginated timeline.

    Each source is asked for at most ``offset + limit`` entries, so a patient
    with years of history does not load a lifetime to show one page — section
    27. The merge is a k-way merge over already-sorted streams.

    Returns ``(entries, total_seen, has_more)``. ``total_seen`` is what the
    sources actually produced within the window, not a claim about the
    patient's whole history: counting everything would mean fetching
    everything, which is the cost this is avoiding.
    """
    window = max(offset + limit, 1)
    # One past the window, so "is there another page?" is answerable without a
    # second round trip — fetching exactly the window can never see the
    # entry that proves there is more.
    fetch = window + 1
    if types or search or since or until:
        # Headroom, because filtering happens after the fetch: a page could
        # otherwise come back short while matches sat just past the window.
        fetch = window * 4 + 1

    streams = collect(viewer, patient, limit=fetch)
    merged = heapq.merge(*streams, key=lambda entry: entry.at, reverse=True)

    wanted = set(types) if types else None
    selected = []
    seen = 0
    for entry in merged:
        if wanted is not None and entry.type not in wanted:
            continue
        if since is not None and entry.at < since:
            continue
        if until is not None and entry.at > until:
            continue
        if not entry.matches(search):
            continue
        seen += 1
        if seen > offset:
            selected.append(entry)
        if len(selected) > limit:
            break

    has_more = len(selected) > limit
    return selected[:limit], seen, has_more
