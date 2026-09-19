"""Routing an utterance with ordinary code, for when there is no model to ask.

``T2S_OFFLINE=1`` replays recorded fixtures (D8). That works beautifully for the
utterances somebody captured and not at all for anything else, because fixture
coverage of open-ended natural language is unbounded by construction. A demo
that survives bad wifi, a rotated key or an interviewer's laptop only if the
interviewer types one of twenty-four exact sentences is not a demo that
survives anything.

So: when the recorded client has no fixture -- or there is no API key at all --
classify the utterance here instead of failing. This is Chris's rule of thumb #3
("prefer agents build deterministic tooling rather than always flowing through
context") applied exactly, and it is tractable for precisely one reason: **the
router's output space is a small fixed enum**. Picking one of eight intents and
one of eleven inspect targets is a job keyword and pattern matching can do
honestly. It is not a general natural-language understanding problem and we do
not pretend it is one.

Three rules define the boundary, and the third is the important one:

1. **Fixtures win.** :func:`classify` is only ever reached after the recorded
   client has been tried and has said it has nothing (``FixtureNotFound``).
   ``t2s_nl.router`` owns that ordering.
2. **It says what it did.** Every plan this module produces carries
   ``routed_by="keyword"``; the orchestrator turns that into a visible note on
   every turn (:data:`OFFLINE_ROUTING_NOTE`). The system never pretends a
   keyword match was a model's judgement.
3. **Deterministic routing, never deterministic generation.** ``inspect``,
   ``help``, ``execute`` and ``load_data`` all end in a deterministic read or a
   deterministic action, so routing them is the whole job. ``query``,
   ``create_schema`` and ``corrective`` end in *text a model has to write*. A
   keyword router that emitted SQL would be fabricating, so this one refuses
   and says what it would need. There is no wording of this module's rules
   under which it produces a SELECT statement.

The refusal is a :attr:`Plan.refusal`, which is the shape D15 already defined
for "nothing was executed" -- an over-long plan and an ungeneratable one are the
same kind of answer, and they get the same handling in ``execute_plan``.

**W17 adds two things this module must be honest about.**

*Destructive intents are routable here, and that is safe*, because routing
``destroy`` does not destroy anything: it produces a description of what would
be lost and a question. The confirmation itself is read by
``t2s_nl.confirmation``, which is keyword matching too -- so the destructive
lifecycle is one of the few flows that works *identically* with and without a
model. What this module refuses to do is guess between ``destroy`` and
``clear_data`` when an utterance supports both readings; it asks, exactly as the
router prompt tells the model to.

*W18 adds model deletion to the same table, and the same honesty applies.*
``destroy`` now covers the data model as well as the sample database, and which
one is a ``delete_scope`` on the directive. When only one cue family fires this
module fills it in; when both do it writes ``"unspecified"`` and lets the
orchestrator ask, because the two readings differ by an entire database and
there is no keyword technique that picks between them honestly.

*It has no conversational memory and does not pretend to.* ``RouterContext`` now
carries recent turns (``t2s_nl.history``) for the model's benefit. This module
**never reads them** -- there is no keyword technique for "the one I already
mentioned" that is not a guess dressed up as a feature, and a wrong guess here
picks which database to delete. An utterance that leans on earlier conversation
gets :data:`NO_HISTORY_QUESTION`: a plain statement that keyword routing cannot
resolve back-references, and a request to name the thing. A test pins the
property by classifying every corpus utterance with and without history and
asserting the two are identical.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from t2s_nl.intents import (
    MAX_PLAN_DIRECTIVES,
    DeleteScope,
    Directive,
    InspectTarget,
    Intent,
    Parameters,
    Plan,
    Referent,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle; annotations are strings
    from t2s_nl.router import RouterContext

__all__ = [
    "DEFAULT_REASON",
    "GENERATION_INTENTS",
    "KEYLESS_ROUTING_NOTE",
    "NO_HISTORY_QUESTION",
    "OFFLINE_ROUTING_NOTE",
    "classify",
]

#: What a surface shows when a turn was routed by this module. Rendered as a
#: note, i.e. "· offline: routed by keyword, not by the model". It rides on the
#: plan (``Plan.routing_note``) rather than being re-derived downstream, so the
#: thing that knows *why* there was no model is the thing that words it.
OFFLINE_ROUTING_NOTE: Final = "offline: routed by keyword, not by the model"
KEYLESS_ROUTING_NOTE: Final = "no API key: routed by keyword, not by the model"

DEFAULT_REASON: Final = "no language model is available"

#: The three intents whose product is text a model must write. Routing to them
#: is fine; *performing* them without a model is fabrication, so a plan
#: containing one is refused whole. See the module docstring, rule 3.
_GENERATION_LABELS: Final[dict[str, str]] = {
    "query": "write SQL for a question",
    "create_schema": "design a schema",
    "corrective": "record a corrective in your words and re-ask your last question",
}
GENERATION_INTENTS: Final[frozenset[str]] = frozenset(_GENERATION_LABELS)


def _compile(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


# ---------------------------------------------------------------------------
# Cue tables. Ordered, first match wins, most specific first. Every entry is a
# phrase we have actually seen in scenarios.ROUTER_CASES, MONEY_PATH, the chat
# transcripts, or Chris's own examples -- this is a table of observed English,
# not a table of imagined English.
# ---------------------------------------------------------------------------

_HELP = _compile(
    r"^\s*/?help\b",
    r"\bwhat can (you|i|this|it)\b",
    r"\bhow (do|can) i\b",
    r"\bhow does (this|it|the system|that) work\b",
    r"\bwhat (do|are) you (do|for)\b",
    r"\bwhat (commands|options) ",
)

_EXECUTE = _compile(
    r"^\s*run\b",
    r"\brun (it|that|this|the query|the sql|them)\b",
    r"\bexecute\b",
    r"\b(show|give) me the results\b",
    r"\bwhat (are|were) the results\b",
    r"\bgo ahead and run\b",
)

#: `export` copies a file and says where it went -- no model, no generation, so
#: it is routable here like `inspect` and `execute`. It belongs with the intent:
#: a value in the enum the keyword router cannot reach is a value that silently
#: stops working the moment there is no key.
_EXPORT = _compile(
    r"\bexport\b",
    r"\b(save|write|give|get|hand) (me )?(a |the )?(copy|snapshot|dump|file)\b",
    r"\b(copy|snapshot) of (the|this|that|my) (database|db|data)\b",
    r"\b(db browser|datagrip|sqlite3 shell|my own tools?)\b",
    r"\bas a (sqlite )?file\b",
)

_LOAD_DATA = _compile(
    r"\b(load|populate|fill|seed)\b[^.]*\b(data|rows|it|them|database|db|tables?)\b",
    r"\bsample data\b",
    r"\b(create|make|build|spin up)\s+(me\s+)?(a\s+|the\s+)?(sample\s+)?(database|db)\b",
    r"\bwith about \d+ rows?\b",
)

#: "Awesome -- what's the SQL for that?" -- a question about prior output, which
#: D15 resolves from the activity log (``referent="last_query"``) with no model
#: involved at all. Every pattern here requires a *pronoun*: "show me the SQL for
#: unpaid balances" names a new question and must stay a generation request.
_LAST_QUERY = _compile(
    r"\b(the )?sql (for|behind|of) (that|it|this|the last (one|query))\b",
    r"\bwhat'?s the (sql|query)\s*\??$",
    r"\bshow me (that|the last) (sql|query)\b",
)

#: An ``inspect`` cue only counts inside a question about the user's own stuff.
#: Without this guard "I want to **model** a small bookstore" matches the
#: ``models`` cue and becomes a catalogue read instead of a schema request --
#: the one misclassification that would produce a confidently wrong answer
#: rather than a refusal.
_INSPECT_FRAME = re.compile(
    r"\b(what|which|who|show|list|display|tell me|remind me|do i have|have i|"
    r"where am i|where are we|got any|any\b)",
    re.IGNORECASE,
)

_INSPECT_RULES: Final[tuple[tuple[InspectTarget, tuple[re.Pattern[str], ...]], ...]] = (
    (
        "activity",
        _compile(
            r"\bactivit(y|ies)\b",
            r"\blogs?\b",
            r"\bwhat (have|'ve) you been (doing|up to|working on)\b",
            r"\bwhat have you done\b",
            r"\bwhat (did|have) you (just )?(do|done)\b",
            r"\bwhat are you doing\b",
            r"\bwhat you'?ve (been )?(doing|done)\b",
            r"\bhow long (did|does|has)\b",
            r"\bwhy (did|was|is) (that|it|this)\b",
            r"\btimings?\b",
        ),
    ),
    (
        "correctives",
        _compile(
            r"\bcorrectives?\b",
            r"\bwhat (have|'ve) i told you\b",
            r"\bwhat do you know about\b",
            r"\bwhat have i corrected\b",
        ),
    ),
    (
        "sessions",
        _compile(
            r"\bsessions?\b",
            r"\bother conversations?\b",
        ),
    ),
    (
        "queries",
        _compile(
            r"\bqueries\b",
            r"\bwhat (have|'ve) i asked\b",
            r"\bquestions (have )?i\b",
            r"\b(sql|queries) (have|did) (i|we)\b",
            r"\bmy (past |previous |earlier )?(sql|questions)\b",
        ),
    ),
    (
        "sample_rows",
        _compile(
            r"\bsample rows\b",
            r"\brows\b",
            r"\bsome data (from|in)\b",
            r"\bdata (from|in) (the )?\w+\b",
            r"\bwhat'?s in (the )?\w+\b",
        ),
    ),
    (
        "schemas",
        _compile(r"\bschemas\b"),
    ),
    (
        "schema_detail",
        _compile(
            r"\bschema\b",
            r"\bddl\b",
            r"\bcolumns?\b",
            r"\bwhat tables\b",
            r"\bmy tables\b",
            r"\btable (structure|definition|layout)\b",
        ),
    ),
    (
        "databases",
        _compile(
            r"\bdatabases\b",
            r"\b(sample )?database\b",
            r"\bdbs?\b",
        ),
    ),
    (
        "models",
        _compile(
            r"\b(data )?models?\b",
            r"\bdesigns?\b",
            r"\bwhat have i modell?ed\b",
        ),
    ),
    (
        "state",
        _compile(
            r"\bwhere (am i|are we)\b",
            r"\bwhat am i (working on|doing|up to)\b",
            r"\b(current|my) state\b",
            r"\bstatus\b",
            r"\bwhat'?s going on\b",
        ),
    ),
)

#: W17. Utterances whose subject lives in an earlier turn. The model gets a
#: bounded history block and can resolve these; this module gets nothing and
#: says so. Checked before everything else, because "I already mentioned it -
#: delete it" would otherwise route to a destructive action whose target we
#: would have to invent.
_HISTORY_REFERENCE = _compile(
    r"\bi (already |just )?(mentioned|said|told you)\b",
    r"\b(like|as) i (said|mentioned|told you)\b",
    r"\b(the|that) one i (mentioned|said|named|asked about)\b",
    r"\bdo(n'?t| not)? you (not )?(have|remember|recall)\b",
    r"\bthat context\b",
    r"\bsame (one|thing) as (before|last time)\b",
)

NO_HISTORY_QUESTION: Final = (
    "I'm routing by keyword rather than by the model ({reason}), and keyword matching "
    "has no memory of earlier turns — I can see what you just typed and nothing else. "
    "I won't guess which thing you meant, so please name it: the data model's name, or "
    "the database's id from /dbs."
)

#: The instance itself goes, and the utterance **names** it. Every pattern pairs
#: a destructive verb with a word that means "the database", so "delete the rows"
#: never lands here.
_DESTROY = _compile(
    r"\b(delete|destroy|drop|remove|nuke|obliterate|trash|scrap)\b[^.]{0,40}?"
    r"\b(database|databases|db|dbs|instance)\b",
)

#: Destructive, and names nothing: "blow it away", "get rid of it", "delete
#: that". Kept apart from :data:`_DESTROY` since W18, because these say only
#: *that* something should go and not *what* -- so they must not count as
#: evidence for "the database" when the same sentence names a model ("get rid
#: of that data model"). They still default to the database when nothing else
#: fires, which is W17's behaviour and the session's current pointer.
_DESTROY_BARE = _compile(
    r"\b(blow (it |that )?away|tear (it |that )?down|get rid of)\b",
    r"\b(delete|destroy|drop|remove) (it|that|this|them)\b",
    r"\btotally delete\b",
)

#: W18. The same destructive verbs paired with a word that means "the design".
#: Routing one of these destroys nothing -- it produces a description of the
#: whole cascade and a question -- so keyword-routing it is exactly as safe as
#: keyword-routing a database destroy, and leaving it out would mean the newest
#: and largest deletion is the one that silently stops working with no key.
_DESTROY_MODEL = _compile(
    r"\b(delete|destroy|drop|remove|nuke|obliterate|trash|scrap)\b[^.]{0,40}?"
    r"\b(data ?models?|models?|designs?)\b",
    r"\b(get rid of|blow away)\b[^.]{0,40}?\b(data ?models?|models?|designs?)\b",
)

#: The rows go, the instance stays.
_CLEAR_DATA = _compile(
    r"\b(clear|empty|wipe|truncate|purge|flush|blank)\b[^.]{0,40}?"
    r"\b(data|rows|tables?|database|db|it|out)\b",
    r"\b(delete|remove|drop) (all (of )?)?(the )?(rows|data|records)\b",
    r"\bstart (over|again|fresh)\b[^.]{0,20}?\b(no|without|empty) data\b",
)

_CORRECTIVE = _compile(
    r"^\s*actually\b",
    r"\bactually,",
    r"\b(remember|note|keep in mind) that\b",
    r"\bfor the record\b",
    r"\bfrom now on\b",
    r"\bbear in mind\b",
)

_CREATE_SCHEMA = _compile(
    r"\bi want to (model|design|build)\b",
    r"\blet'?s (model|design|build)\b",
    r"\b(model|design|build|create|make|sketch|define) (me )?(a|an|the|some)\b",
    r"\badd (a|an|another)\b[^.]*\b(table|column|field|entity)\b",
    r"\b(schema|data model) for\b",
    r"\bwith (columns|fields)\b",
)

_QUERY = _compile(
    r"\bhow (many|much)\b",
    r"\bcount of\b",
    r"\b(average|sum|total|median|revenue)\b",
    r"\b(top|bottom) \d+\b",
    r"\b(most|least|highest|lowest|biggest|largest)\b",
    r"\bper (day|week|month|year|quarter|customer|order|table)\b",
    r"\blast (day|week|month|year|quarter)\b",
    r"\b(write|give) (me )?(a |the )?(sql|query)\b",
    r"\b(sql|query) for\b",
    r"\bgroup(ed)? by\b",
)

# ---------------------------------------------------------------------------
# Parameter extraction. Deliberately narrow: an unfilled slot makes the
# orchestrator use its default or ask, which is always better than a wrong one.
# ---------------------------------------------------------------------------

_ROW_COUNT = _compile(
    r"\b(?:about|around|roughly|approximately|with)?\s*(\d{1,6})\s*rows?\b",
    r"\b(?:about|around|roughly|approximately)\s+(\d{1,6})\b",
)
_SEED = re.compile(r"\bseed(?:ed)?\s*(?:=|:|is|of)?\s*(\d{1,9})\b", re.IGNORECASE)
_TABLE_AFTER = re.compile(
    r"\b(?:from|in|of|for|table|about)\s+(?:the\s+|my\s+|a\s+|an\s+)?([a-z_][a-z0-9_]*)",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z_][a-z0-9_]*")

#: Words that follow "from"/"in" without naming a table. Without this,
#: "show me some rows from the database" yields the table ``database``.
_TABLE_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a", "all", "an", "any", "data", "database", "databases", "db", "dbs", "each",
        "every", "here", "it", "last", "model", "models", "my", "row", "rows", "sample",
        "schema", "schemas", "some", "table", "tables", "that", "the", "them", "there",
        "these", "this", "those", "week", "year", "month", "day", "session", "sessions",
    }
)  # fmt: skip

#: ``Parameters.row_count`` is validated ``1 <= n <= 10_000``; a number outside
#: that is a misread, not a request, and is dropped rather than clamped.
_MIN_ROWS: Final = 1
_MAX_ROWS: Final = 10_000

_CONNECTOR = re.compile(
    r"\s*[,;]?\s*\b(?:and then|and also|and after that|after that|then)\b\s*",
    re.IGNORECASE,
)

_PRONOUN = re.compile(r"\b(that|it|this|them|the query|the sql)\b", re.IGNORECASE)

#: Internal sentinels. A ``_Match`` carrying one of these is an honest question,
#: not a classification, and :func:`classify` renders it with the reason there is
#: no model -- which only the caller knows.
_HISTORY_MARKER: Final = "__history__"

_AMBIGUOUS_QUESTION: Final = (
    "That reads two ways and I won't pick for you: do you want the sample database "
    "EMPTIED (the rows go, the database stays and can be reloaded), or DESTROYED (the "
    'database instance itself goes)? One of those cannot be undone. Say "empty it" or '
    '"destroy it".'
)

#: The name in front of "database"/"db"/"instance" -- "the sports league database"
#: -> "sports league". Narrow on purpose: an unfilled ``model_ref`` makes the
#: orchestrator fall back to the session's current database or ask, and both are
#: better than confidently naming the wrong one.
_NAMED_OBJECT = re.compile(
    r"\b(?:the|my|that|this)?\s*([a-z0-9][a-z0-9 '_-]{0,40}?)"
    r"\s+(?:database|db|instance|data model|model|design)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _Match:
    """One segment's classification, plus the literal cue it matched on.

    ``question`` is set when the honest answer is a question rather than an
    intent -- an utterance that leans on earlier turns, or one that reads as
    both ``destroy`` and ``clear_data``. It rides on the match instead of being
    re-derived later so that the thing which *found* the ambiguity is the thing
    that words it.
    """

    intent: Intent
    cue: str
    parameters: Parameters = field(default_factory=Parameters)
    referent: Referent = "none"
    question: str | None = None


#: The dispatch order, as data rather than as a ladder of ``if``s, so that the
#: precedence *is* readable: whichever cue table matches first wins, and adding
#: an intent means adding a row. Order is the whole design here --
#: ``load_data`` must precede ``inspect`` ("fill the database with about 25 rows
#: per table" contains both a database cue and a rows cue and is neither
#: question), and ``inspect`` must precede the generation cues ("what models do
#: I have" contains the word ``model``).
_STAGES: Final[tuple[tuple[tuple[re.Pattern[str], ...], Callable[[str, str], _Match]], ...]] = (
    (_HELP, lambda segment, cue: _Match("help", cue)),
    (_EXPORT, lambda segment, cue: _Match("export", cue)),
    (
        _EXECUTE,
        lambda segment, cue: _Match(
            "execute", cue, referent="last_query" if _PRONOUN.search(segment) else "none"
        ),
    ),
    (
        _LAST_QUERY,
        lambda segment, cue: _Match(
            "inspect", cue, Parameters(inspect_target="queries"), referent="last_query"
        ),
    ),
    (
        _LOAD_DATA,
        lambda segment, cue: _Match(
            "load_data", cue, Parameters(row_count=_row_count(segment), seed=_seed(segment))
        ),
    ),
)

#: Tried last, and only to *name* what was asked for: every one of these ends in
#: a refusal (rule 3). Ordered corrective → create_schema → query, weakest cues
#: last, because ``_QUERY`` is the broadest table in the module.
_GENERATION_STAGES: Final[tuple[tuple[tuple[re.Pattern[str], ...], Intent], ...]] = (
    (_CORRECTIVE, "corrective"),
    (_CREATE_SCHEMA, "create_schema"),
    (_QUERY, "query"),
)


def classify(
    utterance: str,
    *,
    context: RouterContext | None = None,
    reason: str = DEFAULT_REASON,
    note: str = OFFLINE_ROUTING_NOTE,
) -> Plan:
    """Route one utterance with keywords alone. Never raises, never calls out.

    ``reason`` is why there is no model (no fixture / no key); it appears
    verbatim in the refusal and the clarifying question, because "I can't do
    that" without "and here is what I would need" is a dead end. ``note`` is the
    one line every resulting turn carries so the user knows who did the routing.
    """
    text = _normalise(utterance)
    if not text:
        return _undecided(reason, note)

    segments = _split(text)
    if len(segments) > MAX_PLAN_DIRECTIVES:
        # Same rule as D15's cap: refused whole, never truncated.
        return _refused(
            f"That reads as {len(segments)} separate things to do, and I run at most "
            f"{MAX_PLAN_DIRECTIVES} in one go. Nothing was done.",
            note,
        )

    matches = [_classify(segment, context) for segment in segments]
    if asked := next((m.question for m in matches if m.question), None):
        # W17: a question beats every other reading of the utterance. Both cases
        # that produce one are cases where continuing would mean guessing at
        # which object to delete.
        return _asking(
            NO_HISTORY_QUESTION.format(reason=reason) if asked == _HISTORY_MARKER else asked,
            note,
        )
    if len(matches) > 1 and any(m.intent == "unknown" for m in matches):
        # "Support multi-directive splits where unambiguous." A split that
        # leaves a piece we cannot name is exactly the ambiguous case, so we
        # back out of the split and read the sentence whole.
        matches = [_classify(text, context)]

    ungeneratable = [m.intent for m in matches if m.intent in GENERATION_INTENTS]
    if ungeneratable:
        return _refused(_generation_refusal(ungeneratable, reason), note)

    if all(m.intent == "unknown" for m in matches):
        return _undecided(reason, note)

    return Plan(
        directives=[
            Directive(
                intent=m.intent,
                parameters=m.parameters,
                referent=m.referent,
                rationale=f"keyword router matched {m.cue!r}",
            )
            for m in matches
        ],
        # Never "high": a keyword match is a good guess, not a judgement, and
        # the confidence field is where that has to be honest.
        confidence="medium",
        routed_by="keyword",
        routing_note=note,
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
def _normalise(utterance: str) -> str:
    return re.sub(r"\s+", " ", utterance.replace("’", "'")).strip()


def _split(text: str) -> list[str]:
    parts = [p.strip(" ,;.") for p in _CONNECTOR.split(text)]
    return [p for p in parts if p] or [text]


def _classify(segment: str, context: RouterContext | None) -> _Match:  # noqa: PLR0911
    """One segment → one intent. Ordered: the guards that must win, first.

    ``load_data`` precedes ``inspect`` because "fill the database with about 25
    rows per table" contains both a database cue and a rows cue and is neither
    question. ``inspect`` precedes the generation cues because "what models do I
    have" contains the word ``model``.

    W17 puts two guards ahead of all of it, and both answer with a *question*:

    * a back-reference to an earlier turn, which this module cannot see;
    * an utterance that reads as both ``destroy`` and ``clear_data``.

    Both could be guessed at. Neither should be, because both guesses end at the
    same place -- deleting something the user did not mean.

    ``context`` is used only for table-name extraction. ``context.recent`` is
    never read here; see the module docstring.
    """
    if cue := _first(_HISTORY_REFERENCE, segment):
        return _Match("unknown", cue, question=_HISTORY_MARKER)

    named_database = _first(_DESTROY, segment)
    destroy_cue = named_database or _first(_DESTROY_BARE, segment)
    model_cue = _first(_DESTROY_MODEL, segment)
    clear_cue = _first(_CLEAR_DATA, segment)
    if (destroy_cue or model_cue) and clear_cue:
        return _Match(
            "unknown", f"{destroy_cue or model_cue} / {clear_cue}", question=_AMBIGUOUS_QUESTION
        )
    if destroy_cue or model_cue:
        # W18. Which of the two the user meant is a *scope*, not a separate
        # intent, and it is the one thing this module refuses to decide when
        # both cues fire: "delete the sports model and its database" gets
        # `unspecified`, which the orchestrator turns into a question that
        # enumerates both blast radii. That is a better answer than either guess
        # and it is available offline precisely because asking costs no model.
        scope = _delete_scope(model_cue, named_database)
        return _Match(
            "destroy",
            model_cue or destroy_cue,
            Parameters(model_ref=_named_object(segment), delete_scope=scope),
        )
    if clear_cue:
        return _Match("clear_data", clear_cue, Parameters(model_ref=_named_object(segment)))

    for patterns, build in _STAGES:
        if cue := _first(patterns, segment):
            return build(segment, cue)

    if _INSPECT_FRAME.search(segment):
        for target, patterns in _INSPECT_RULES:
            if cue := _first(patterns, segment):
                return _Match(
                    "inspect",
                    cue,
                    Parameters(inspect_target=target, table=_table(segment, context)),
                )

    for patterns, intent in _GENERATION_STAGES:
        if cue := _first(patterns, segment):
            return _Match(intent, cue, Parameters(text=segment))

    if _INSPECT_FRAME.search(segment) or segment.endswith("?"):
        # It is shaped like a question and no deterministic read answers it, so
        # it is a question of the *data* -- which needs a model. Refusing here
        # is a better answer than "I don't understand", because it names what
        # is missing.
        return _Match("query", "a question with no deterministic answer", Parameters(text=segment))

    return _Match("unknown", "")


def _delete_scope(model_cue: str, named_database: str) -> DeleteScope:
    """Which of the two the utterance named, or "unspecified" when it named both.

    ``named_database`` is deliberately narrower than "a destructive cue fired".
    "Get rid of that data model" carries a bare destructive phrase *and* a model
    cue, and reading the bare phrase as evidence for "the database" would turn
    an unambiguous request into a question. Only an utterance that names a
    database outright can make the scope open.
    """
    if model_cue and named_database:
        return "unspecified"
    return "model" if model_cue else "database"


def _first(patterns: tuple[re.Pattern[str], ...], segment: str) -> str:
    for pattern in patterns:
        match = pattern.search(segment)
        if match:
            return match.group(0).strip()
    return ""


def _row_count(segment: str) -> int | None:
    for pattern in _ROW_COUNT:
        match = pattern.search(segment)
        if match:
            value = int(match.group(1))
            return value if _MIN_ROWS <= value <= _MAX_ROWS else None
    return None


def _seed(segment: str) -> int | None:
    match = _SEED.search(segment)
    return int(match.group(1)) if match else None


def _table(segment: str, context: RouterContext | None) -> str | None:
    """A table the user named. The session's own table names win when we have them.

    Matching against ``context.table_names`` first is what makes "show me some
    sample rows from orders" resolve to ``orders`` and not to whatever noun
    happens to follow "from" -- and it is free, because those names are already
    in the router context for the model's benefit.
    """
    tokens = set(_WORD.findall(segment.lower()))
    for name in context.table_names if context else ():
        lowered = name.lower()
        if lowered in tokens or lowered.rstrip("s") in tokens or f"{lowered}s" in tokens:
            return name
    match = _TABLE_AFTER.search(segment)
    if match:
        candidate = match.group(1).lower()
        if candidate not in _TABLE_STOPWORDS:
            return candidate
    return None


#: Words that can sit between a destructive verb and the word "database" without
#: being part of anything's name. The verbs are here because the regex has to
#: start loose enough to catch "delete the sports league database" and therefore
#: also catches "delete".
_OBJECT_NOISE: Final[frozenset[str]] = frozenset(
    {
        "blow", "clear", "delete", "design", "destroy", "drop", "empty", "flush", "get",
        "nuke", "obliterate", "of", "out", "please", "purge", "remove", "rid", "scrap",
        "totally", "trash", "truncate", "wipe",
    }
) | _TABLE_STOPWORDS  # fmt: skip


def _named_object(segment: str) -> str | None:
    """The name in front of "database", with the verbs and articles taken off.

    Returns ``None`` rather than a guess whenever nothing is left -- "drop that
    database" names nothing, and the orchestrator resolving that against the
    session's current database is right, while inventing the name "drop" is a
    lookup that fails or, worse, matches.
    """
    match = _NAMED_OBJECT.search(segment)
    if not match:
        return None
    name = " ".join(w for w in match.group(1).split() if w.lower() not in _OBJECT_NOISE).strip()
    return name or None


def _asking(question: str, note: str) -> Plan:
    """An ``unknown`` plan whose clarifying question is the whole answer."""
    return Plan(
        directives=[Directive(intent="unknown", rationale="keyword router will not guess")],
        confidence="low",
        routed_by="keyword",
        routing_note=note,
        clarifying_question=question,
    )


def _refused(refusal: str, note: str) -> Plan:
    return Plan(
        directives=[],
        confidence="low",
        refusal=refusal,
        routed_by="keyword",
        routing_note=note,
    )


def _undecided(reason: str, note: str) -> Plan:
    return Plan(
        directives=[Directive(intent="unknown", rationale="keyword router matched nothing")],
        confidence="low",
        routed_by="keyword",
        routing_note=note,
        clarifying_question=(
            f"I'm routing by keyword rather than by the model ({reason}), and none of my "
            "patterns matched that. Without a model I can answer anything about your own "
            "objects — your models, databases, schemas, the current schema, sample rows "
            "from a table, past queries, sessions, correctives, where you are, and what "
            'I have been doing — plus "help", "run that" and "load sample data". '
            "Every slash command works too; /help lists them."
        ),
    )


def _generation_refusal(intents: Sequence[str], reason: str) -> str:
    what = " and ".join(dict.fromkeys(_GENERATION_LABELS[i] for i in intents))
    return (
        f"That asks me to {what}, and {reason}. Keywords can work out *what* you want; "
        "they cannot write SQL or DDL, and I will not invent either — fabricated SQL that "
        "looks plausible is worse than no answer. Nothing was done.\n"
        "  To get it: set FIREWORKS_API_KEY (or put the key in ~/.fireworks-key) and run "
        "without T2S_OFFLINE, or capture a fixture for this exact wording.\n"
        "  Without a model I can still answer anything about your own saved objects — "
        '"what models do I have", "what\'s my current schema", "show me some rows from '
        '<table>", "what have you been doing" — and every slash command.'
    )
