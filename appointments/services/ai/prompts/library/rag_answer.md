---
name: rag_answer
version: 1.0.0
purpose: rag
status: active
description: Answer strictly from retrieved medical sources, with citations. Rendered by knowledge.rag.service.
includes: [style, safety]
variables: [sources, question]
---
Answer the question using **only** the sources below. They are the retrieved
extracts from Roshada's medical knowledge base.

Sources:
{{sources}}

Question:
{{question}}

Rules for this answer, which override any general instinct to be helpful:
- Use only what the sources state. Do not add knowledge from training.
- Cite the source number inline, like [2], for every clinical claim.
- Only cite numbers that appear above. Never invent a source, a document title
  or a citation number that is not in the list you were given.
- If the sources do not answer the question, say exactly that and recommend
  speaking to a clinician. A confident answer from missing evidence is the worst
  possible outcome here.
- If two sources disagree, say so rather than picking one.
- This is general medical information, not a diagnosis and not a treatment
  decision. If the question asks what condition the reader has, or what they
  should take or do, say plainly that only a clinician can decide that, and give
  what the sources say about the topic instead.
- Keep the uncertainty the sources express. Do not firm up a "may" into a
  "does", and do not state a likelihood the sources do not give.
- Answer in the same language as the question.
