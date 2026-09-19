"""The intent taxonomy, the directive/plan model, and the schema that pins them.

The classifications are MAIN.md's six, verbatim in meaning, plus the ones the
design work added:

================= ===============================================================
``help``          questions about this system itself
``inspect``       questions about the user's own domain objects in this system
``create_schema`` build or iterate a data model + DDL from a description
``query``         a natural-language question to convert into SQL
``load_data``     generate sample data and load it into a sample database
``execute``       run the current/named query against a loaded database
``corrective``    D13: a durable domain fact ("actually, revenue is in cents")
``clear_data``    W17: empty a sample database's rows, keeping the instance
``destroy``       W17/W18: delete a sample database instance, or (W18) the whole
                  data model and everything derived from it -- ``delete_scope`` says
                  which
``cancel``        W17: "no, don't" -- never on the wire, see below
``unknown``       the router could not tell -- ask, never guess
================= ===============================================================

**W17: two of these are destructive, and one of them is not routable.** The
workbench could create, load, query and execute, and had no way at all to
*undo* any of it -- Chris asked to delete a sample database and was correctly
told the workbench has no such command. ``destroy`` and ``clear_data`` are that
command, and because they are the first irreversible things layer 3 can do,
neither ever runs on the utterance that asked for it: the orchestrator
describes exactly what would be lost and requires an affirmative next turn
(``t2s_nl.confirmation``).

**W18: ``destroy`` grew a scope rather than a sibling.** The workbench could
delete a sample database and still not delete the *model* -- the thing users
name. It is one intent with :data:`DeleteScope` rather than two destructive
intents, because a third destructive label next to ``destroy`` and
``clear_data`` is a third way to conflate them, while a scope makes
model-versus-database one question with one answer. That answer may be
``"both"``, which is executable precisely because model deletion cascades.

``cancel`` is in :data:`INTENTS` and deliberately **not** in
:data:`WIRE_INTENTS`, so it is absent from the JSON schema the model fills in.
It is produced only by reading a "no" against a pending confirmation, in code,
with no model involved -- the same discipline as ``Plan.routed_by``: a field the
model cannot set is a field the model cannot lie about. Answering a destruction
prompt is not a classification problem and must not become one.

**D15: one utterance yields an ordered list of directives, not one intent.**
"Show me the query for unpaid balances and sample results" is two directives
(``query`` then ``execute``) and the single-intent router dropped the second
half on the floor. A :class:`Plan` is that list; a :class:`Directive` is one
element of it -- an intent, its parameters, and a *referent* naming what it
operates on.

Three properties of this module are load-bearing:

1. **The enum is enforced on the wire** (D7). :data:`PLAN_SCHEMA` is sent as
   ``response_format`` with ``strict: true``.
2. **The enum is validated again here.** inference-findings warns that this
   model honours nested schemas but can still mislabel an enum, and a wire
   schema cannot express "don't return ``query`` when the user asked for help".
   So :func:`plan_from_payload` degrades a malformed or unrecognised answer to
   a one-directive ``unknown`` plan with a clarifying question, rather than
   raising.
3. **The plan length is capped and over-length is refused, never truncated**
   (D15). A plan is a bigger blast radius than an intent: silently executing
   the first four directives of a seven-directive misreading is precisely the
   failure a cap is supposed to prevent, so :data:`MAX_PLAN_DIRECTIVES` turns
   into a refusal and no directive runs.

The router decides *what to do*. It is never asked *what is true* -- every
factual answer in this package is read from ``foundation``, and a directive's
referent is resolved from the activity log (D14), not from the model.
"""

from __future__ import annotations

import json
from typing import Any, Literal, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

__all__ = [
    "DESTRUCTIVE_INTENTS",
    "INTENTS",
    "MAX_PLAN_DIRECTIVES",
    "PLAN_SCHEMA",
    "PLAN_SCHEMA_NAME",
    "REFERENT_KINDS",
    "Confidence",
    "DeleteScope",
    "Directive",
    "Intent",
    "InspectTarget",
    "Parameters",
    "Plan",
    "Referent",
    "RoutedBy",
    "WIRE_INTENTS",
    "plan_from_payload",
]

Intent = Literal[
    "help",
    "inspect",
    "create_schema",
    "query",
    "load_data",
    "execute",
    "corrective",
    "clear_data",
    "destroy",
    "export",
    "cancel",
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
    "activity",
    "state",
    "unspecified",
]

