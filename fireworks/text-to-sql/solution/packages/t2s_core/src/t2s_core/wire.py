"""The wire JSON schema sent as ``response_format`` (D7).

Deliberately *permissive*: it pins the shape (keys, types, the response_class
enum) and nothing else. The semantic cross-field invariants live in
:class:`t2s_core.models.ModelEnvelope` -- see finding #2. ``strict: true``
requires every property to be listed in ``required`` and
``additionalProperties: false``, which is why nullable fields are typed as
``["string", "null"]`` rather than omitted.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ENVELOPE_SCHEMA", "SCHEMA_NAME", "response_format"]

SCHEMA_NAME = "t2s_envelope"

_ERROR_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "properties": {
        "code": {
            "type": "string",
            "description": "Short stable slug, e.g. 'schema_mismatch' or 'unanswerable'.",
        },
        "message": {"type": "string", "description": "One sentence, user-facing."},
        "details": {"type": ["string", "null"]},
    },
    "required": ["code", "message", "details"],
    "additionalProperties": False,
}

ENVELOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "response_class": {
            "type": "string",
            "enum": ["valid", "clarification_needed", "error"],
        },
        "query": {
            "type": ["string", "null"],
            "description": "The SQL, or null when response_class is not 'valid'.",
        },
        "prose": {
            "type": "string",
            "description": "One sentence for the user explaining the answer, the "
            "clarifying question, or the error.",
        },
        "error": _ERROR_SCHEMA,
    },
    "required": ["response_class", "query", "prose", "error"],
    "additionalProperties": False,
}


def response_format(
    schema: dict[str, Any] | None = None, name: str = SCHEMA_NAME
) -> dict[str, Any]:
    """The OpenAI-compatible ``response_format`` block Fireworks honours."""
    return {
        "type": "json_schema",
        "json_schema": {"name": name, "schema": schema or ENVELOPE_SCHEMA, "strict": True},
    }
