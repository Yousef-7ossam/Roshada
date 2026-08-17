"""The dosage-form vocabulary, in one place.

Kept out of ``models`` for the same reason ``radiology.modalities`` is: the
frontend, the tests and the API all need to agree on the list, and a vocabulary
that is restated is a vocabulary that drifts. Serving it from an endpoint means
no client ever hardcodes it.
"""

TABLET = "tablet"
CAPSULE = "capsule"
SYRUP = "syrup"
SUSPENSION = "suspension"
INJECTION = "injection"
CREAM = "cream"
OINTMENT = "ointment"
DROPS = "drops"
INHALER = "inhaler"
SUPPOSITORY = "suppository"
OTHER = "other"

#: Declaration order is display order: the oral forms a prescription most often
#: names come first.
ALL = (TABLET, CAPSULE, SYRUP, SUSPENSION, INJECTION, CREAM, OINTMENT, DROPS,
       INHALER, SUPPOSITORY, OTHER)

LABELS = {
    TABLET: "Tablet",
    CAPSULE: "Capsule",
    SYRUP: "Syrup",
    SUSPENSION: "Suspension",
    INJECTION: "Injection",
    CREAM: "Cream",
    OINTMENT: "Ointment",
    DROPS: "Drops",
    INHALER: "Inhaler",
    SUPPOSITORY: "Suppository",
    OTHER: "Other",
}

CHOICES = [(form, LABELS[form]) for form in ALL]


def label(form):
    return LABELS.get(form, (form or "").title())


def is_valid(form):
    return form in LABELS