#: What a directive operates on when the user said "that" (D15).
#:
#: Deliberately a small closed enum of *kinds of prior output*, not a free-text
#: description: the resolution is a lookup in the activity log, so the model's
#: whole job is to name which shelf to look on. "Awesome -- what's the SQL for
#: that?" is ``referent="last_query"``, and the answer is the last successful
#: ``query.generate`` activity. No second model call, and nothing invented.
Referent = Literal["none", "last_query", "last_result", "last_schema", "last_data"]

#: What a ``destroy`` directive is aimed at (W18).
#:
#: One intent with a scope rather than two destructive intents, deliberately.
#: ``destroy`` and ``clear_data`` already sit next to each other in the enum and
#: W17 documented at length how easily a model conflates neighbouring
#: destructive labels; adding a third would make that worse, not better. A scope
#: instead makes "which did you mean?" *one* question with *one* answer -- and
#: ``"both"`` is then a value the enum can hold rather than a reply the system
#: has to refuse, which is exactly where Chris's session dead-ended.
#:
#: ``"unspecified"`` is not a failure. It is the honest answer whenever the
#: user named an object without saying whether they meant the design or the
#: instance built from it, and the orchestrator turns it into a question that
#: enumerates both blast radii.
DeleteScope = Literal["model", "database", "both", "unspecified"]

Confidence = Literal["high", "medium", "low"]

#: Who cut the utterance into directives. ``"model"`` is the router's LLM call;
#: ``"keyword"`` is ``t2s_nl.offline_router``, reached only when there is no
#: model to ask. A surface MUST make ``"keyword"`` visible -- a keyword match
#: presented as the model's judgement would be the system lying about its own
#: provenance, which is the one thing this package exists to prevent.
RoutedBy = Literal["model", "keyword"]

INTENTS: tuple[str, ...] = get_args(Intent)

#: The intents a destructive-confirmation gate stands in front of (W17). Neither
#: ever executes on the turn that asked for it.
DESTRUCTIVE_INTENTS: frozenset[str] = frozenset({"destroy", "clear_data"})

#: What the model is allowed to choose from. ``cancel`` is ours: it is how a
#: deterministic "no" to a pending confirmation is expressed, it is never
#: offered on the wire, and :func:`plan_from_payload` rejects it on arrival.
WIRE_INTENTS: tuple[str, ...] = tuple(i for i in INTENTS if i != "cancel")
_TARGETS: tuple[str, ...] = get_args(InspectTarget)
_REFERENTS: tuple[str, ...] = get_args(Referent)
_DELETE_SCOPES: tuple[str, ...] = get_args(DeleteScope)

PLAN_SCHEMA_NAME = "t2s_plan"

#: D15's cap. Four covers every compound utterance we have observed ("make a
#: database, load it, ask X, and run it" is exactly four) while keeping a
#: confused or hostile utterance from becoming a long chain of actions. Past it
#: the plan is refused whole -- see :func:`plan_from_payload`.
MAX_PLAN_DIRECTIVES = 4

#: Referent -> the activity kind whose latest successful row answers it (D14).
#: This table is the entire referent-resolution mechanism; it exists because the
#: activity log made "the previous thing of this sort" a query rather than a
#: guess.
REFERENT_KINDS: dict[str, str] = {
    "last_query": "query.generate",
    "last_result": "query.execute",
    "last_schema": "schema.generate",
    "last_data": "data.load",
}


