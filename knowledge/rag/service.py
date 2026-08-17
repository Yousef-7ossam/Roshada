"""The RAG service — the one entry point, and the order things happen in.

    question -> process -> retrieve -> build context -> prompt -> LLM
             -> validate -> verify citations -> grounded answer + sources

Each stage is its own module; this one coordinates them and owns the decisions
that span stages. Three of those decisions are the point of the whole file:

**No context means no model call.** If the governed corpus returns nothing
relevant, the LLM is never invoked. A prompt that says "answer only from these
sources", handed no sources, is an invitation to answer from training data —
and the safest way to not take that risk is to not make the call.

**Citations are verified against what was actually sent.** The model's "[2]" is
checked against the numbered sources the application built. A number that does
not exist is reported as a fabricated citation and the answer is marked
degraded — never silently deleted, because editing the model's words into
looking grounded is the failure this is meant to catch.

**Relevance is not certainty.** What is reported is a *retrieval* signal — how
well the corpus matched — under names that say so. There is no medical
confidence here, because nothing in this pipeline could justify one.

This module is deliberately not an agent: it calls no tools, reads no patient
record, and takes no action. It answers from general medical reference material
and nothing else.
"""
import logging
import re
import time
from dataclasses import dataclass, field

from appointments.services.ai import llm, prompts, validation
from knowledge import retrieval as knowledge_retrieval

from . import config, context as context_builder, evaluation, query as query_module

logger = logging.getLogger("appointments")

#: Inline citation markers: ``[1]``, ``[2, 3]``, ``[4][5]``.
_CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

#: What is said when the corpus cannot support an answer. Fixed text, not
#: generated: the one reply that must never be improvised is the one admitting
#: we do not know.
NO_CONTEXT_REPLY = (
    "I couldn't find enough information in Roshada's approved medical "
    "knowledge sources to answer that reliably. Please ask a clinician, who "
    "can consider your own circumstances.")

UNAVAILABLE_REPLY = (
    "The medical knowledge assistant is not available right now. No answer "
    "was generated — please try again later or ask a clinician.")

FAILED_REPLY = (
    "The medical knowledge assistant could not complete that answer. Nothing "
    "was generated from partial information — please try again.")

REJECTED_REPLY = (
    "An answer was generated but did not pass Roshada's safety checks, so it "
    "is not being shown. Please ask a clinician.")

#: Appended to every generated answer. The distinction the brief insists on —
#: information versus diagnosis — stated by the application rather than left to
#: the model to remember.
INFORMATION_NOTICE = (
    "This is general medical information drawn from the sources listed below. "
    "It is not a diagnosis, a treatment decision, or a substitute for your "
    "own clinician.")


@dataclass
class RAGAnswer:
    """One grounded answer, with everything needed to audit it."""

    answer: str = ""
    sources: list = field(default_factory=list)
    #: The passages actually sent to the model. Admin/debug only — never part
    #: of a patient-facing payload.
    retrieved: list = field(default_factory=list)
    #: Retrieval signals. Explicitly *not* medical confidence; see the module
    #: docstring and the field names.
    retrieval: dict = field(default_factory=dict)
    #: True when this is a safe fallback rather than a model answer.
    degraded: bool = False
    #: Why, when it is. One of: no_context, unavailable, failed, rejected,
    #: invalid_query.
    reason: str = ""
    warnings: list = field(default_factory=list)
    #: Citation numbers the model used that do not exist in ``sources``.
    fabricated_citations: list = field(default_factory=list)
    provider: str = None
    model: str = None
    prompt_version: str = None
    language: str = query_module.ENGLISH
    #: Wall clock for the whole call, and its two expensive halves. Split
    #: because they fail for unrelated reasons and are tuned with unrelated
    #: knobs: slow retrieval is an embedding or index problem, slow generation
    #: is a provider one. A single total hides which.
    latency_ms: int = 0
    retrieval_ms: int = 0
    llm_ms: int = 0

    def as_dict(self, *, include_retrieved=False):
        """The response shape. ``retrieved`` is opt-in, not default.

        The raw passages are a debugging aid; including them by default would
        make every caller a debug surface.
        """
        payload = {
            "answer": self.answer,
            "sources": self.sources,
            "retrieval": self.retrieval,
            "degraded": self.degraded,
            "reason": self.reason,
            "warnings": self.warnings,
            "fabricated_citations": self.fabricated_citations,
            "language": self.language,
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "latency_ms": self.latency_ms,
            "retrieval_ms": self.retrieval_ms,
            "llm_ms": self.llm_ms,
        }
        if include_retrieved:
            payload["retrieved_chunks"] = self.retrieved
        return payload


