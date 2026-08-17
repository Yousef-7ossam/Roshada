"""The tool-using turn.

    user message
        -> model, with the tools this role holds
        -> model asks for tools
        -> Roshada runs them as the authenticated user
        -> results go back as their own turns
        -> model answers from what it actually got

This is what stops the assistant saying "I don't have access to that" about data
Roshada is holding. It does not give the model new reach: every tool is a call
into a service the user's own pages already use, executed as them.

Three properties are load-bearing:

**The loop is bounded.** A model that keeps asking for tools is stopped after
:data:`MAX_STEPS` rounds and the turn degrades to a plain answer. An unbounded
agent loop is an unbounded bill and an unbounded wait.

**A tool failure is reported, never filled in.** Failed calls come back as
results saying so, and each result carries its own instruction to say what
happened. The alternative — a gap the model closes with something plausible —
is the exact failure this whole layer exists to prevent.

**Providers that cannot call tools still answer.** Tool support is a capability,
not a requirement: against the mock or any adapter without it, this module is
skipped entirely and the assistant behaves as it did before.

**Where the tool rules live.** Not in a separate system prompt. The library has
an ``agent_tool_use`` template written for this, but it is marked
``status: planned`` and the platform enforces that planned prompts are not used
by running code — and prompts are Markdown, which this task may not edit. That
turns out to be the better arrangement anyway: the rules that matter are
*enforced* rather than requested. "Ask before writing" is the two-turn gate in
:mod:`.tools.confirm`, which turns on the person's own reply rather than on an
instruction the model may forget; "report what the tool returned" travels in
each result's own ``note``; what each tool does is its schema description. The
role prompt the assistant already uses supplies the clinical safety rules,
unchanged.
"""
import json
import logging
import os
from dataclasses import dataclass, field

from . import llm, prompts, tools
from .providers import ASSISTANT, SYSTEM, TOOL, USER, ChatRequest, Message

logger = logging.getLogger("appointments")

#: How many times the model may ask for tools in one turn. Four is enough for
#: "find the doctor, check their availability, then answer" with room to spare,
#: and small enough that a confused model cannot spend a fortune.
MAX_STEPS = 4

#: ``AI_TOOLS=off`` turns tool calling off platform-wide, leaving the assistant
#: exactly as it was before it had any. An operator kill switch that does not
#: need a deploy, and the switch tests use to exercise the fallback path.
TOOLS_VAR = "AI_TOOLS"

#: Truncation guard for one tool result. A runaway result would crowd out the
#: conversation; every tool already limits its own rows, so this is a backstop.
MAX_RESULT_CHARS = 4000


@dataclass
class AgentTurn:
    """One tool-using exchange, and what it did."""

    text: str = ""
    provider: str = None
    model: str = None
    prompt_version: str = None
    #: Tool names actually executed, in order. Shown to nobody by default; used
    #: by tests, the check command and the log.
    used: list = field(default_factory=list)
    #: True when the model asked for more rounds than allowed.
    exhausted: bool = False
    #: An action proposed but not performed, waiting for the person to
    #: agree. Stored against this reply so their next message can be
    #: checked against it — see :mod:`.tools.confirm`.
    pending: dict = None

    @property
    def used_tools(self):
        return bool(self.used)


def available_for(role):
    """The tool schemas this role may be offered, or ``[]`` for none."""
    return tools.schemas_for(role)


def enabled():
    """Whether tool calling is switched on for this deployment."""
    return (os.environ.get(TOOLS_VAR) or "on").strip().lower() != "off"


def is_available(role):
    """Can this turn use tools at all?

    Three things must hold: tools are switched on, the role holds some, and the
    configured provider can actually call them.
    """
    return enabled() and bool(available_for(role)) and llm.supports_tools()


def _result_message(call, payload):
    """One tool result, as the turn the model expects back."""
    body = json.dumps(payload, ensure_ascii=False, default=str)
    if len(body) > MAX_RESULT_CHARS:
        body = body[:MAX_RESULT_CHARS] + '..."}'
    return Message(TOOL, body, tool_call_id=call.id, name=call.name)


def run(user, message, *, role, context_block="", history=None,
        max_steps=MAX_STEPS):
    """Answer ``message`` for ``user``, calling tools as needed.

    Raises the same :class:`llm.LLMUnavailable` / :class:`llm.LLMFailed` the
    plain path raises, so the caller handles provider failure in one place.
    """
    schemas = available_for(role)
    # The caller's ordinary role prompt: same clinical safety rules, same
    # language handling, with their context folded in as always.
    system, prompt_id = prompts.system_prompt(role, context_block)

    conversation = [Message(SYSTEM, system)]
    for turn in history or []:
        conversation.append(Message(turn.get("role") or USER,
                                    turn.get("content", "")))
    conversation.append(Message(USER, message))

    result = AgentTurn(prompt_version=prompt_id)

    for step in range(max_steps):
        response = llm.converse(ChatRequest(
            messages=list(conversation), tools=schemas, tool_choice="auto"))
        result.provider, result.model = response.provider, response.model

        if not response.wants_tools:
            result.text = response.text
            return result

        # The assistant's request has to go back verbatim: the ids in it are
        # what pair each result with the call that asked for it.
        conversation.append(Message(ASSISTANT, response.text or "",
                                    tool_calls=response.tool_calls))

        for call in response.tool_calls:
            payload = tools.execute(user, call.name, call.arguments,
                                    message=message)
            result.used.append(call.name)
            # A proposal is bookkeeping, not something the model reads
            # back; it is lifted out here and travels with the turn.
            proposed = payload.pop("_pending", None)
            if proposed is not None:
                result.pending = proposed
            conversation.append(_result_message(call, payload))

        logger.info("AI agent step %d: ran %s", step + 1,
                    ", ".join(c.name for c in response.tool_calls))

    # Out of rounds. Ask once more with tools withheld, so the model has to
    # answer from what it already collected rather than asking again.
    result.exhausted = True
    logger.warning("AI agent hit the step limit after %s", result.used)
    final = llm.converse(ChatRequest(messages=list(conversation),
                                     tool_choice="none"))
    result.text = final.text
    result.provider, result.model = final.provider, final.model
    return result