class Parameters(BaseModel):
    """Slots the router fills in alongside an intent.

    All optional, all nullable: a router that is unsure of a parameter should
    leave it null and let the orchestrator ask, rather than invent one.
    """

    model_config = ConfigDict(extra="ignore")

    inspect_target: InspectTarget = "unspecified"
    table: str | None = Field(default=None, description="A table the user named, e.g. 'orders'.")
    model_ref: str | None = Field(
        default=None,
        description=(
            "A saved object the user named, by name or UUID -- a data model, or "
            "the sample database they identified by its model's name. This is how "
            "destroy/clear_data say *which* instance."
        ),
    )
    delete_scope: DeleteScope = Field(
        default="unspecified",
        description=(
            "Only meaningful when intent is 'destroy': whether the user meant the "
            "data model, the sample database built from it, or both. 'unspecified' "
            "when they did not say -- the orchestrator asks."
        ),
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


class Directive(BaseModel):
    """One atomic thing to do, extracted from part of an utterance (D15).

    MAIN.md's "Interpretations of Natural Language speech-acts" asked for "a
    structured list of atomic interpretations". This is one item of that list.
    """

    model_config = ConfigDict(extra="ignore")

    intent: Intent
    parameters: Parameters = Field(default_factory=Parameters)
    referent: Referent = "none"
    rationale: str = ""

    @property
    def needs_clarification(self) -> bool:
        return self.intent == "unknown"

    @property
    def referent_kind(self) -> str | None:
        """The activity kind this directive's referent resolves against."""
        return REFERENT_KINDS.get(self.referent)


class Plan(BaseModel):
    """The router's ruling on one utterance: an ordered list of directives.

    ``refusal`` is set by us, never by the model: it is how an over-long or
    structurally unusable plan reports that *nothing was executed*, which is
    the difference between refusing and silently truncating.

    ``routed_by`` is likewise ours. It is **not** in :data:`PLAN_SCHEMA` and
    :func:`plan_from_payload` never reads it off the wire, so a model cannot
    claim a provenance it does not have; the only way to get ``"keyword"`` is
    for ``t2s_nl.offline_router`` to have built the plan itself.
    """

    model_config = ConfigDict(extra="ignore")

    directives: list[Directive] = Field(default_factory=list)
    confidence: Confidence = "medium"
    clarifying_question: str | None = None
    refusal: str | None = None
    routed_by: RoutedBy = "model"
    #: The one line a surface must show on every turn this plan produced, when
    #: the plan was not the model's work. Set by ``t2s_nl.offline_router``, which
    #: is the only place that knows *why* there was no model to ask.
    routing_note: str | None = None
    #: Things that happened *while routing* which the user has to be told about,
    #: shown on the first turn of the plan. W17's case is the only one so far and
    #: it is not optional: a pending destruction that this utterance invalidated
    #: must be reported, or a later "yes" silently does nothing and the user has
    #: no way to know why. Ours, never on the wire.
    notes: list[str] = Field(default_factory=list)

    @property
    def primary(self) -> Directive:
        """The first directive; an ``unknown`` one when the plan is empty."""
        return self.directives[0] if self.directives else Directive(intent="unknown")

    @property
    def is_single(self) -> bool:
        return len(self.directives) == 1

    @property
    def intents(self) -> tuple[str, ...]:
        return tuple(d.intent for d in self.directives)


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
                    "'queries' means SQL already produced in this conversation; "
                    "'activity' means the log of what THIS system has been doing during "
                    "this conversation and how long each step took -- 'what have you been "
                    "doing', 'why is that so slow', 'show me the log', 'what did you just "
                    "do'. "
                    "'sessions' means OTHER conversations in this project, and is rarely "
                    "what is meant. "
                    "'state' means 'where am I / what am I working on'. "
                    "Use 'unspecified' otherwise."
                ),
            },
            "table": {"type": ["string", "null"]},
            "model_ref": {
                "type": ["string", "null"],
                "description": (
                    "The saved object the user named, in their own words -- 'the sports "
                    "league database', 'the sports league model', 'bookstore'. Required "
                    "for destroy and clear_data whenever the user named one; null when "
                    "they said only 'it' or 'this one', which the orchestrator resolves "
                    "from the conversation. Copy their words including any 'model' or "
                    "'database' in them; a separate deterministic component matches the "
                    "name and 'parameters.delete_scope' carries which of the two they "
                    "meant."
                ),
            },
            "delete_scope": {
                "type": "string",
                "enum": list(_DELETE_SCOPES),
                "description": (
                    "Only for intent 'destroy'. What the user wants deleted. "
                    "'database' = the sample DATABASE INSTANCE built from a model: the "
                    "file and its rows go, the design survives and another database can "
                    "be built from it. Use when they say 'database', 'db', 'instance', "
                    "or name one by its id. "
                    "'model' = the DATA MODEL itself -- the design. Deleting it also "
                    "deletes every version, schema, dataset and sample database derived "
                    "from it, so it is strictly the larger of the two. Use when they say "
                    "'model', 'data model', 'design', or 'the whole thing'. "
                    "'both' = they explicitly asked for the model AND its database, or "
                    "answered a question with 'both', 'everything', 'all of it'. "
                    "'unspecified' = they named an object but not which of the two they "
                    "meant ('delete the sports league one', 'get rid of sports league'). "
                    "Do NOT guess: 'unspecified' makes the system ask, and asking costs a "
                    "turn while guessing costs their data. Use 'unspecified' for every "
                    "intent other than 'destroy'."
                ),
            },
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
        "required": [
            "inspect_target",
            "table",
            "model_ref",
            "delete_scope",
            "row_count",
            "seed",
            "text",
        ],
        "additionalProperties": False,
    }


