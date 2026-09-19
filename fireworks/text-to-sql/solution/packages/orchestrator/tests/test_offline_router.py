"""Offline mode has to be usable, not merely present.

``T2S_OFFLINE=1`` replays fixtures, and fixture coverage of open-ended English is
unbounded, so "what models do I have?" used to die with ``FixtureNotFound``.
``t2s_nl.offline_router`` classifies with keywords instead. These tests pin the
three properties that make that acceptable rather than dishonest:

1. it is a **fallback**, never the path -- a matching fixture always wins, and
   the fallback is unreachable online;
2. it **says so** on every turn it produced;
3. it **refuses** anything that needs generated text, in every shape, rather
   than inventing SQL or DDL.
"""

from __future__ import annotations

import dataclasses
import inspect as inspect_module
import re
from pathlib import Path

import pytest
from nl_doubles import ExplodingClient

import t2s_core.clients
from t2s_core.errors import (
    FixtureNotFound,
    InvalidResponse,
    RateLimited,
    TransportError,
    TruncatedResponse,
    UpstreamError,
)
from t2s_nl import chat, confirmation, offline_router
from t2s_nl.clients import (
    NO_MODEL_AVAILABLE,
    DeferredFireworksClient,
    MissingApiKey,
    make_client,
    offline_client,
)
from t2s_nl.intents import PLAN_SCHEMA, InspectTarget
from t2s_nl.offline_router import (
    GENERATION_INTENTS,
    KEYLESS_ROUTING_NOTE,
    OFFLINE_ROUTING_NOTE,
    classify,
)
from t2s_nl.orchestrator import Orchestrator
from t2s_nl.router import RouterContext, route
from t2s_nl.scenarios import (
    CONTEXTS,
    LOADED_CONTEXT,
    RECALL_CONTEXT,
    ROUTER_CASES,
)
from t2s_nl.store import Store

# --------------------------------------------------------------------------
# 1. Coverage: every inspect target, plus the other deterministic intents
# --------------------------------------------------------------------------

#: One plain-English utterance per ``InspectTarget``. Deliberately *not* the
#: wording the fixtures were captured from, so a pass here means the keyword
#: router works, not that a fixture leaked in.
TARGET_UTTERANCES: dict[str, str] = {
    "models": "what models do I have?",
    "databases": "what databases do I have?",
    "schemas": "list my schemas",
    "schema_detail": "what's my current schema",
    "sample_rows": "show me some sample rows from orders",
    "queries": "what have I asked you so far?",
    "sessions": "show me my other sessions",
    "correctives": "what correctives are on this model?",
    "activity": "what have you been doing?",
    "state": "where am I right now?",
}


@pytest.mark.parametrize(("target", "utterance"), sorted(TARGET_UTTERANCES.items()))
def test_every_inspect_target_is_reachable_by_keyword(target: str, utterance: str) -> None:
    plan = classify(utterance, context=LOADED_CONTEXT)
    assert plan.intents == ("inspect",), utterance
    assert plan.primary.parameters.inspect_target == target
    assert plan.routed_by == "keyword"


def test_the_target_table_covers_the_whole_enum_bar_unspecified() -> None:
    """A new ``InspectTarget`` must come with a way to reach it offline."""
    declared = set(InspectTarget.__args__)  # type: ignore[attr-defined]
    assert declared - set(TARGET_UTTERANCES) == {"unspecified"}


@pytest.mark.parametrize(
    ("utterance", "intent"),
    [
        ("help", "help"),
        ("what can you do?", "help"),
        ("how do I save a schema?", "help"),
        ("run that", "execute"),
        ("go ahead and execute the query against the sample database", "execute"),
        ("load it with some sample data", "load_data"),
        ("fill the database with about 25 rows per table", "load_data"),
        ("make me a sample database", "load_data"),
    ],
)
def test_the_other_deterministic_intents_are_reachable(utterance: str, intent: str) -> None:
    assert classify(utterance, context=LOADED_CONTEXT).intents == (intent,)


