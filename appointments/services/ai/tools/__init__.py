"""Roshada tools the assistant may call.

    model asks for a tool
        -> execute(authenticated user, name, arguments)
        -> the same service the HTTP views use
        -> real Roshada data

Importing this package registers every tool. Patient and doctor tools live in
their own modules because the role split is the security boundary, and a single
file would invite a tool that quietly serves both.

The assistant answers with real data or says it could not get it. It never
invents an appointment, a doctor or a prescription — a tool that fails returns
an error the model is told to report, not a gap for it to fill.
"""
from . import doctor as doctor  # noqa: F401  (registers its tools)
from . import patient as patient  # noqa: F401  (registers its tools)
from .base import (
    REGISTRY, Tool, ToolError, execute, for_role, names_for, role_of,
    schemas_for, tool,
)
from .confirm import is_affirmative

__all__ = [
    # The tool modules are re-exported so that importing them for their
    # registration side effect is also a declared part of this package's API,
    # rather than something a linter reads as a stray import.
    "doctor", "patient",
    "REGISTRY", "Tool", "ToolError", "execute", "for_role", "names_for",
    "role_of", "schemas_for", "tool", "is_affirmative",
]
