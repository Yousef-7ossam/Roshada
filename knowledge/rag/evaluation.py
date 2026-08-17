"""The evaluation foundation.

Enough structure to measure retrieval and grounding over time, and deliberately
no more: a metrics table, a dashboard and a scoring job would all be scope this
task does not have, and the brief asks for a foundation.

Each answered question produces one :class:`RAGTrace` — retrieval count, top
score, how many of the sources sent were actually cited, latency, and which
provider, model and prompt version produced it. Written to the application log
as a single structured line, which is the observability the platform already
has.

**What is deliberately not recorded: the question text.** A medical question is
the most sensitive thing a person types into this product, and an evaluation
record is not a reason to keep one. What is stored is a short hash, which is
enough to notice that the same question is asked often and to correlate a trace
with a report, and not enough to read it back.

Nothing here claims medical accuracy. Groundedness is measured as *how much of
the answer's citation behaviour lines up with the sources it was given* —
a structural signal, not a clinical one. A real accuracy claim needs a validated
evaluation set and clinician review, and saying so is more useful than a number
that implies otherwise.
"""
import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field

logger = logging.getLogger("appointments")


def query_fingerprint(text):
    """A short, stable, non-reversible id for a question."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class RAGTrace:
    """One answered question, as measured."""

    query_id: str
    language: str = "en"
    # -- retrieval --
    matches: int = 0
    sources_sent: int = 0
    top_score: float = 0.0
    strong_matches: int = 0
    # -- grounding (structural, never clinical) --
    sources_cited: int = 0
    fabricated_citations: int = 0
    # -- generation --
    provider: str = None
    model: str = None
    prompt_version: str = None
    degraded: bool = False
    reason: str = ""
    latency_ms: int = 0
    # Split so a latency regression can be attributed. Retrieval and generation
    # are tuned with entirely different knobs.
    retrieval_ms: int = 0
    llm_ms: int = 0
    filters: dict = field(default_factory=dict)

    @property
    def citation_coverage(self):
        """Share of the sources sent that the answer actually cited.

        A structural groundedness signal: an answer citing one of six sources
        used less of its evidence than one citing five. It says nothing about
        whether either answer is correct.
        """
        if not self.sources_sent:
            return None
        return round(self.sources_cited / self.sources_sent, 3)

    def as_dict(self):
        return {**asdict(self), "citation_coverage": self.citation_coverage}


def build(processed, result):
    """Measure one answered question."""
    signals = result.retrieval or {}
    return RAGTrace(
        query_id=query_fingerprint(processed.text),
        language=result.language,
        matches=signals.get("matches", 0),
        sources_sent=signals.get("sources_used", 0),
        top_score=signals.get("top_score", 0.0),
        strong_matches=signals.get("strong_matches", 0),
        sources_cited=sum(1 for s in result.sources if s.get("cited")),
        fabricated_citations=len(result.fabricated_citations),
        provider=result.provider,
        model=result.model,
        prompt_version=result.prompt_version,
        degraded=result.degraded,
        reason=result.reason,
        latency_ms=result.latency_ms,
        retrieval_ms=result.retrieval_ms,
        llm_ms=result.llm_ms,
        # Which facets narrowed the search — useful for spotting a filter that
        # always returns nothing. Values, not free text: these are ids and
        # short facet names the caller supplied.
        filters={k: v for k, v in (processed.filters or {}).items()
                 if k != "metadata"},
    )


def record(processed, result):
    """Log one trace. Never raises — measurement must not cost an answer."""
    try:
        trace = build(processed, result)
        logger.info("RAG trace %s", json.dumps(trace.as_dict(), default=str))
        return trace
    except Exception:                                       # noqa: BLE001
        logger.exception("Could not record a RAG trace")
        return None
