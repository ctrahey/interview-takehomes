"""W18: deleting a data model, and answering "the model, the database, or both?".

Chris's session, in full:

    › can you delete the sports model?
      ? Do you want to remove the 'sports-league-reporting' data model, delete the
        sample database, or something else?
    › both
      ? What two things would you like me to do or show? ...
    › remove the sports league data model
      ? I don't have a way to delete data models. ...

The last answer was true. `destroy` resolved only to sample databases and
`foundation` had no delete for a `DataModel` at all, so the router asked a good
question and then could not honour either answer. Every test here is about one
of the three turns above.

What is asserted, in order:

1. the scope question is asked when -- and only when -- it is genuinely open,
   and **"both" is an executable answer**, which it is because model deletion
   cascades;
2. a model deletion is described before it happens, and the description names
   the whole blast radius -- versions, schemas, each database by id and row
   count, datasets;
3. the description matches what is actually removed;
4. every W17 safety property still holds at the larger blast radius: a refusal
   leaves everything standing, a stale "yes" after an unrelated turn deletes
   nothing, and a scope answer is not consent.

The model is scripted throughout. Nothing below depends on a network, and the
confirmation turns are answered with no model in the loop at all.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from nl_doubles import ScriptedClient, envelope_payload, router_payload

from foundation import deletion, paths, sample_db
from foundation.models import DataModel
from foundation.repositories import (
    DataModelRepository,
    PendingActionRepository,
    QueryRepository,
    SessionStateRepository,
)
from t2s_nl import lifecycle
from t2s_nl.chat import run_command
from t2s_nl.confirmation import read_scope
from t2s_nl.orchestrator import Orchestrator
from t2s_nl.store import Store
from t2s_nl.turns import Turn

DDL = (
    "CREATE TABLE teams (id INTEGER NOT NULL PRIMARY KEY, name TEXT NOT NULL);\n"
    "CREATE TABLE matches (id INTEGER NOT NULL PRIMARY KEY, home_id INTEGER NOT NULL, "
    "away_id INTEGER NOT NULL, FOREIGN KEY (home_id) REFERENCES teams (id), "
    "FOREIGN KEY (away_id) REFERENCES teams (id));\n"
)

DATA = {
    "tables": [
        {"name": "teams", "columns": ["id", "name"], "rows": [["1", "Rovers"], ["2", "City"]]},
        {"name": "matches", "columns": ["id", "home_id", "away_id"], "rows": [["1", "1", "2"]]},
    ],
    "notes": "two teams, one match",
}


def _client(*router_payloads: dict) -> ScriptedClient:
    return ScriptedClient(
        router=list(router_payloads),
        envelope=[envelope_payload("valid", query=DDL, prose="Two tables.")],
        data=[DATA],
    )


def _seeded(store: Store, *later: dict) -> Orchestrator:
    """A session with one loaded sample database under a model named `sports-league`."""
    orch = Orchestrator(
        client=_client(
            router_payload("create_schema", text="a sports league", model_ref="sports-league"),
            router_payload("load_data", row_count=2),
            *later,
        ),
        store=store,
    )
    orch.run("model a sports league with teams and matches")
    orch.run("load it with data")
    return orch


def _model_id(orch: Orchestrator) -> uuid.UUID:
    with orch.store.scope() as db:
        pointers = orch._pointers(db)
    assert pointers.data_model_id is not None
    return pointers.data_model_id


def _database_id(orch: Orchestrator) -> uuid.UUID:
    with orch.store.scope() as db:
        pointers = orch._pointers(db)
    assert pointers.database_id is not None
    return pointers.database_id


def _model_exists(orch: Orchestrator, model_id: uuid.UUID) -> bool:
    with orch.store.scope() as db:
        return db.get(DataModel, model_id) is not None


# ---------------------------------------------------------------------------
# 1. Chris's transcript, turn by turn
# ---------------------------------------------------------------------------
def test_naming_a_model_without_a_scope_asks_which_and_deletes_nothing(
    store: Store, sample_db_dir: Path
) -> None:
    """Turn one: the question the old system asked well, still asked -- and now answerable."""
    orch = _seeded(store, router_payload("destroy", model_ref="sports league"))
    model_id, database_id = _model_id(orch), _database_id(orch)

    asked = orch.handle("can you delete the sports model?")

    assert asked.kind == "clarification_needed"
    assert asked.intent == "destroy"
    assert "the model" in asked.text and "both" in asked.text
    assert asked.table is not None
    assert [row[0] for row in asked.table.rows] == ["the model", "just the database", "both"]
    assert _model_exists(orch, model_id)
    assert sample_db.exists(database_id)

    # The pending row is a question, not a permission: nothing consumes it as one.
    with store.scope() as db:
        pending = PendingActionRepository(db).get(store.session_id)
        assert pending is not None
        assert pending.action == "destroy_scope"
        assert pending.data_model_id == model_id


def test_both_is_an_answer_the_system_can_honour(store: Store, sample_db_dir: Path) -> None:
    """Turn two. "Both" used to produce "what two things?"; it now produces a plan.

    It resolves to the model because deleting the model *is* both -- the cascade
    already takes the databases. That equivalence is the practical payoff of the
    cascade decision, so it is asserted rather than assumed: the description
    names the sample database, and after the yes the file is gone too.
    """
    orch = _seeded(store, router_payload("destroy", model_ref="sports league"))
    model_id, database_id = _model_id(orch), _database_id(orch)
    path = paths.database_path(database_id)

    orch.handle("can you delete the sports model?")
    described = orch.handle("both")

    assert described.kind == "clarification_needed", "a scope answer is not consent"
    assert "sports-league" in described.text
    assert str(database_id)[:8] in described.text
    assert _model_exists(orch, model_id)
    assert path.exists()

    done = orch.handle("yes")

    assert done.kind == "answer"
    assert done.intent == "destroy"
    assert not _model_exists(orch, model_id)
    assert not path.exists()


def test_remove_the_sports_league_data_model_is_now_executable(
    store: Store, sample_db_dir: Path
) -> None:
    """Turn three: the utterance that was met with "I don't have a way to do that"."""
    orch = _seeded(
        store, router_payload("destroy", model_ref="sports league", delete_scope="model")
    )
    model_id = _model_id(orch)

    asked = orch.handle("remove the sports league data model")
    assert asked.kind == "clarification_needed"
    assert "permanently delete the data model 'sports-league'" in asked.text
    assert _model_exists(orch, model_id)

    done = orch.handle("yes")
    assert done.kind == "answer"
    assert done.deterministic_answer
    assert not _model_exists(orch, model_id)


def test_naming_a_database_still_means_the_database(store: Store, sample_db_dir: Path) -> None:
    """The W17 behaviour, unchanged: the design survives a database deletion."""
    orch = _seeded(
        store, router_payload("destroy", model_ref="sports league", delete_scope="database")
    )
    model_id, database_id = _model_id(orch), _database_id(orch)

    orch.handle("delete the sports league database")
    done = orch.handle("yes")

    assert done.kind == "answer"
    assert not sample_db.exists(database_id)
    assert _model_exists(orch, model_id), "a database deletion must not take the design"


def test_a_model_with_nothing_built_from_it_needs_no_scope_question(
    store: Store, sample_db_dir: Path
) -> None:
    """Asking when only one reading exists is ceremony, not care."""
    orch = _seeded(store, router_payload("destroy", model_ref="payroll"))
    with store.scope() as db:
        bare = DataModelRepository(db).create(project_id=store.project_id, name="payroll")
        bare_id = bare.id

    asked = orch.handle("delete the payroll one")

    assert asked.kind == "clarification_needed"
    assert "Nothing has been built from it yet" in asked.text
    assert _model_exists(orch, bare_id)

    orch.handle("yes")
    assert not _model_exists(orch, bare_id)


# ---------------------------------------------------------------------------
# 2. The blast-radius description
# ---------------------------------------------------------------------------
def test_the_description_enumerates_the_whole_blast_radius(
    store: Store, sample_db_dir: Path
) -> None:
    """A user must never confirm a deletion whose size they were not shown."""
    orch = _seeded(
        store, router_payload("destroy", model_ref="sports league", delete_scope="model")
    )
    database_id = _database_id(orch)
    orch.add_corrective("team names are unique")

    asked = orch.handle("delete the sports league model")

    # The sentence: the model, the versions, the schemas, the databases, the
    # datasets, the correctives, the row total, and that it cannot be undone.
    for fragment in (
        "data model 'sports-league'",
        "1 version(s)",
        "1 schema(s)",
        "1 sample database(s)",
        str(database_id)[:8],
        "3 row(s)",
        "saved dataset(s)",
        "1 corrective(s)",
        "cannot be undone",
    ):
        assert fragment in asked.text, fragment

    # The table: one line per thing that goes, databases by id and row count.
    assert asked.table is not None
    cells = {row[0] for row in asked.table.rows}
    assert {"data model", "versions", "schema", "sample database", "dataset"} <= cells
    database_row = next(row for row in asked.table.rows if row[0] == "sample database")
    assert str(database_id)[:8] in database_row[1]
    assert "3 row(s)" in database_row[1]


def test_the_description_matches_what_is_actually_removed(
    store: Store, sample_db_dir: Path
) -> None:
    """The confirmation is a promise about size. Here it is checked against the outcome."""
    orch = _seeded(
        store, router_payload("destroy", model_ref="sports league", delete_scope="model")
    )
    model_id = _model_id(orch)

    with store.scope() as db:
        promised = deletion.plan(db, model_id)
    orch.handle("delete the sports league model")

    with store.scope() as db:
        recorded = PendingActionRepository(db).get(store.session_id)
        assert recorded is not None
        shape = dict(recorded.detail or {})["shape"]

    done = orch.handle("yes")

    assert promised is not None
    assert shape == {
        "versions": promised.versions,
        "schemas": len(promised.schemas),
        "databases": len(promised.databases),
        "datasets": len(promised.datasets),
        "correctives": promised.correctives,
        "rows": promised.total_rows,
    }
    # ...and the answer states the same numbers back, rather than a vaguer claim.
    assert "1 versions, 1 schemas, 1 databases, 1 datasets, 0 correctives, 3 rows" in done.text
    assert not any("something changed between the two turns" in n for n in done.notes)


def test_the_description_says_what_survives_as_well_as_what_goes(
    store: Store, sample_db_dir: Path
) -> None:
    """A paragraph that only lists losses trains people to skim it."""
    orch = _seeded(
        store, router_payload("destroy", model_ref="sports league", delete_scope="model")
    )
    with store.scope() as db:
        pointers = orch._pointers(db)
        query = QueryRepository(db).create(
            "SELECT name FROM teams",
            session_id=store.session_id,
            schema_id=pointers.schema_id,
            database_id=pointers.database_id,
        )
        query_id = query.id

    asked = orch.handle("delete the sports league model")
    assert "saved quer(y/ies) written against it are kept" in asked.text

    orch.handle("yes")
    with store.scope() as db:
        kept = QueryRepository(db).list_for_session(store.session_id)
    assert [q.id for q in kept] == [query_id]
    assert kept[0].sql == "SELECT name FROM teams"
    assert kept[0].schema_id is None


def test_a_blast_radius_that_changed_between_the_turns_is_reported(
    store: Store, sample_db_dir: Path
) -> None:
    """The promise was about size. If the world moved, say so rather than glossing."""
    orch = _seeded(
        store, router_payload("destroy", model_ref="sports league", delete_scope="model")
    )
    database_id = _database_id(orch)

    orch.handle("delete the sports league model")
    # Something else removes the rows between the description and the yes.
    sample_db.clear(database_id, ["teams", "matches"])

    done = orch.handle("yes")

    assert done.kind == "answer"
    assert any("something changed between the two turns" in note for note in done.notes)


# ---------------------------------------------------------------------------
# 3. The W17 safety properties, at the larger blast radius
# ---------------------------------------------------------------------------
def test_a_refusal_leaves_the_model_and_everything_under_it_intact(
    store: Store, sample_db_dir: Path
) -> None:
    orch = _seeded(
        store, router_payload("destroy", model_ref="sports league", delete_scope="model")
    )
    model_id, database_id = _model_id(orch), _database_id(orch)
    before = sample_db.row_counts(database_id, ["teams", "matches"])

    orch.handle("delete the sports league model")
    declined = orch.handle("no, don't")

    assert declined.intent == "cancel"
    assert _model_exists(orch, model_id)
    assert sample_db.exists(database_id)
    assert sample_db.row_counts(database_id, ["teams", "matches"]) == before
    with store.scope() as db:
        assert PendingActionRepository(db).get(store.session_id) is None

    # And the permission is gone: a later "yes" answers nothing.
    orch.handle("yes")
    assert _model_exists(orch, model_id)


def test_a_stale_yes_after_an_unrelated_turn_deletes_nothing(
    store: Store, sample_db_dir: Path
) -> None:
    """The bigger the blast radius, the more this one matters."""
    orch = _seeded(
        store,
        router_payload("destroy", model_ref="sports league", delete_scope="model"),
        router_payload("inspect", inspect_target="databases"),
    )
    model_id, database_id = _model_id(orch), _database_id(orch)

    orch.handle("delete the sports league model")
    moved_on = orch.handle("actually, show me my databases")

    assert moved_on.intent == "inspect"
    assert any("nothing was deleted" in note for note in moved_on.notes)

    stale = orch.handle("yes")

    assert _model_exists(orch, model_id)
    assert sample_db.exists(database_id)
    assert stale.intent != "destroy" or stale.kind != "answer"


def test_an_unanswered_scope_question_is_dropped_and_announced(
    store: Store, sample_db_dir: Path
) -> None:
    """A scope question follows the same rule as a confirmation: moving on cancels it."""
    orch = _seeded(
        store,
        router_payload("destroy", model_ref="sports league"),
        router_payload("inspect", inspect_target="models"),
    )
    model_id = _model_id(orch)

    orch.handle("delete the sports league one")
    moved_on = orch.handle("actually, what models do I have?")

    assert moved_on.intent == "inspect"
    assert any("nothing was deleted" in note for note in moved_on.notes)
    with store.scope() as db:
        assert PendingActionRepository(db).get(store.session_id) is None

    orch.handle("both")
    assert _model_exists(orch, model_id)


def test_a_scope_answer_is_never_consent(store: Store, sample_db_dir: Path) -> None:
    """Three turns, and the deletion happens on the third. Not the second."""
    orch = _seeded(store, router_payload("destroy", model_ref="sports league"))
    model_id = _model_id(orch)

    orch.handle("delete the sports league one")
    for answer in ("the model", "both", "just the database"):
        assert read_scope(answer) is not None
    described = orch.handle("the model")

    assert described.kind == "clarification_needed"
    assert _model_exists(orch, model_id)
    with store.scope() as db:
        pending = PendingActionRepository(db).get(store.session_id)
        assert pending is not None
        assert pending.action == "destroy_model", "the scope row became a permission row"


def test_a_yes_cannot_answer_a_scope_question(store: Store, sample_db_dir: Path) -> None:
    """ "Yes" is not a scope. It drops the question rather than picking the larger one."""
    orch = _seeded(
        store,
        router_payload("destroy", model_ref="sports league"),
        router_payload("inspect", inspect_target="models"),
    )
    model_id, database_id = _model_id(orch), _database_id(orch)

    orch.handle("delete the sports league one")
    answered = orch.handle("yes")

    assert _model_exists(orch, model_id)
    assert sample_db.exists(database_id)
    assert answered.intent != "destroy" or answered.kind != "answer"


def test_two_model_destroy_directives_in_a_row_delete_nothing(
    store: Store, sample_db_dir: Path
) -> None:
    """The gate is structural at this size too: the permission lives in another table."""
    orch = _seeded(
        store,
        router_payload("destroy", model_ref="sports league", delete_scope="model"),
        router_payload("destroy", model_ref="sports league", delete_scope="model"),
    )
    model_id = _model_id(orch)

    first = orch.handle("delete the sports league model")
    second = orch.handle("delete the sports league model")

    assert first.kind == second.kind == "clarification_needed"
    assert _model_exists(orch, model_id)


def test_a_slash_command_invalidates_a_pending_model_deletion(
    store: Store, sample_db_dir: Path
) -> None:
    orch = _seeded(
        store, router_payload("destroy", model_ref="sports league", delete_scope="model")
    )
    model_id = _model_id(orch)
    orch.handle("delete the sports league model")

    result = run_command(orch, "/new")
    assert isinstance(result, str)
    assert "nothing was deleted" in result

    orch.handle("yes")
    assert _model_exists(orch, model_id)


# ---------------------------------------------------------------------------
# 4. Resolution, and the log
# ---------------------------------------------------------------------------
def test_an_unknown_model_name_is_a_question_with_a_list(store: Store, sample_db_dir: Path) -> None:
    orch = _seeded(store, router_payload("destroy", model_ref="payroll", delete_scope="model"))
    model_id = _model_id(orch)

    answer = orch.handle("delete the payroll model")

    assert answer.kind == "clarification_needed"
    assert "payroll" in answer.text
    assert answer.table is not None
    assert "sports-league" in {row[1] for row in answer.table.rows}
    assert _model_exists(orch, model_id)


def test_two_models_with_the_same_name_are_refused_not_guessed(
    store: Store, sample_db_dir: Path
) -> None:
    orch = _seeded(
        store, router_payload("destroy", model_ref="sports league", delete_scope="model")
    )
    with store.scope() as db:
        DataModelRepository(db).create(project_id=store.project_id, name="sports-league")

    answer = orch.handle("delete the sports league model")

    assert answer.kind == "clarification_needed"
    assert answer.table is not None
    assert len(answer.table.rows) == 2
    assert _model_exists(orch, _model_id(orch))


def test_the_session_forgets_a_model_it_just_deleted(store: Store, sample_db_dir: Path) -> None:
    """Pointers into a deleted subtree are both a dangling FK and a lying `/state`."""
    orch = _seeded(
        store, router_payload("destroy", model_ref="sports league", delete_scope="model")
    )
    orch.handle("delete the sports league model")
    orch.handle("yes")

    with store.scope() as db:
        state = SessionStateRepository(db).get_or_create(store.session_id)
        assert state.current_data_model_id is None
        assert state.current_data_model_version_id is None
        assert state.current_schema_id is None
        assert state.current_database_id is None


def test_the_log_records_the_request_the_answer_and_the_act(
    store: Store, sample_db_dir: Path
) -> None:
    orch = _seeded(store, router_payload("destroy", model_ref="sports league"))

    orch.handle("delete the sports league one")
    orch.handle("both")
    orch.handle("yes")

    ends = [r for r in orch.activity.history() if r.phase == "end"]
    kinds = [r.kind for r in ends]
    assert "confirm.scope" in kinds
    assert "model.destroy" in kinds
    assert kinds.index("confirm.scope") < kinds.index("model.destroy")

    destroyed = next(r for r in ends if r.kind == "model.destroy")
    assert "deleted data model 'sports-league'" in (destroyed.summary or "")
    assert (destroyed.detail or {})["shape"]["databases"] == 1

    # ...and `/log` shows it, with no model involved.
    logged = run_command(orch, "/log")
    assert isinstance(logged, Turn)
    assert logged.table is not None
    assert any("model" in " ".join(row).lower() for row in logged.table.rows)


def test_the_whole_lifecycle_needs_no_inference_after_the_first_classification(
    store: Store, sample_db_dir: Path
) -> None:
    """The scope answer and the yes are both read in code."""
    orch = _seeded(store, router_payload("destroy", model_ref="sports league"))
    orch.handle("delete the sports league one")
    before = len(orch.client.calls)  # type: ignore[attr-defined]

    orch.handle("both")
    orch.handle("yes")

    assert len(orch.client.calls) == before  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 5. The resolver, directly
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("database", lifecycle.Target),
        ("model", lifecycle.ModelTarget),
        ("both", lifecycle.ModelTarget),
        ("unspecified", lifecycle.NeedsScope),
    ],
)
def test_the_scope_decides_the_kind_and_the_name_decides_the_instance(
    store: Store, sample_db_dir: Path, scope: str, expected: type
) -> None:
    _seeded(store)
    with store.scope() as db:
        resolution = lifecycle.resolve_destroy(
            db, store.project_id, named="sports league", scope=scope
        )
    assert isinstance(resolution, expected)


def test_a_pronoun_with_no_scope_still_means_the_current_database(
    store: Store, sample_db_dir: Path
) -> None:
    """W17's "delete that database" must not become a question about a model."""
    orch = _seeded(store)
    database_id = _database_id(orch)
    with store.scope() as db:
        resolution = lifecycle.resolve_destroy(
            db,
            store.project_id,
            named=None,
            scope="unspecified",
            current_database_id=database_id,
        )
    assert isinstance(resolution, lifecycle.Target)
    assert resolution.database_id == database_id


