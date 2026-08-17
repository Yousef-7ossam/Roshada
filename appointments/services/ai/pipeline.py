"""The assistant pipeline.

    user -> medical knowledge question?  -> retrieve -> grounded answer
         -> otherwise: context -> prompt -> llm -> validation -> response

One entry point, :func:`ask`. It always returns an :class:`AssistantReply`; it
never raises for a provider problem, because a chat UI has nothing useful to do
with an exception and a patient has nothing useful to do with a stack trace.

Three invariants are load-bearing and covered by tests:

1. **The emergency notice is emitted regardless of provider state.** If the
   message describes a red-flag presentation, the notice is prepended even when
   no provider is configured and even when the provider call fails. The one
   message a user must never miss cannot depend on a third party being up.
2. **Context is assembled before the exchange is recorded**, so the prompt never
   contains the question it is being asked to answer.
3. **A general medical question is retrieved for before it is answered.** The
   grounded branch (:mod:`.grounding`) runs first; only questions that are not
   medical knowledge — the user's own appointments, using the product, small
   talk — reach the ungrounded path below. The two branches never share
   context: a grounded answer is built from published reference material and
   carries none of the user's record.
"""
import logging
from dataclasses import dataclass, field

from shared.safety import EMERGENCY_NOTICE, detect_emergency

from .. import chat
from . import agent
from . import context as context_module
from . import grounding
from . import llm, prompts, validation

logger = logging.getLogger("appointments")

# The fallback copy lives in ``services.chat`` because that module also has to
# recognise these replies and keep them out of the conversation window — a
# stored "couldn't reach the provider" must never be replayed to the model as
# though it were an answer.
#
# UNCONFIGURED_REPLY — no provider configured (an operator problem; say so
#   plainly rather than implying the user did something wrong).
# FAILED_REPLY       — a configured provider errored. The provider detail is
#   logged, never shown: it carries URLs, model ids and library internals.
# REJECTED_REPLY     — the provider answered but validation refused the answer.
UNCONFIGURED_REPLY = chat.UNCONFIGURED_REPLY
FAILED_REPLY = chat.FAILED_REPLY
REJECTED_REPLY = chat.REJECTED_REPLY


@dataclass
class AssistantReply:
    """One structured assistant turn."""
    reply: str
    emergency: dict = field(default_factory=lambda: {"detected": False, "label": None})
    #: Which parts of the *user's own record* informed the answer, as
    #: attribution chips. Empty on a grounded answer, by construction: that
    #: branch reads no patient data.
    sources: list = field(default_factory=list)
    #: Numbered knowledge-base citations behind a grounded answer. A separate
    #: field from ``sources`` because they are different things — one is "what
    #: we looked at about you", the other is "what this claim rests on" — and
    #: collapsing them would make a medical citation indistinguishable from a
    #: note that we read your profile.
    citations: list = field(default_factory=list)
    #: True when the reply came from the knowledge base rather than the model's
    #: own knowledge.
    grounded: bool = False
    #: Roshada tools that ran while answering, in order. Real data was read
    #: from the platform whenever this is non-empty.
    tools_used: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    provider: str = None
    model: str = None
    #: ``name@version`` of the prompt that produced this reply, so a stored
    #: answer can be traced to the exact instructions behind it.
    prompt_version: str = None
    #: True when the reply is a fallback rather than a model answer.
    degraded: bool = False
    #: The two persisted ChatMessage rows (user turn, assistant turn).
    messages: list = field(default_factory=list)

    def as_dict(self):
        return {
            "reply": self.reply,
            "emergency": self.emergency,
            "sources": self.sources,
            "citations": self.citations,
            "grounded": self.grounded,
            "tools_used": self.tools_used,
            "warnings": self.warnings,
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "degraded": self.degraded,
        }


def _compose(notice, body):
    """Emergency notice first, always, with a visible separator."""
    return f"{notice}\n\n---\n\n{body}" if notice else body


def status():
    """Whether the assistant can answer, and with what. Used for the UI caption.

    Reports the provider, model, capability flags and which other providers are
    configured — never a credential.
    """
    return {**llm.describe(),
            "prompts": {t["name"]: t["version"] for t in prompts.catalogue()
                        if t["name"] in prompts.ROLE_PROMPTS.values()}}


