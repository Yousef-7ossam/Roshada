"""The prompt library.

Every instruction Roshada gives a model lives in ``library/`` as a Markdown file
with versioned frontmatter — not in a Python string, and not scattered across
the modules that happen to call the model. Application code asks for a prompt by
name; it never contains one.

Organised by purpose:

| purpose | prompt | status |
|---|---|---|
| `medical_assistant` | `medical_assistant` | active — patient chat |
| `doctor_copilot` | `doctor_copilot` | active — clinician chat |
| `patient_copilot` | `patient_copilot` | planned |
| `rag` | `rag_answer` | planned |
| `agents` | `agent_tool_use` | planned |
| `safety` | `safety` (fragment), `safety_review` | active / planned |

*Planned* prompts are registered so the category exists and is reviewable, but
no running code renders one. A test enforces that, so the distinction cannot
quietly rot into "we shipped an unused prompt that drifted".

Reuse works by composition: `includes: [style, safety, context_block]` prepends
shared fragments, so the safety rules are written once and cannot drift between
the patient prompt and the clinician one.

Changing a prompt does not mean changing code — see ``PROMPT_DIR`` in
:mod:`.registry`.
"""
from .registry import (
    bundle, by_purpose, catalogue, fragment_names, get, names, override_dir,
    reload, render,
)
from .template import PromptBundle, PromptError, PromptTemplate

#: Which prompt serves which account role. The only mapping from an application
#: concept to a prompt name, deliberately in one place.
ROLE_PROMPTS = {
    "patient": "medical_assistant",
    "doctor": "doctor_copilot",
}
DEFAULT_PROMPT = "medical_assistant"

#: Placeholder shown when a user has no records yet, so the context section of
#: the prompt is never blank (an empty heading invites the model to invent one).
EMPTY_CONTEXT = "(nothing on file yet)"


def for_role(role):
    """The prompt template serving an account role."""
    return get(ROLE_PROMPTS.get(role, DEFAULT_PROMPT))


def system_prompt(role, context_block):
    """The rendered system prompt for a role, with the user's context folded in.

    Returns ``(text, prompt_id)``. The id is ``name@version`` — recorded against
    the answer so a stored reply can always be traced back to the exact
    instructions that produced it.
    """
    template = for_role(role)
    text = render(template.name, context_block=context_block or EMPTY_CONTEXT)
    return text, template.id


__all__ = [
    "PromptTemplate", "PromptBundle", "PromptError",
    "get", "render", "names", "fragment_names", "by_purpose", "catalogue",
    "bundle", "reload", "override_dir",
    "ROLE_PROMPTS", "DEFAULT_PROMPT", "EMPTY_CONTEXT",
    "for_role", "system_prompt",
]