def _directive_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": list(WIRE_INTENTS),
                "description": (
                    "What this directive asks the system to do. "
                    "'help' = a question about this system. "
                    "'inspect' = a question about the user's own saved objects. "
                    "'create_schema' = design or revise a data model and its DDL. "
                    "'query' = turn a question about the DATA into SQL. "
                    "'load_data' = generate sample rows and load them. "
                    "'execute' = run SQL against the loaded sample database. "
                    "'corrective' = record a durable domain fact. "
                    # The two destructive ones are the pair most at risk of being
                    # conflated, and conflating them is exactly what produced the
                    # bad turn W17 exists to fix ("clear out ... just totally
                    # delete it" offered both readings in one sentence). State
                    # the difference in terms of what SURVIVES, not in terms of
                    # the verb, because the verbs overlap in English and the
                    # outcomes do not.
                    "'clear_data' = DELETE THE ROWS ONLY from a sample database: "
                    "the database instance, its tables and its schema all survive "
                    "and it can be reloaded. Use for 'empty it', 'wipe the data', "
                    "'truncate the tables', 'clear out the rows', 'start again with "
                    "no data'. "
                    "'destroy' = DELETE SOMETHING FOR GOOD -- either the sample "
                    "DATABASE INSTANCE (the file and its rows go; the design "
                    "survives and another database can be built from it) or the "
                    "DATA MODEL itself (the design, and with it every version, "
                    "schema, dataset and sample database derived from it). Which "
                    "of the two goes in 'parameters.delete_scope'; the intent is "
                    "the same either way. Use for 'delete the bookstore database', "
                    "'delete the sports model', 'drop it', 'get rid of it "
                    "entirely', 'blow it away'. "
                    "If the user's words fit both 'clear_data' and 'destroy' and "
                    "nothing settles it, choose 'unknown' and ask which they mean "
                    "-- do not guess, because one of the two cannot be undone. "
                    "(Model-versus-database is NOT that case: it is a scope, so "
                    "stay on 'destroy' and set delete_scope to 'unspecified'.) "
                    "'unknown' = you cannot tell; ask instead of guessing."
                ),
            },
            "parameters": _param_schema(),
            "referent": {
                "type": "string",
                "enum": list(_REFERENTS),
                "description": (
                    "What this directive operates on when the user referred to earlier "
                    "output with a pronoun. 'last_query' = the SQL most recently written; "
                    "'last_result' = the rows most recently returned; 'last_schema' = the "
                    "DDL most recently designed; 'last_data' = the sample data most "
                    "recently loaded. Use 'none' when the directive names its own subject. "
                    "Within one plan, a directive that acts on the previous directive's "
                    "output also uses these -- 'and show me the results' after a query is "
                    "'last_query'."
                ),
            },
            "rationale": {
                "type": "string",
                "description": "One short clause naming the cue you classified on.",
            },
        },
        "required": ["intent", "parameters", "referent", "rationale"],
        "additionalProperties": False,
    }


#: Sent as ``response_format`` (D7). ``strict: true`` requires every property in
#: ``required`` and ``additionalProperties: false``, which is why the nullable
#: slots are typed ``["string", "null"]`` rather than omitted -- same discipline
#: as ``t2s_core.wire``. ``maxItems`` states the cap on the wire; the cap is
#: enforced again in :func:`plan_from_payload`, because a wire constraint the
#: provider chooses not to honour must not become our only guard.
PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "directives": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_PLAN_DIRECTIVES,
            "items": _directive_schema(),
            "description": (
                "The atomic things the user asked for, in the order they should happen. "
                "Most utterances are ONE directive. Emit more only when the utterance "
                "genuinely asks for more than one thing."
            ),
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "clarifying_question": {
            "type": ["string", "null"],
            "description": (
                "The single question to put to the user. Required when any directive is "
                "'unknown'; null otherwise."
            ),
        },
    },
    "required": ["directives", "confidence", "clarifying_question"],
    "additionalProperties": False,
}