def test_it_agrees_with_every_live_captured_router_case() -> None:
    """The model's own answers, replayed as the specification for the keywords.

    ``ROUTER_CASES`` is what a live ``kimi-k2p7-code`` actually decided for 24
    utterances. Where the expectation is a deterministic intent the keyword
    router must match it exactly; where it is a generation intent the keyword
    router must refuse. Anything else is a silent disagreement with the model
    we are standing in for.
    """
    # CONTEXTS, not a local dict of two: the canonical contexts are data now, so
    # adding a third (W17's `recall`) cannot silently skip cases here.
    disagreements = []
    for utterance, context_name, expected in ROUTER_CASES:
        plan = classify(utterance, context=CONTEXTS[context_name])
        got = plan.intents if plan.directives else ("REFUSED",)
        wants_generation = any(i in GENERATION_INTENTS for i in expected)
        # W17: one further outcome counts as agreement, and only for cases
        # declared against the `recall` context -- **asking**. Those utterances
        # are resolvable only from earlier conversation, which this module
        # deliberately cannot see, so `unknown` plus a question is the honest
        # result and a match would mean it had guessed. It must be a *question*
        # though; a bare `unknown` is a dead end, so that is asserted rather
        # than waved through.
        needs_history = context_name == "recall"
        honestly_asked = needs_history and got == ("unknown",) and bool(plan.clarifying_question)
        if not ((wants_generation and got == ("REFUSED",)) or got == expected or honestly_asked):
            disagreements.append((utterance, expected, got))
    assert not disagreements


def test_it_does_not_pretend_to_read_the_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    """W17's degradation contract, pinned two ways.

    First: the keyword router's answer must be **identical** with and without a
    history block on the context. Anything else would mean it had started using
    a signal it has no honest way to interpret.

    Second: an utterance that leans explicitly on earlier conversation gets a
    question that says so, rather than a guess. The stakes are why -- the first
    thing a back-reference is likely to name is which database to delete.
    """
    for utterance, context_name, _ in ROUTER_CASES:
        base = CONTEXTS[context_name]
        bare = dataclasses.replace(base, recent=())
        loaded = dataclasses.replace(base, recent=RECALL_CONTEXT.recent)
        assert classify(utterance, context=bare) == classify(utterance, context=loaded), utterance

    asked = classify("delete the one I already mentioned", context=RECALL_CONTEXT)
    assert asked.intents == ("unknown",)
    assert "no memory of earlier turns" in (asked.clarifying_question or "")
    assert asked.routed_by == "keyword"


# --------------------------------------------------------------------------
# 2. Parameters and plans
# --------------------------------------------------------------------------
def test_it_extracts_a_table_a_row_count_and_a_seed() -> None:
    rows = classify("load it with about 12 rows per table, seed 7", context=LOADED_CONTEXT)
    assert rows.intents == ("load_data",)
    assert rows.primary.parameters.row_count == 12
    assert rows.primary.parameters.seed == 7

    table = classify("show me some rows from order_items", context=LOADED_CONTEXT)
    assert table.primary.parameters.table == "order_items"


def test_a_session_table_name_wins_over_the_noun_after_from() -> None:
    """The context already carries the schema's table names for the model's
    benefit; using them here is free and beats guessing at grammar."""
    plan = classify("show me rows in the books table", context=LOADED_CONTEXT)
    assert plan.primary.parameters.table == "books"


def test_a_nonsense_row_count_is_dropped_not_clamped() -> None:
    plan = classify("load it with 999999 rows", context=LOADED_CONTEXT)
    assert plan.intents == ("load_data",)
    assert plan.primary.parameters.row_count is None


def test_it_splits_a_compound_utterance_on_and_then() -> None:
    plan = classify(
        "load it with sample data and then show me some rows from orders",
        context=LOADED_CONTEXT,
    )
    assert plan.intents == ("load_data", "inspect")
    assert plan.directives[1].parameters.inspect_target == "sample_rows"
    assert plan.directives[1].parameters.table == "orders"


def test_an_ambiguous_split_backs_out_and_reads_the_sentence_whole() -> None:
    """A split that leaves a piece we cannot name IS the ambiguous case.

    "then" is a connector and also an ordinary English word; splitting on it
    when the halves do not both classify would manufacture directives out of
    grammar.
    """
    plan = classify("show me my databases then", context=LOADED_CONTEXT)
    assert plan.intents == ("inspect",)


def test_too_many_directives_are_refused_whole_never_truncated() -> None:
    plan = classify(
        "run that and then load it with data and then run that and then "
        "show me my databases and then show me my schemas",
        context=LOADED_CONTEXT,
    )
    assert plan.directives == []
    assert plan.refusal and "Nothing was done" in plan.refusal


def test_the_last_query_referent_resolves_without_a_model() -> None:
    plan = classify("Awesome -- what's the SQL for that?", context=LOADED_CONTEXT)
    assert plan.intents == ("inspect",)
    assert plan.primary.referent == "last_query"


