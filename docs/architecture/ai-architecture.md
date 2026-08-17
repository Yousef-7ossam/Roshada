# AI agent and tool calling

How the assistant selects a tool, and the two-turn gate that must pass
before anything is written.

[Back to the project README](../../README.md)

## AI agent and tool calling

```mermaid
sequenceDiagram
    participant P as Patient
    participant A as Assistant
    participant X as Tool executor
    participant S as Domain service
    P->>A: Where is the medicine my doctor prescribed?
    A->>X: get_patient_prescriptions
    X->>X: Check role, inject authenticated user, drop unknown arguments
    X->>S: patient_visible_prescriptions("user")
    S-->>X: Real prescriptions
    X-->>A: Result
    A-->>P: Answer built from what the tool returned
```

## Booking requires agreement

```mermaid
sequenceDiagram
    participant P as Patient
    participant A as Assistant
    participant G as Confirmation gate
    P->>A: Book me with a cardiologist tomorrow at 10
    A->>G: book_appointment("...")
    G-->>A: Nothing written. Proposal stored against this reply.
    A-->>P: I can book 10:00 with Dr X. Shall I?
    P->>A: Yes
    A->>G: book_appointment("..., confirm=true")
    G->>G: Does the stored proposal match, and did the person agree?
    G-->>A: Allowed
    A-->>P: Booked
```