# ---------------------------------------------------------------------------
# Retrieval signals — relevance, never certainty
# ---------------------------------------------------------------------------
def _retrieval_signals(results, sent):
    """What the corpus match looked like, in terms that cannot be misread.

    Every figure here describes *how well the question matched stored text*.
    None of them describes whether the answer is medically correct, and the
    naming is chosen so that a field cannot be quoted out of context as though
    it did.
    """
    scores = [r.get("score") or 0.0 for r in results]
    top = max(scores) if scores else 0.0
    strong = sum(1 for score in scores if score >= config.STRONG_SCORE)
    if not results:
        strength = "none"
    elif top >= config.STRONG_SCORE:
        strength = "strong"
    else:
        strength = "weak"
    return {
        "matches": len(results),
        "sources_used": sent,
        "top_score": round(float(top), 4),
        "strong_matches": strong,
        "threshold": config.MIN_SCORE,
        # A label about the *retrieval*, spelled out so it cannot be read as a
        # medical claim.
        "match_strength": strength,
        "note": "Retrieval relevance only — not medical certainty.",
    }


# ---------------------------------------------------------------------------
# Citation verification
# ---------------------------------------------------------------------------
def cited_numbers(text):
    """Every source number the answer refers to."""
    found = set()
    for group in _CITATION_RE.findall(text or ""):
        for part in group.split(","):
            part = part.strip()
            if part.isdigit():
                found.add(int(part))
    return found