def test_naming_a_new_question_is_not_a_referent() -> None:
    """ "the SQL for X" is generation; only "the SQL for *that*" is a lookup."""
    plan = classify("show me the sql for unpaid balances", context=LOADED_CONTEXT)
    assert plan.directives == []
    assert plan.refusal


# --------------------------------------------------------------------------
# 3. The refusal boundary: deterministic routing, never deterministic generation
# --------------------------------------------------------------------------
GENERATION_UTTERANCES = [
    "which author sold the most copies last year?",
    "how many orders were placed in March?",
    "I want to model a small bookstore: authors, books, customers and orders",
    "add a reviews table with a rating and a comment",
    "actually, revenue is in cents not dollars",
    "remember that cancelled orders have status 'C' and should be excluded",
    "show me the query for unpaid balances and sample results",
    "populate sample data and then show me a query for unpaid balances",
]

_SQL_SHAPED = re.compile(r"\b(SELECT|CREATE TABLE|INSERT|UPDATE|DELETE|JOIN)\b")


@pytest.mark.parametrize("utterance", GENERATION_UTTERANCES)
def test_anything_needing_generation_is_refused_and_nothing_is_fabricated(
    utterance: str,
) -> None:
    plan = classify(utterance, context=LOADED_CONTEXT)
    assert plan.directives == [], "a refused plan executes nothing"
    assert plan.refusal
    assert not _SQL_SHAPED.search(plan.refusal), "the refusal must not contain SQL"
    # It says what would unblock it, which is the difference between a refusal
    # and a dead end.
    assert "FIREWORKS_API_KEY" in plan.refusal


def test_the_keyword_router_can_never_emit_a_generation_directive() -> None:
    """Whatever it is fed, no ``query``/``create_schema``/``corrective`` runs.

    A property over every utterance in the repo's own corpora, not a spot
    check: the refusal is the *only* exit for a generation-shaped utterance.
    """
    corpus = [u for u, _, _ in ROUTER_CASES]
    corpus += GENERATION_UTTERANCES + list(TARGET_UTTERANCES.values())
    corpus += ["", "   ", "?", "select * from users", "DROP TABLE customers; --"]
    for utterance in corpus:
        plan = classify(utterance, context=LOADED_CONTEXT)
        assert not (set(plan.intents) & GENERATION_INTENTS), utterance


