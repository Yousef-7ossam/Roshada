"""AI-assistant chat history use-cases.

Every read and write is scoped to a single user. The previous implementation
appended to one shared ``chat_history.jsonl`` with no identity recorded, so the
"saved history" panel showed each signed-in user the most recent questions asked
by *anyone* — a direct disclosure of other patients' medical information.
"""
from ..models import ChatMessage

#: How many past turns to hand back to the model as conversation context.
CONTEXT_TURNS = 10
#: Upper bound on a single stored message, to keep rows sane.
MAX_TEXT_LENGTH = 8000

# Replies that are *not answers*: the assistant telling the user it could not
# answer. They are still stored, because the transcript must match what the user
# actually saw — but they must never be replayed to the model as context.
#
# Live check on 2026-08-10: with the provider rate-limited, a third of a user's
# recent turns were "couldn't reach its provider", and every one of them was
# being sent back as conversation history. The model was being asked to continue
# a conversation with its own outage messages.
#
# This module owns the definition because it owns what counts as memory; the
# pipeline imports these as its user-facing fallback copy.
UNCONFIGURED_REPLY = (
    "The AI assistant isn't available right now because no AI provider is "
    "configured for this deployment. Your message was saved, and everything "
    "else in Roshada still works.")

FAILED_REPLY = (
    "The AI assistant couldn't reach its provider just now. Please try again in "
    "a moment. If this keeps happening, let your care team know.")

REJECTED_REPLY = (
    "The assistant's answer didn't pass Roshada's safety checks, so it isn't "
    "being shown. Please rephrase your question, or ask your doctor directly.")

#: Substring match, not equality: a fallback may carry a prepended emergency
#: notice, which does not make it an answer.
NON_ANSWER_REPLIES = (UNCONFIGURED_REPLY, FAILED_REPLY, REJECTED_REPLY)


def _grounded_non_answers():
    """The knowledge path's fallbacks, which are non-answers too.

    Imported on demand: ``knowledge.rag.service`` depends on this package, so
    naming it at module level would close an import loop. A missing knowledge
    app simply means there are no extra markers.
    """
    try:
        from knowledge.rag import service as rag
    except Exception:                                       # noqa: BLE001
        return ()
    return (rag.NO_CONTEXT_REPLY, rag.UNAVAILABLE_REPLY, rag.FAILED_REPLY,
            rag.REJECTED_REPLY)


def is_non_answer(text):
    """True when a stored assistant turn is a fallback rather than an answer.

    Covers both paths. A stored "I couldn't find that in the approved sources"
    replayed into a later prompt would read to the model as something the
    assistant established, which is the opposite of what it means.
    """
    text = text or ""
    markers = NON_ANSWER_REPLIES + _grounded_non_answers()
    return any(marker in text for marker in markers)


#: How long a proposed action stays open for agreement. Long enough to read a
#: summary and reply, short enough that "yes" tomorrow books nothing.
PENDING_ACTION_TTL_MINUTES = 30


def record_exchange(user, prompt, reply, pending_action=None):
    """Persist one user/assistant turn pair for ``user``. Returns both rows."""
    question = ChatMessage.objects.create(
        user=user, role=ChatMessage.USER, text=prompt[:MAX_TEXT_LENGTH])
    answer = ChatMessage.objects.create(
        user=user, role=ChatMessage.ASSISTANT, text=reply[:MAX_TEXT_LENGTH],
        pending_action=pending_action or None)
    return question, answer


def pending_action(user):
    """The action the assistant last proposed and is still waiting on.

    The *most recent* assistant turn only: if the assistant has spoken again
    since, whatever it proposed before has been superseded and a "yes" now is
    answering the newer thing. Expired proposals are ignored rather than
    deleted — the turn is still part of the conversation, it is simply no
    longer an open offer.
    """
    from django.utils import timezone

    latest = (ChatMessage.objects
              .filter(user=user, role=ChatMessage.ASSISTANT)
              .order_by("-created_at", "-id").first())
    if latest is None or not latest.pending_action:
        return None

    age = timezone.now() - latest.created_at
    if age.total_seconds() > PENDING_ACTION_TTL_MINUTES * 60:
        return None
    return latest.pending_action


def clear_pending_action(user):
    """Close the open proposal, once it has been carried out.

    A proposal authorises one action, not a standing permission: without this,
    a second identical confirm in the same turn would pass the gate again. The
    booking engine would refuse the duplicate anyway, but a gate that keeps
    saying yes is not the guarantee it claims to be.
    """
    latest = (ChatMessage.objects
              .filter(user=user, role=ChatMessage.ASSISTANT)
              .exclude(pending_action=None)
              .order_by("-created_at", "-id").first())
    if latest is None:
        return False
    latest.pending_action = None
    latest.save(update_fields=["pending_action"])
    return True


def history(user, limit=50):
    """The user's most recent messages, oldest-first (chat reading order)."""
    recent = list(
        ChatMessage.objects.filter(user=user).order_by('-created_at', '-id')[:limit]
    )
    return list(reversed(recent))


def context_messages(user, turns=CONTEXT_TURNS):
    """Recent turns as ``[{"role", "content"}]`` for the LLM.

    Without this the assistant received only the current prompt, so any
    follow-up ("what dose?", "is that safe for me?") was answered without the
    question it referred to.

    Fallback turns are filtered out (see :data:`NON_ANSWER_REPLIES`): replaying
    "I couldn't reach the provider" as conversation history teaches the model
    that this is how it answers, and wastes the window on nothing.
    """
    messages = history(user, limit=turns * 2)
    return [{"role": m.role, "content": m.text} for m in messages
            if not (m.role == ChatMessage.ASSISTANT and is_non_answer(m.text))]


def clear(user):
    """Delete this user's history. Returns the number of messages removed."""
    deleted, _ = ChatMessage.objects.filter(user=user).delete()
    return deleted
