"""Cross-field invariants (finding #2).

The wire schema cannot express these: ``error`` is a required *nullable* field,
and the model satisfied it with ``null`` while claiming ``response_class: error``.
So they live in Pydantic, and a violation is repairable rather than fatal.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from t2s_core.models import ModelEnvelope
from t2s_core.wire import ENVELOPE_SCHEMA, response_format

VALID_CASES = [
    {"response_class": "valid", "query": "SELECT 1", "prose": "one", "error": None},
    {"response_class": "clarification_needed", "query": None, "prose": "which?", "error": None},
    {
        "response_class": "error",
        "query": None,
        "prose": "nope",
        "error": {"code": "unanswerable", "message": "no such data", "details": None},
    },
]

INVALID_CASES = [
    # The exact failure observed live: error class, null error object.
    (
        "error_without_detail",
        {"response_class": "error", "query": None, "prose": "x", "error": None},
    ),
    (
        "valid_without_query",
        {"response_class": "valid", "query": None, "prose": "x", "error": None},
    ),
    (
        "valid_with_empty_query",
        {"response_class": "valid", "query": "   ", "prose": "x", "error": None},
    ),
    (
        "valid_with_error",
        {
            "response_class": "valid",
            "query": "SELECT 1",
            "prose": "x",
            "error": {"code": "c", "message": "m", "details": None},
        },
    ),
    (
        "clarification_with_query",
        {
            "response_class": "clarification_needed",
            "query": "SELECT 1",
            "prose": "x",
            "error": None,
        },
    ),
    (
        "clarification_without_prose",
        {"response_class": "clarification_needed", "query": None, "prose": "  ", "error": None},
    ),
    (
        "error_with_query",
        {
            "response_class": "error",
            "query": "SELECT 1",
            "prose": "x",
            "error": {"code": "c", "message": "m", "details": None},
        },
    ),
    (
        "error_with_empty_message",
        {
            "response_class": "error",
            "query": None,
            "prose": "x",
            "error": {"code": "c", "message": " "},
        },
    ),
    ("unknown_class", {"response_class": "maybe", "query": None, "prose": "x", "error": None}),
]


@pytest.mark.parametrize("payload", VALID_CASES)
def test_accepts_coherent_envelopes(payload: dict[str, object]) -> None:
    assert ModelEnvelope.model_validate(payload)


@pytest.mark.parametrize(("label", "payload"), INVALID_CASES, ids=[c[0] for c in INVALID_CASES])
def test_rejects_incoherent_envelopes(label: str, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelEnvelope.model_validate(payload)


def test_wire_schema_is_strict_and_complete() -> None:
    """strict:true requires every property listed in required, and
    additionalProperties:false at every level."""
    assert ENVELOPE_SCHEMA["additionalProperties"] is False
    assert set(ENVELOPE_SCHEMA["required"]) == set(ENVELOPE_SCHEMA["properties"])
    error = ENVELOPE_SCHEMA["properties"]["error"]
    assert set(error["required"]) == set(error["properties"])
    assert error["additionalProperties"] is False
    assert response_format()["json_schema"]["strict"] is True