def ask(user, message) -> AssistantReply:
    """Answer ``message`` for ``user`` and persist the exchange."""
    message = (message or "").strip()

    # ---- 1. Safety pre-check (independent of every external dependency) ----
    emergency_label = detect_emergency(message)
    notice = EMERGENCY_NOTICE if emergency_label else None
    result = AssistantReply(
        reply="",
        emergency={"detected": bool(emergency_label), "label": emergency_label},
    )

    # ---- 2. Grounded branch: retrieve before answering ----
    # Ahead of everything else, because a general medical question must not
    # reach the provider without sources. Returns None when this is not a
    # knowledge question, and the tool-using path below takes over.
    #
    # The role is resolved first, from the account record, because it decides
    # both which tools exist and — through that — whether a question the
    # knowledge heuristic did not recognise should be looked up instead.
    role = context_module.role_of(user)
    try:
        grounded = grounding.attempt(
            message, tools_available=agent.is_available(role))
    except Exception:                                       # noqa: BLE001
        # Grounding is a whole branch; if it breaks, the user still gets the
        # assistant. Logged loudly because a silent loss of grounding is a
        # silent loss of a safety property.
        logger.exception("Grounded answering failed for %s", user.username)
        grounded = None

    if grounded is not None:
        result.reply = _compose(notice, grounded.answer)
        result.citations = grounded.sources
        result.grounded = not grounded.degraded
        result.warnings = list(grounded.warnings)
        result.degraded = grounded.degraded
        result.provider = grounded.provider
        result.model = grounded.model
        result.prompt_version = grounded.prompt_version
        # ``sources`` stays empty: nothing from the user's record was read.
        # The knowledge base is attributed through ``citations``.
        return _record(user, message, result)

    # ---- 3. Context: who is asking, and what do we know about them? ----
    # Built before recording, so the prompt excludes the current question.
    try:
        assembled = context_module.build(user)
    except Exception:
        # Context is an enhancement; losing it must not cost the user an answer.
        logger.exception("Could not assemble AI context for %s", user.username)
        assembled = context_module.AssembledContext(role=role)

    result.sources = assembled.source_dicts()

    # An action the assistant is about to propose, recorded with the reply
    # so the person's next message can be checked against it.
    pending = None

    # ---- 4. Answer: with Roshada's tools when they are available ----
    # The tool-using turn reads the caller's real appointments, prescriptions
    # and reports through the same services their own pages use. When the role
    # holds no tools, or the configured provider cannot call them, this falls
    # back to the context-only prompt — the assistant degrades, it does not
    # fail.
    try:
        if agent.is_available(assembled.role):
            turn = agent.run(user, message, role=assembled.role,
                             context_block=assembled.facts,
                             history=assembled.turns)
            answer_text = turn.text
            result.provider, result.model = turn.provider, turn.model
            result.prompt_version = turn.prompt_version
            result.tools_used = list(turn.used)
            pending = turn.pending
        else:
            system, result.prompt_version = prompts.system_prompt(
                assembled.role, assembled.facts)
            completion = llm.complete(message, history=assembled.turns,
                                      system_prompt=system)
            answer_text = completion.text
            result.provider, result.model = completion.provider, completion.model
    except prompts.PromptError:
        # A malformed prompt library is an operator fault, not the patient's:
        # report it as "unavailable" rather than failing the request with a 500.
        logger.exception("Prompt library is unusable")
        result.reply = _compose(notice, UNCONFIGURED_REPLY)
        result.degraded = True
        return _record(user, message, result)
    except llm.LLMUnavailable:
        result.reply = _compose(notice, UNCONFIGURED_REPLY)
        result.degraded = True
        return _record(user, message, result)
    except llm.LLMFailed:
        result.reply = _compose(notice, FAILED_REPLY)
        result.degraded = True
        return _record(user, message, result)

    # ---- 5. Validation ----
    checked = validation.validate(answer_text)
    if not checked.ok:
        logger.warning("AI reply rejected by validation for %s", user.username)
        result.reply = _compose(notice, REJECTED_REPLY)
        result.warnings = checked.warnings
        result.degraded = True
        return _record(user, message, result)

    result.warnings = checked.warnings

    # ---- 6. Response ----
    result.reply = _compose(notice, checked.text)
    return _record(user, message, result, pending=pending)


def _record(user, message, result, pending=None):
    """Persist the turn pair. A storage failure must not cost the user the answer."""
    try:
        question, answer = chat.record_exchange(user, message, result.reply,
                                                pending_action=pending)
        result.messages = [question, answer]
    except Exception:
        logger.exception("Could not persist AI exchange for %s", user.username)
    return result
