"""The tool contract, the registry, and the one place authorization happens.

A tool is a **named, schema'd call into an existing Roshada service**. It is not
a new capability: every handler in this package delegates to the same service
the HTTP views call, so there is exactly one implementation of "what are my
appointments" and the AI does not get a second, laxer one.

Three rules make this safe to hand to a language model:

**The model never chooses whose data it reads.** Handlers receive the
authenticated ``user`` from the request, injected here — never from the model's
arguments. No tool schema contains ``patient_id`` or ``user_id``, so there is no
field for a model to put someone else's identifier in. The one identifier a
model may pass is a *provider* id, which is public directory data.

**Role decides what exists.** A tool declares the roles that may call it, and
``execute`` refuses anything else, reading the role from the account record
rather than from anything the caller sent.

**Writes need a confirmation the model cannot forge.** See :mod:`.confirm`.

Nothing here can reach the database except through a service, and no handler
receives a query, an ORM object, or SQL.
"""
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger("appointments")

#: Every registered tool, by name.
REGISTRY = {}


class ToolError(Exception):
    """A handler could not do what was asked, for a reason worth reporting.

    The message reaches the model (and so may reach the user), so it must read
    as an explanation — never as an internal error.
    """


@dataclass(frozen=True)
class Tool:
    """One callable capability."""

    name: str
    description: str
    #: JSON Schema for the arguments the model may supply. Identity is never
    #: among them.
    parameters: dict
    #: Roles permitted to call it.
    roles: tuple
    handler: object
    #: True when calling it changes something. Gated by :mod:`.confirm`.
    writes: bool = False

    def schema(self):
        """The OpenAI-dialect tool declaration."""
        return {"type": "function",
                "function": {"name": self.name,
                             "description": self.description,
                             "parameters": self.parameters}}


def tool(*, name, description, parameters=None, roles, writes=False):
    """Register a handler as a tool.

    ``parameters`` defaults to "no arguments", which is the right default: most
    of these answer a question about the caller, and the caller is not something
    the model supplies.
    """
    def register(handler):
        REGISTRY[name] = Tool(
            name=name, description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            roles=tuple(roles), handler=handler, writes=writes)
        return handler
    return register


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------
def role_of(user):
    """The authenticated user's role, from the account record.

    Never from the request body, a header, or the model. ``accounts.services``
    owns the resolution (including users created outside registration), so this
    is a delegation rather than a second answer.
    """
    from accounts.services import role_of as resolve

    return resolve(user)


def for_role(role):
    """Tools this role may call, in declaration order."""
    return [t for t in REGISTRY.values() if role in t.roles]


def schemas_for(role):
    """Tool declarations to send to the model for this role."""
    return [t.schema() for t in for_role(role)]


def names_for(role):
    return [t.name for t in for_role(role)]


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _parse(arguments):
    """The model's argument JSON, as a dict.

    Models occasionally emit an empty string or ``null`` for "no arguments";
    both mean the same thing and neither is an error.
    """
    if isinstance(arguments, dict):
        return arguments
    text = (arguments or "").strip()
    if not text or text == "null":
        return {}
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("arguments must be an object")
    return value


def _accepted(tool_, supplied):
    """Only the arguments this tool declares. Everything else is dropped.

    A model that invents ``patient_id=7`` gets it discarded here rather than
    passed to a handler that might have honoured it. Silent by design: the
    field does not exist, so there is nothing to report.
    """
    declared = set((tool_.parameters or {}).get("properties") or {})
    return {k: v for k, v in supplied.items() if k in declared}


def execute(user, name, arguments="{}", *, message=""):
    """Run one tool call on behalf of ``user``.

    Always returns a JSON-serialisable dict; never raises. A failure is a
    result the model must be told about — an exception here would abandon the
    turn and lose the answer the user was waiting for.

    ``message`` is the user's current message, used only by the confirmation
    gate on write tools.
    """
    from . import confirm

    tool_ = REGISTRY.get(name)
    if tool_ is None:
        return {"ok": False, "error": f"There is no tool called '{name}'."}

    role = role_of(user)
    if role not in tool_.roles:
        # Logged as a security event: a model reaching for a tool its role does
        # not hold is worth seeing, even though it was refused.
        logger.warning("AI tool '%s' refused for role %s", name, role)
        return {"ok": False,
                "error": f"Your account is not allowed to use '{name}'."}

    try:
        supplied = _parse(arguments)
    except (ValueError, TypeError):
        return {"ok": False,
                "error": f"The arguments for '{name}' were not valid JSON."}

    kwargs = _accepted(tool_, supplied)

    if tool_.writes:
        verdict = confirm.check(user, tool_, kwargs, supplied, message)
        if verdict is not None:
            return verdict
        # ``confirm`` is the gate's field, not a handler argument.
        kwargs.pop("confirm", None)

    try:
        result = tool_.handler(user, **kwargs)
    except ToolError as exc:
        return {"ok": False, "error": str(exc)}
    except TypeError as exc:
        # A wrong argument shape from the model, not a bug: report it so the
        # model can correct itself rather than failing the whole turn.
        if _looks_like_signature_error(exc, tool_.handler):
            logger.info("AI tool '%s' called with bad arguments: %s", name, exc)
            return {"ok": False,
                    "error": f"'{name}' was called with arguments it does not "
                             f"accept."}
        logger.exception("AI tool '%s' failed", name)
        return {"ok": False, "error": f"'{name}' could not be completed."}
    except Exception:                                       # noqa: BLE001
        logger.exception("AI tool '%s' failed", name)
        return {"ok": False, "error": f"'{name}' could not be completed."}

    if tool_.writes:
        # The proposal has been acted on. It authorised this one write, not
        # every future call that happens to match it.
        from ... import chat

        chat.clear_pending_action(user)

    logger.info("AI tool '%s' ran for role %s", name, role)
    return result if isinstance(result, dict) else {"ok": True, "result": result}


def _looks_like_signature_error(exc, handler):
    """Distinguish "you passed the wrong arguments" from a bug inside."""
    try:
        name = handler.__name__
    except AttributeError:
        return False
    return name in str(exc)
