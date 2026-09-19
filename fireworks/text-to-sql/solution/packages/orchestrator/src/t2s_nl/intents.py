"""The intent taxonomy and the JSON schema that constrains the router call.

The six classifications are MAIN.md's, verbatim in meaning, plus two the design
work added:

============== ==================================================================
``help``       questions about this system itself
``inspect``    questions about the user's own domain objects in this system
``create_schema`` build or iterate a data model + DDL from a description
``query``      a natural-language question to convert into SQL
``load_data``  generate sample data and load it into a sample database
``execute``    run the current/named query against a loaded database
``corrective`` D13: a durable domain fact ("actually, revenue is in cents")
``unknown``    the router could not tell -- ask, never guess
============== ==================================================================

Two properties of this module are load-bearing:

1. **The enum is enforced on the wire** (D7). ``ROUTER_SCHEMA`` is sent as
   ``response_format`` with ``strict: true``.
2. **The enum is validated again here.** inference-findings warns that this
   model honours nested schemas but can still mislabel an enum, and a wire
   schema cannot express "don't return ``query`` when the user asked for help".
   So :func:`decision_from_payload` degrades a malformed or unrecognised answer
   to ``unknown`` with a clarifying question rather than raising.

The router decides *what to do*. It is never asked *what is true* -- every
factual answer in this package is read from ``foundation``. See
``t2s_nl.inspection``.
"""

from __future__ import annotations

import json
from typing import Any, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "INTENTS",
    "ROUTER_SCHEMA",
    "ROUTER_SCHEMA_NAME",
    "Confidence",
    "Intent",
    "IntentDecision",
    "InspectTarget",
    "Parameters",
    "decision_from_payload",
]

Intent = Literal[
    "help",
    "inspect",
    "create_schema",
    "query",
    "load_data",
    "execute",
    "corrective",
    "unknown",
]

#: What an ``inspect`` turn is asking about. Every one of these is answered by a
#: deterministic read in ``t2s_nl.inspection`` -- the router only picks which.
InspectTarget = Literal[
    "databases",
    "models",
    "schemas",
    "schema_detail",
    "sample_rows",
    "queries",
    "sessions",
    "correctives",
    "state",
    "unspecified",
]

Confidence = Literal["high", "medium", "low"]

INTENTS: tuple[str, ...] = get_args(Intent)
_TARGETS: tuple[str, ...] = get_args(InspectTarget)

ROUTER_SCHEMA_NAME = "t2s_intent"


class Parameters(BaseModel):
    """Slots the router fills in alongside the intent.

    All optional, all nullable: a router that is unsure of a parameter should
    leave it null and let the orchestrator ask, rather than invent one.
    """

    model_config = ConfigDict(extra="ignore")

    inspect_target: InspectTarget = "unspecified"
    table: str | None = Field(default=None, description="A table the user named, e.g. 'orders'.")
    model_ref: str | None = Field(
        default=None, description="A saved data model the user named, by name or UUID."
    )
    row_count: int | None = Field(
        default=None, ge=1, le=10_000, description="How many rows the user asked for."
    )
    seed: int | None = Field(default=None, description="A reproducibility seed, if named.")
    text: str | None = Field(
        default=None,
        description=(
            "The payload of the intent, restated: the domain description for "
            "create_schema, the question for query, the corrective for corrective."
        ),
    )


class IntentDecision(BaseModel):
    """The router's ruling on one utterance."""

    model_config = ConfigDict(extra="ignore")

    intent: Intent
    confidence: Confidence = "medium"
    parameters: Parameters = Field(default_factory=Parameters)
    clarifying_question: str | None = None
    rationale: str = ""

    @property
    def needs_clarification(self) -> bool:
        return self.intent == "unknown"


def _param_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "inspect_target": {
                "type": "string",
                "enum": list(_TARGETS),
                "description": (
                    "Only meaningful when intent is 'inspect': which of the user's own "
                    "objects the question is about. "
                    # 'models' vs 'databases' was measured at 77% before this
                    # wording; the two are the most confusable pair in the enum
                    # and the user's word for either one varies ("workspace",
                    # "designs", "instances"), so name both explicitly.
                    "'models' means the DATA MODELS the user has designed -- the logical "
                    "designs and their versions. Any phrasing about models, data models, "
                    "designs, or 'what have I modelled', in a workspace or otherwise, is "
                    "'models'. "
                    "'databases' means running SAMPLE DATABASE instances created from a "
                    "schema and possibly loaded with rows -- physical databases, not "
                    "designs. "
                    "'schemas' means concrete DDL renderings of a model. "
                    "'schema_detail' means the structure of the current or named schema; "
                    "'sample_rows' means actual rows out of a loaded sample database; "
                    "'state' means 'where am I / what am I working on'. "
                    "Use 'unspecified' otherwise."
                ),
            },
            "table": {"type": ["string", "null"]},
            "model_ref": {"type": ["string", "null"]},
            "row_count": {"type": ["integer", "null"]},
            "seed": {"type": ["integer", "null"]},
            "text": {
                "type": ["string", "null"],
                "description": (
                    "The payload of the intent restated in the user's own words: the "
                    "domain description for create_schema, the question for query, the "
                    "domain fact for corrective. Null when the intent carries none."
                ),
            },
        },
        "required": ["inspect_target", "table", "model_ref", "row_count", "seed", "text"],
        "additionalProperties": False,
    }


