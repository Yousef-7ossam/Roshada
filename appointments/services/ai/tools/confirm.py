"""The confirmation gate for tools that change something.

A booking must never happen because a model decided to make one. The brief's
rule is "ask first, then execute", and prompt instructions cannot enforce it —
the model writes both halves of that conversation, so a model that means to book
can also produce the "shall I?" and the "you said yes".

So the gate is server-side, and it turns on the one thing the model does not
control: **the person's own next message.**

    turn 1  model calls book_appointment
            -> nothing is written
            -> the proposal is stored against the assistant's reply
            -> the model shows it and asks

    turn 2  the person answers
            -> a write is allowed only if their answer agrees,
               and only for the action that was stored

The stored proposal is what makes this real. A token minted during the same turn
would prove nothing: the model could preview and confirm in one breath, having
shown the person nothing. Because the proposal has to come from an *earlier*
turn, there is always a message from the person in between — and that message is
what the gate reads.

Three attacks fall out of that shape:

* **Booking with no agreement** — nothing was stored, so nothing executes.
* **Booking after a refusal** — "no, not that one" is not agreement.
* **Bait and switch** — agreement to Tuesday cannot execute Wednesday, or a
  cancellation instead of a booking: the tool name and the arguments are both
  compared against what was actually proposed.
"""
import json
import re

#: Words that mean "yes, do the thing you just described".
#:
#: Deliberately excludes imperatives like "book it" and "احجز": those are how
#: somebody *asks* for a booking, and treating a request as its own confirmation
#: would let the very first message authorise the write.
_AFFIRMATIVE = re.compile(
    r"(?:^|\W)(?:"
    r"yes|yeah|yep|yup|ok|okay|sure|confirm|confirmed|correct|right|"
    r"go ahead|do it|please do|proceed|agreed|that works|sounds good"
    # Arabic: yes (formal and colloquial), fine, agreed, go on.
    "|نعم|أجل|أيوة|ايوة|أيوه|ايوه|اه|آه|تمام|موافق|موافقة|اوك|أوك"
    "|ماشي|أكيد|اكيد|كمل|يلا|اتفقنا"
    r")(?:$|\W)", re.IGNORECASE)

#: Words that mean "no". Checked first, because "no, don't do it" contains
#: "do it" and "yes but not that one" contains "yes".
_NEGATIVE = re.compile(
    r"(?:^|\W)(?:no|nope|don'?t|do not|stop|wait|not yet|but not|not that"
    r"|not this|instead|cancel that|never ?mind|hold on"
    "|لا|لأ|مش|ماتحجزش|متحجزش|استنى|انتظر|ارفض|مش موافق|مش دلوقتي"
    r")(?:$|\W)", re.IGNORECASE)


def is_affirmative(message):
    """Did the person just agree?

    A negative anywhere in the message wins. When the two signals disagree the
    safe reading is the one that does nothing.
    """
    text = (message or "").strip()
    if not text:
        return False
    if _NEGATIVE.search(text):
        return False
    return bool(_AFFIRMATIVE.search(text))


def action_fields(arguments):
    """The arguments that describe *what* would happen.

    ``confirm`` is excluded: it is how a confirmation is carried, not part of
    what is being confirmed.
    """
    return {k: v for k, v in (arguments or {}).items() if k != "confirm"}


def _canonical(arguments):
    """A form two calls can be compared by, whatever order the keys arrive in."""
    return json.dumps(action_fields(arguments), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False)


def matches(pending, tool_name, arguments):
    """Is this write the one that was proposed and agreed to?"""
    if not pending:
        return False
    return (pending.get("tool") == tool_name
            and _canonical(pending.get("arguments")) == _canonical(arguments))


def proposal(tool_name, arguments):
    """The record stored against the assistant's reply."""
    return {"tool": tool_name, "arguments": action_fields(arguments)}


def check(user, tool_, kwargs, supplied, message):
    """Decide whether this write may proceed.

    Returns ``None`` to allow it, or the result the model gets instead. Called
    by :func:`base.execute` before any handler runs.
    """
    from ... import chat

    pending = chat.pending_action(user)
    agreed = is_affirmative(message)
    asked = bool(supplied.get("confirm"))

    if asked and agreed and matches(pending, tool_.name, kwargs):
        return None

    if not asked:
        return _propose(tool_, kwargs)
    if not pending:
        return _propose(tool_, kwargs, reason=(
            "Nothing was proposed to this person yet, so there is nothing they "
            "could have agreed to."))
    if not matches(pending, tool_.name, kwargs):
        return _propose(tool_, kwargs, reason=(
            "That is not what this person was asked about. They were asked "
            f"about {pending.get('tool')} with different details."))
    return _propose(tool_, kwargs, reason=(
        "Their latest message is not an agreement. Ask again, plainly, and "
        "wait for a clear yes."))


def _propose(tool_, kwargs, reason=""):
    """What the model must show the person before anything is written."""
    action = action_fields(kwargs)
    describe = getattr(tool_.handler, "preview", None)
    summary = describe(**action) if describe else ""
    return {
        "ok": True,
        "executed": False,
        "confirmation_required": True,
        "action": tool_.name,
        "arguments": action,
        "summary": summary,
        "instruction": (
            "Nothing has been changed. Tell the person exactly what you are "
            "about to do, in their language, and ask them to confirm. Wait for "
            "their reply — you cannot agree on their behalf. Once they have "
            "said yes, call this tool again with the same arguments and "
            "confirm=true."
            + (f" {reason}" if reason else "")),
        # The proposal the caller records against this reply, so the person's
        # next message can be checked against it.
        "_pending": proposal(tool_.name, kwargs),
    }
