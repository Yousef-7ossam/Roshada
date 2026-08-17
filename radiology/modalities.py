"""The imaging modality vocabulary.

One list, used in three places: the centre's service catalogue writes it into
``Service.category``, a doctor's imaging order records it as ``modality``, and
the patient's search matches the two. Because both sides are constrained to the
same constants, matching an order to the services that can fulfil it is exact by
construction rather than a string comparison that a typo silently breaks.

Kept in its own module, free of Django imports, so the frontend can offer the
same choices without importing the backend.
"""

XRAY = "xray"
CT = "ct"
MRI = "mri"
ULTRASOUND = "ultrasound"
DOPPLER = "doppler"
MAMMOGRAPHY = "mammography"

#: Declaration order is display order.
ALL = (XRAY, CT, MRI, ULTRASOUND, DOPPLER, MAMMOGRAPHY)

LABELS = {
    XRAY: "X-Ray",
    CT: "CT Scan",
    MRI: "MRI",
    ULTRASOUND: "Ultrasound",
    DOPPLER: "Doppler",
    MAMMOGRAPHY: "Mammography",
}

CHOICES = [(value, LABELS[value]) for value in ALL]

#: The DICOM modality code each maps to. Recorded on stored files so a future
#: PACS integration has the value it needs; nothing reads it yet.
DICOM_CODES = {
    XRAY: "CR",
    CT: "CT",
    MRI: "MR",
    ULTRASOUND: "US",
    DOPPLER: "US",
    MAMMOGRAPHY: "MG",
}


def label(modality):
    return LABELS.get(modality, (modality or "").title())


def is_valid(modality):
    return modality in LABELS