#: Sent as ``response_format`` (D7). ``strict: true`` requires every property in
#: ``required`` and ``additionalProperties: false``, which is why the nullable
#: slots are typed ``["string", "null"]`` rather than omitted -- same discipline
#: as ``t2s_core.wire``.
ROUTER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "parameters": _param_schema(),
        "clarifying_question": {
            "type": ["string", "null"],
            "description": (
                "The single question to put to the user. Required when intent is "
                "'unknown'; null otherwise."
            ),
        },
        "rationale": {
            "type": "string",
            "description": "One short clause naming the cue you classified on.",
        },
    },
    "required": ["intent", "confidence", "parameters", "clarifying_question", "rationale"],
    "additionalProperties": False,
}


def decision_from_payload(raw: str) -> IntentDecision:
    """Parse the router's JSON into a decision, degrading rather than raising.

    inference-findings: enforced structured output guarantees *shape*, never
    *meaning*. Three things can still go wrong and all three land on ``unknown``
    with a clarifying question, because in a chat loop "I'm not sure what you
    meant -- did you want X?" is always a better turn than a traceback:

    * the content is not JSON at all (truncation; see finding #1);
    * ``intent`` is a string the enum does not contain;
    * the payload is JSON but the wrong shape.
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _undecided("I could not parse that. Could you rephrase what you'd like me to do?")

    if not isinstance(payload, dict):
        return _undecided("I could not parse that. Could you rephrase what you'd like me to do?")

    raw_intent = payload.get("intent")
    if not isinstance(raw_intent, str) or raw_intent not in INTENTS:
        return _undecided(
            "I'm not sure what you'd like me to do. Are you asking about your saved "
            "objects, describing a schema to build, or asking a question of the data?"
        )
    intent = cast(Intent, raw_intent)

    try:
        return IntentDecision.model_validate(payload)
    except ValidationError as first_error:
        # One bad optional parameter must not cost us the good ones. Models
        # routinely fill irrelevant fields with placeholder zeros -- a
        # create_schema decision arriving with `row_count: 0` is the observed
        # case -- and discarding the whole block there threw away the schema
        # description we actually needed. Drop only the offending fields and
        # revalidate; a still-invalid payload keeps the old behaviour.
        pruned = _prune_invalid_parameters(payload, first_error)
        if pruned is not None:
            try:
                salvaged = IntentDecision.model_validate(pruned)
            except ValidationError:
                pass
            else:
                # We dropped something the model asked for, so the decision is
                # no longer as confident as the model claimed, whatever it said.
                return salvaged.model_copy(
                    update={"confidence": "low", "rationale": "some parameters discarded"}
                )
        return IntentDecision(intent=intent, confidence="low", rationale="parameters discarded")


def _prune_invalid_parameters(
    payload: dict[str, object], error: ValidationError
) -> dict[str, object] | None:
    """Remove the specific `parameters.<field>` entries that failed validation.

    Returns ``None`` when every failure lies outside ``parameters`` -- there is
    nothing safe to prune in that case, so the caller falls back.
    """
    raw_parameters = payload.get("parameters")
    if not isinstance(raw_parameters, dict):
        return None

    # ("parameters", "<field>") -- the shallowest location that names a field.
    parameter_path_length = 2
    offending = {
        str(err["loc"][1])
        for err in error.errors()
        if len(err.get("loc", ())) >= parameter_path_length and err["loc"][0] == "parameters"
    }
    if not offending or len(offending) != len(error.errors()):
        return None

    parameters = {k: v for k, v in raw_parameters.items() if k not in offending}
    return {**payload, "parameters": parameters}


def _undecided(question: str) -> IntentDecision:
    return IntentDecision(
        intent="unknown",
        confidence="low",
        clarifying_question=question,
        rationale="router output was unusable",
    )