# ---------------------------------------------------------------------------
# 6. "Both" as a widening, and the trap under it
# ---------------------------------------------------------------------------
def test_both_widens_a_database_confirmation_and_is_not_consent(
    store: Store, sample_db_dir: Path
) -> None:
    """A user shown a database deletion who says "both" is escalating, not agreeing.

    The trap this pins: "both" produces a `destroy` directive, and a `destroy`
    directive that finds a live destructive pending row of its own kind is
    consent by construction (W17's gate). So the standing permission has to be
    consumed *before* the widened directive runs, or the word "both" silently
    becomes an unconfirmed deletion of strictly more than was ever described.
    """
    orch = _seeded(
        store, router_payload("destroy", model_ref="sports league", delete_scope="database")
    )
    model_id, database_id = _model_id(orch), _database_id(orch)

    narrow = orch.handle("delete the sports league database")
    assert "only this database instance goes" in narrow.text

    widened = orch.handle("both")

    assert widened.kind == "clarification_needed", "widening must not delete anything"
    assert "permanently delete the data model 'sports-league'" in widened.text
    assert not any("nothing was deleted" in note for note in widened.notes)
    assert _model_exists(orch, model_id)
    assert sample_db.exists(database_id)

    with store.scope() as db:
        pending = PendingActionRepository(db).get(store.session_id)
        assert pending is not None
        assert pending.action == "destroy_model"

    # Only now, and only after the larger description, does it happen.
    done = orch.handle("yes")
    assert done.kind == "answer"
    assert not _model_exists(orch, model_id)
    assert not sample_db.exists(database_id)


def test_a_widening_is_recorded_in_the_log(store: Store, sample_db_dir: Path) -> None:
    orch = _seeded(
        store, router_payload("destroy", model_ref="sports league", delete_scope="database")
    )
    orch.handle("delete the sports league database")
    orch.handle("both")

    # `activity.note` writes a begin/end pair like every other step (D14), so
    # one widening is two rows.
    resolved = [
        r
        for r in orch.activity.history()
        if r.kind == "confirm.resolve"
        and r.phase == "end"
        and (r.detail or {}).get("outcome") == "widened"
    ]
    assert len(resolved) == 1
    assert resolved[0].detail is not None
    assert resolved[0].detail["scope"] == "both"
    assert "widened it to the data model" in (resolved[0].summary or "")
