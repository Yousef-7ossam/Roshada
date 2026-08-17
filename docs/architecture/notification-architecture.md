# Notification architecture

How a domain event becomes a notification, and which channels exist.

[Back to the project README](../../README.md)

## Notification architecture

```mermaid
flowchart TD
    EV["Domain event: booking, prescription, report release, request status"]
    EV --> HOOK["Callback registry in the domain module"]
    HOOK --> SVC["Notification service"]
    SVC --> ROW["Notification row in PostgreSQL"]
    ROW --> INAPP["In-app notification centre"]
    SVC -.no backend registered.-> EMAIL["Email"]
    SVC -.no backend registered.-> PUSH["Push"]
    SVC -.no backend registered.-> SMS["SMS"]
```

## Notification data flow

```mermaid
flowchart LR
    A["Appointment booked / cancelled / rescheduled / completed"] --> NS["Notification service"]
    B["Prescription issued or updated"] --> NS
    C["Radiology report released"] --> NS
    D["Medication request confirmed / preparing / ready / completed"] --> NS
    E["Message received"] --> NS
    NS --> IA["In-app notification centre"]
    IA --> U["Patient / Doctor / Facility"]
```
