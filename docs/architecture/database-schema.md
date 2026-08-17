# Database schema

The PostgreSQL entities and relationships, taken from the Django models.

[Back to the project README](../../README.md)

## Database architecture

```mermaid
erDiagram
    USER ||--|| USERACCOUNT : "has role"
    USER ||--o| PATIENTPROFILE : "patient"
    USER ||--o| DOCTOR : "doctor"
    USER ||--o| LABORATORYPROFILE : "laboratory"
    USER ||--o| RADIOLOGYPROFILE : "radiology"
    USER ||--o| PHARMACYPROFILE : "pharmacy"
    USER ||--o{ SERVICE : "offers"
    USER ||--o{ AVAILABILITYRULE : "publishes"
    USER ||--o{ TIMEOFF : "blocks"
    USER ||--o{ APPOINTMENT : "books as patient or hosts as provider"
    SERVICE ||--o{ APPOINTMENT : "booked as"
    USER ||--o| MEDICALRECORD : "registry for patient"
```

## Database architecture

```mermaid
erDiagram
    USER ||--o{ PRESCRIPTION : "issues as doctor or receives as patient"
    PRESCRIPTION ||--|{ PRESCRIPTIONITEM : contains
    MEDICATION ||--o{ PRESCRIPTIONITEM : "prescribed as"
    MEDICATION ||--o{ PHARMACYINVENTORY : "stocked as"
    USER ||--o{ PHARMACYINVENTORY : "held by pharmacy"
    USER ||--o{ MEDICATIONREQUEST : "raised by patient or filled by pharmacy"
    PRESCRIPTION ||--o{ MEDICATIONREQUEST : "requested from"
    MEDICATIONREQUEST ||--|{ MEDICATIONREQUESTITEM : contains
    USER ||--o{ IMAGINGORDER : "ordered by doctor or for patient"
    IMAGINGORDER ||--o{ EXAMINATION : "fulfilled by"
    APPOINTMENT ||--o| EXAMINATION : "is"
    EXAMINATION ||--o{ IMAGINGFILE : "produces"
    EXAMINATION ||--o| RADIOLOGYREPORT : "reported in"
```

## Database architecture

```mermaid
erDiagram
    USER ||--o{ NOTIFICATION : receives
    USER ||--o{ CONVERSATION : "patient side or doctor side"
    CONVERSATION ||--|{ MESSAGE : contains
    USER ||--o{ CHATMESSAGE : "AI conversation"
    KNOWLEDGESOURCE ||--o{ DOCUMENT : publishes
    DOCUMENT ||--|{ DOCUMENTCHUNK : "chunked into"
```