def test_a_generation_directive_reached_with_no_model_explains_itself(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop below the keyword router's refusal.

    ``load_data`` is routable by keyword but its *rows* come from the model, so
    it is the one deterministic-looking intent that can still reach a generation
    call with nothing behind it. It must explain, not fabricate.
    """
    monkeypatch.setenv("T2S_OFFLINE", "1")
    orch = Orchestrator(client=offline_client(), store=store)
    turn = orch.handle("load it with about 4013 rows per table")
    assert turn.kind in ("error", "clarification_needed")
    assert turn.sql is None and turn.ddl is None and turn.table is None


# --------------------------------------------------------------------------
# 4. Fixtures win, and the heuristic is inert online
# --------------------------------------------------------------------------
def _tripwire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any use of the keyword router an immediate, loud failure."""

    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("the keyword router ran on a path that must not reach it")

    monkeypatch.setattr(offline_router, "classify", explode)


@pytest.mark.parametrize(
    ("utterance", "context_name"),
    [(u, c) for u, c, _ in ROUTER_CASES],
)
def test_a_matching_fixture_always_wins(
    utterance: str, context_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every captured utterance still replays from its recording, untouched.

    The ordering in ``router.route`` is the contract: the recorded client is
    asked *first*, and only a ``FixtureNotFound`` reaches the fallback. With the
    fallback booby-trapped, a pass here means all 24 answers came from the model
    that was recorded, not from keywords that happen to agree with it.
    """
    _tripwire(monkeypatch)
    plan = route(utterance, client=offline_client(), context=CONTEXTS[context_name])
    assert plan.routed_by == "model"
    assert plan.routing_note is None


class _RaisingClient:
    model = "raising/test"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        raise self.exc


@pytest.mark.parametrize(
    "exc",
    [
        UpstreamError("503 from upstream"),
        RateLimited("429"),
        TransportError("read timeout"),
        TruncatedResponse("cut off", max_tokens=1500),
        InvalidResponse("no choices in body"),
    ],
)
def test_a_live_failure_never_falls_back_to_keywords(
    exc: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Online is unchanged, and that is the point.

    Every one of these comes from a client that *has* a key and a network. A
    transient 429 or a read timeout must degrade to "ask the user", exactly as
    it always did -- never to a silently lower-quality routing path that the
    user has no way to notice.
    """
    _tripwire(monkeypatch)
    plan = route("what models do I have?", client=_RaisingClient(exc), context=RouterContext())
    assert plan.intents == ("unknown",)
    assert plan.routed_by == "model"
    assert plan.clarifying_question


def test_only_two_local_conditions_can_trigger_the_fallback() -> None:
    assert set(NO_MODEL_AVAILABLE) == {FixtureNotFound, MissingApiKey}


def test_the_live_client_cannot_raise_either_of_them() -> None:
    """Structural proof that the fallback is unreachable online.

    ``FixtureNotFound`` is raised by ``RecordedClient`` and nothing else;
    ``MissingApiKey`` by a deferred client that found no credential. Neither
    name appears in the live client, so a ``FireworksClient`` built from a real
    key has no path into ``offline_router``.
    """
    source = inspect_module.getsource(t2s_core.clients.FireworksClient)
    assert "FixtureNotFound" not in source
    assert "MissingApiKey" not in source


def test_the_wire_schema_cannot_claim_a_provenance() -> None:
    """A model must not be able to stamp its own plan as keyword-routed, or
    vice versa: ``routed_by`` is ours, and is not on the wire."""
    assert "routed_by" not in PLAN_SCHEMA["properties"]
    assert "routing_note" not in PLAN_SCHEMA["properties"]


# --------------------------------------------------------------------------
# 5. It says what it did
# --------------------------------------------------------------------------
def test_every_turn_of_a_keyword_routed_plan_carries_the_note(store: Store) -> None:
    orch = Orchestrator(client=offline_client(), store=store)
    turns = orch.run("what models do I have?")
    assert len(turns) == 1
    assert OFFLINE_ROUTING_NOTE in turns[0].notes
    assert turns[0].deterministic_answer, "the answer itself is still a foundation read"


def test_a_refusal_carries_the_note_too(store: Store) -> None:
    orch = Orchestrator(client=offline_client(), store=store)
    # Deliberately not a captured utterance -- the point is the unfixtured path.
    turn = orch.handle("which widget sold the most last quarter?")
    assert turn.kind == "error"
    assert "FIREWORKS_API_KEY" in turn.text
    assert OFFLINE_ROUTING_NOTE in turn.notes


def test_a_fixture_answered_turn_carries_no_such_note(store: Store) -> None:
    orch = Orchestrator(client=offline_client(), store=store)
    turn = orch.handle("what can you do?")
    assert turn.intent == "help"
    assert OFFLINE_ROUTING_NOTE not in turn.notes
    assert KEYLESS_ROUTING_NOTE not in turn.notes


def test_the_activity_log_records_who_did_the_routing(store: Store) -> None:
    """``/log`` has to answer "who decided that?", not just "what happened"."""
    orch = Orchestrator(client=offline_client(), store=store)
    orch.handle("what models do I have?")
    rows = [r for r in orch.activity.history() if r.kind == "router.classify"]
    ends = [r for r in rows if r.phase == "end"]
    assert ends and ends[-1].detail.get("routed_by") == "keyword"
    assert "(keyword)" in (ends[-1].summary or "")


# --------------------------------------------------------------------------
# 6. No key is not a startup failure
# --------------------------------------------------------------------------
@pytest.fixture
def keyless(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home with no ``~/.fireworks-key`` and no ``FIREWORKS_API_KEY``."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("FIREWORKS_API_KEY", raising=False)
    monkeypatch.delenv("T2S_OFFLINE", raising=False)
    return home


def test_make_client_never_raises_for_a_missing_key(keyless: Path) -> None:
    client = make_client()
    assert isinstance(client, DeferredFireworksClient)
    # The model id is configuration, so a banner works with no credential.
    assert client.model


def test_the_deferred_client_explains_itself_at_the_point_of_need(keyless: Path) -> None:
    client = DeferredFireworksClient()
    with pytest.raises(MissingApiKey) as caught:
        client.complete([], response_schema={})
    assert "FIREWORKS_API_KEY" in str(caught.value)


def test_a_keyless_chat_starts_and_its_deterministic_commands_work(
    keyless: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """The asymmetry ``memory/status.md`` recorded: the API started keyless and
    the chat did not. It does now, and the whole deterministic layer works."""
    code = chat.main(
        [
            "--db-url",
            db_url,
            "--no-live",
            "--ask",
            "/state",
            "--ask",
            "/models",
            "--ask",
            "/dbs",
            "--ask",
            "/log",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "current session state" in out
    assert "no data models yet" in out
    assert "no sample databases yet" in out
    assert "Traceback" not in out


def test_a_keyless_chat_routes_plain_english_and_says_it_did(
    keyless: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = chat.main(["--db-url", db_url, "--no-live", "--ask", "what databases do I have?"])
    assert code == 0
    out = capsys.readouterr().out
    assert "no sample databases yet" in out
    assert KEYLESS_ROUTING_NOTE in out


def test_a_keyless_chat_refuses_generation_rather_than_inventing_it(
    keyless: Path, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = chat.main(
        ["--db-url", db_url, "--no-live", "--ask", "which author sold the most copies?"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "FIREWORKS_API_KEY" in out
    assert not _SQL_SHAPED.search(out)


def test_nothing_in_the_keyword_path_touches_an_inference_client(store: Store) -> None:
    """The routing is keyword-driven, and everything it routes to is a
    deterministic read -- so the whole turn survives a client that detonates."""
    orch = Orchestrator(client=ExplodingClient(), store=store)
    plan = classify("what models do I have?", context=RouterContext())
    turns = orch.execute_plan(plan, "what models do I have?")
    assert turns[0].deterministic_answer
    assert OFFLINE_ROUTING_NOTE in turns[0].notes


# ---------------------------------------------------------------------------
# W18: model deletion is routable offline, and the scope is not guessed at
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("utterance", "scope"),
    [
        ("delete the sports league model", "model"),
        ("remove the sports league data model", "model"),
        ("drop the bookstore design", "model"),
        ("get rid of that data model", "model"),
        ("delete the sports league database", "database"),
        ("get rid of that sample database entirely", "database"),
        ("delete that database", "database"),
        # Both cue families fire, and there is no honest keyword way to rank
        # them -- so the scope is left open and the orchestrator asks, which it
        # can do with no model at all.
        ("delete the sports league model and its database", "unspecified"),
        ("remove the bookstore data model and the db", "unspecified"),
    ],
)
def test_the_keyword_router_fills_in_the_delete_scope(utterance: str, scope: str) -> None:
    """Routing a deletion destroys nothing, so keyword-routing it is safe.

    What is *not* safe is picking between the model and its database on a cue,
    which is why the last two cases resolve to ``"unspecified"`` rather than to
    whichever pattern happened to be listed first.
    """
    plan = classify(utterance, context=LOADED_CONTEXT)
    assert plan.intents == ("destroy",)
    assert plan.directives[0].parameters.delete_scope == scope
    assert plan.routed_by == "keyword"


def test_a_model_deletion_still_names_what_it_is_about() -> None:
    plan = classify("delete the sports league data model", context=LOADED_CONTEXT)
    assert plan.directives[0].parameters.model_ref == "sports league"


def test_clearing_beats_nothing_and_a_mixed_verb_still_asks() -> None:
    """The W17 destroy/clear ambiguity is unaffected by W18's new cue table."""
    plan = classify("clear out the sports league model, just totally delete it")
    assert plan.intents == ("unknown",)
    assert plan.clarifying_question is not None
    assert "EMPTIED" in plan.clarifying_question


@pytest.mark.parametrize(
    ("utterance", "scope"),
    [
        ("both", "both"),
        ("Both, please", "both"),
        ("everything", "both"),
        ("the model and the database", "both"),
        ("the model", "model"),
        ("just the model", "model"),
        ("the sports league model", "model"),
        ("the design", "model"),
        ("just the database", "database"),
        ("the database", "database"),
        ("keep the model", "database"),
    ],
)
def test_a_scope_answer_is_read_in_code(utterance: str, scope: str) -> None:
    assert confirmation.read_scope(utterance) == scope


@pytest.mark.parametrize(
    "utterance",
    [
        "yes",
        "no",
        "",
        "what models do I have?",
        "wait - which one do you mean?",
        "not yet",
        "purple monkey dishwasher",
    ],
)
def test_anything_that_is_not_one_of_the_three_is_not_a_scope(utterance: str) -> None:
    """Same asymmetry as the yes/no reader: unsure resolves away from deleting more."""
    assert confirmation.read_scope(utterance) is None