def plan_from_payload(raw: str) -> Plan:
    """Parse the router's JSON into a plan, degrading rather than raising.

    inference-findings: enforced structured output guarantees *shape*, never
    *meaning*. Everything that can still go wrong lands on a one-directive
    ``unknown`` plan with a clarifying question, because in a chat loop "I'm not
    sure what you meant -- did you want X?" is always a better turn than a
    traceback:

    * the content is not JSON at all (truncation; see finding #1);
    * ``directives`` is missing, empty, or not a list;
    * every ``intent`` in it is a string the enum does not contain.

    Two outcomes are *not* degradation and are treated differently:

    * a plan longer than :data:`MAX_PLAN_DIRECTIVES` is **refused whole**;
    * a directive with a good intent but one bad optional parameter keeps the
      intent and drops only the offending field (models routinely fill
      irrelevant slots with placeholder zeros).
    """
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _undecided("I could not parse that. Could you rephrase what you'd like me to do?")

    if not isinstance(payload, dict):
        return _undecided("I could not parse that. Could you rephrase what you'd like me to do?")

    raw_directives = payload.get("directives")
    if not isinstance(raw_directives, list) or not raw_directives:
        return _undecided(
            "I'm not sure what you'd like me to do. Are you asking about your saved "
            "objects, describing a schema to build, or asking a question of the data?"
        )

    if len(raw_directives) > MAX_PLAN_DIRECTIVES:
        # Refuse, never truncate (D15). Executing the first four of seven is
        # how a misreading becomes a chain of unintended actions.
        return Plan(
            directives=[],
            confidence="low",
            refusal=(
                f"That reads as {len(raw_directives)} separate things to do, and I run at "
                f"most {MAX_PLAN_DIRECTIVES} in one go. Nothing was done. Could you split "
                "it into a couple of messages?"
            ),
        )

    directives = [d for d in (_directive_from(item) for item in raw_directives) if d is not None]
    if not directives:
        return _undecided(
            "I'm not sure what you'd like me to do. Are you asking about your saved "
            "objects, describing a schema to build, or asking a question of the data?"
        )

    question = payload.get("clarifying_question")
    confidence = payload.get("confidence")
    degraded = len(directives) != len(raw_directives) or any(
        d.rationale == "parameters discarded" for d in directives
    )
    return Plan(
        directives=directives,
        confidence=cast(
            "Confidence",
            "low" if degraded or confidence not in ("high", "medium", "low") else confidence,
        ),
        clarifying_question=question if isinstance(question, str) and question else None,
    )


def _directive_from(item: object) -> Directive | None:
    """One directive, or ``None`` when it is beyond salvage."""
    if not isinstance(item, dict):
        return None
    raw_intent = item.get("intent")
    if not isinstance(raw_intent, str) or raw_intent not in WIRE_INTENTS:
        # WIRE_INTENTS, not INTENTS: `cancel` is not offered on the wire and is
        # not accepted off it either. A model that emits it -- by echoing this
        # docstring, by hallucination, or because someone talked it into one --
        # gets the same treatment as any other unrecognised label.
        return None
    intent = cast("Intent", raw_intent)

    try:
        return Directive.model_validate(item)
    except ValidationError as first_error:
        # One bad optional parameter must not cost us the good ones. Models
        # routinely fill irrelevant fields with placeholder zeros -- a
        # create_schema directive arriving with `row_count: 0` is the observed
        # case -- and discarding the whole block there threw away the schema
        # description we actually needed. Drop only the offending fields and
        # revalidate; a still-invalid payload keeps the intent and nothing else.
        pruned = _prune_invalid_parameters(item, first_error)
        if pruned is not None:
            try:
                salvaged = Directive.model_validate(pruned)
            except ValidationError:
                pass
            else:
                return salvaged.model_copy(update={"rationale": "parameters discarded"})
        return Directive(intent=intent, rationale="parameters discarded")


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


def _undecided(question: str) -> Plan:
    return Plan(
        directives=[Directive(intent="unknown", rationale="router output was unusable")],
        confidence="low",
        clarifying_question=question,
    )