def verify_citations(text, sources):
    """Check the answer's citations against what was actually sent.

    Returns ``(fabricated, sources)`` with each source marked ``cited``.
    Nothing is rewritten: a fabricated citation is reported so a reader can see
    the answer overclaimed, which is information that disappears the moment the
    application edits the text.
    """
    valid = {source["n"] for source in sources}
    used = cited_numbers(text)
    for source in sources:
        source["cited"] = source["n"] in used
    return sorted(used - valid), sources


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------
def answer(question, *, language=None, specialty=None, topic=None, source=None,
           document=None, section=None, top_k=None, min_score=None,
           max_context_chars=None, max_context_chunks=None):
    """Answer a question from the approved medical knowledge base.

    Never raises for a provider problem: every failure becomes a degraded
    :class:`RAGAnswer` with a reason, because "something broke" and "here is a
    medical answer" must never be the same response shape.
    """
    started = time.monotonic()

    # ---- 1. Query processing -------------------------------------------
    try:
        processed = query_module.process(
            question, language=language, specialty=specialty, topic=topic,
            source=source, document=document, section=section)
    except query_module.InvalidQuery as exc:
        return _degraded(RAGAnswer(language=query_module.ENGLISH),
                         str(exc), "invalid_query", started)

    result = RAGAnswer(language=processed.language)

    # ---- 2. Retrieval (gated: approved source, processed, live version) --
    retrieval_started = time.monotonic()
    try:
        results = knowledge_retrieval.search(
            processed.text,
            top_k=top_k if top_k is not None else config.TOP_K,
            min_score=min_score if min_score is not None else config.MIN_SCORE,
            **processed.filters)
    except knowledge_retrieval.RetrievalUnavailable as exc:
        # The corpus is unreadable, not empty. Saying "no information" here
        # would be indistinguishable from a real answer about coverage.
        #
        # **The model is not called.** Falling through to the provider here
        # would answer a medical question from training data precisely when the
        # grounding that was supposed to prevent that is broken.
        result.retrieval_ms = _elapsed(retrieval_started)
        logger.warning("RAG retrieval unavailable: %s", exc)
        return _degraded(result, UNAVAILABLE_REPLY, "unavailable", started)
    result.retrieval_ms = _elapsed(retrieval_started)

    # ---- 3. Context ------------------------------------------------------
    built = context_builder.build(results, max_chars=max_context_chars,
                                  max_chunks=max_context_chunks)
    result.retrieval = _retrieval_signals(results, built.count)
    result.retrieved = built.results
    result.sources = built.sources

    if not built.found:
        # **The model is never called.** See the module docstring.
        logger.info("RAG: no context for a query (%s chars, lang=%s)",
                    len(processed.text), processed.language)
        return _degraded(result, NO_CONTEXT_REPLY, "no_context", started,
                         processed=processed)

    # ---- 4. Prompt -------------------------------------------------------
    try:
        rendered = prompts.render("rag_answer", sources=built.text,
                                  question=processed.text)
        result.prompt_version = prompts.get("rag_answer").id
    except prompts.PromptError:
        logger.exception("RAG prompt is unusable")
        return _degraded(result, UNAVAILABLE_REPLY, "unavailable", started,
                         processed=processed)

    # ---- 5. Generation ---------------------------------------------------
    # Through the LLM facade, never a provider. This module does not know which
    # company answers, and must not: swapping the provider is a .env change.
    llm_started = time.monotonic()
    try:
        completion = llm.complete(rendered, temperature=config.TEMPERATURE,
                                  timeout=config.TIMEOUT)
        result.provider, result.model = completion.provider, completion.model
    except llm.LLMUnavailable:
        result.llm_ms = _elapsed(llm_started)
        return _degraded(result, UNAVAILABLE_REPLY, "unavailable", started,
                         processed=processed)
    except llm.LLMFailed:
        result.llm_ms = _elapsed(llm_started)
        return _degraded(result, FAILED_REPLY, "failed", started,
                         processed=processed)
    result.llm_ms = _elapsed(llm_started)

    # ---- 6. Validation ---------------------------------------------------
    # The same guard the assistant uses. A grounded answer can still state a
    # dose the sources happened to contain, and that is exactly what it checks.
    checked = validation.validate(completion.text)
    if not checked.ok:
        logger.warning("RAG answer rejected by validation")
        result.warnings = checked.warnings
        return _degraded(result, REJECTED_REPLY, "rejected", started,
                         processed=processed)
    result.warnings = checked.warnings

    # ---- 7. Citation verification ---------------------------------------
    fabricated, result.sources = verify_citations(checked.text, built.sources)
    result.fabricated_citations = fabricated
    if fabricated:
        # Surfaced, not scrubbed: an answer that cited a source it was never
        # given is a fact about the answer.
        logger.warning("RAG answer cited %d source(s) that were not provided",
                       len(fabricated))
        result.warnings = list(result.warnings) + [
            f"The answer referred to source(s) {fabricated} that were not "
            f"among the retrieved sources."]
        result.degraded = True
        result.reason = "fabricated_citation"

    result.answer = f"{checked.text.strip()}\n\n{INFORMATION_NOTICE}"
    result.latency_ms = _elapsed(started)
    evaluation.record(processed, result)
    return result


def _degraded(result, reply, reason, started, processed=None):
    result.answer = reply
    result.degraded = True
    result.reason = reason
    result.latency_ms = _elapsed(started)
    if processed is not None:
        evaluation.record(processed, result)
    return result


def _elapsed(started):
    return int((time.monotonic() - started) * 1000)


def describe():
    """The active RAG configuration, for diagnostics. No credentials."""
    from appointments.services import rag as engine

    try:
        embedding = engine.embedder_info()
    except Exception as exc:                                # noqa: BLE001
        embedding = {"error": str(exc)[:200]}

    try:
        prompt = prompts.get("rag_answer")
        prompt_info = {"name": prompt.name, "version": prompt.version,
                       "status": prompt.status}
    except prompts.PromptError as exc:
        prompt_info = {"error": str(exc)[:200]}

    return {
        "config": config.describe(),
        "llm": llm.describe(),
        "embedding": embedding,
        "prompt": prompt_info,
        "coverage": knowledge_retrieval.coverage(),
    }
