"""Tests for the example schema library (W11): registry, ingestion, and seeding.

Two angles, deliberately kept separate:

- `test_examples.py::test_*_alter_fold_*` parse the *vendored* Postgres
  source directly through `foundation.ddl.parse_ddl`, independent of the
  committed `graph.json` artifacts -- a regression test on the ALTER-folding
  mechanism itself (D6), not on whatever happens to be committed.
- The rest exercise the committed artifacts through `foundation.examples`'
  public surface (`list_examples` / `load_example`) plus the real
  `foundation.sample_db` lifecycle, the same path W10's chat and the CLI use.
"""

from __future__ import annotations

import sqlite3

import pytest

from foundation import paths, sample_db
from foundation.ddl import parse_ddl
from foundation.examples import EXAMPLES_DIR, ExampleNotFoundError, list_examples, load_example

ALL_NAMES = [
    "blog_with_tags",
    "blog_with_likes",
    "question_answer",
    "surveys",
    "reddit",
    "photo_gallery",
    "hangman",
    "tic_tac_toe",
]


# ---------------------------------------------------------------------------
# registry surface
# ---------------------------------------------------------------------------


def test_list_examples_has_all_eight_in_a_stable_order() -> None:
    infos = list_examples()
    assert [i.name for i in infos] == ALL_NAMES
    for info in infos:
        assert info.table_count > 0
        assert info.description


def test_list_examples_is_deterministic_across_calls() -> None:
    assert list_examples() == list_examples()


def test_load_example_unknown_name_raises() -> None:
    with pytest.raises(ExampleNotFoundError):
        load_example("does_not_exist")


@pytest.mark.parametrize("name", ALL_NAMES)
def test_load_example_returns_graph_ddl_and_seed(name: str) -> None:
    graph, sqlite_ddl, seed_sql = load_example(name)  # unpacks like the docstring promises
    assert graph.tables
    assert "CREATE TABLE" in sqlite_ddl
    assert "INSERT INTO" in seed_sql


# ---------------------------------------------------------------------------
# deliverable: every one of the 8 ingests, renders, creates, and seeds in
# SQLite, with a row-count assertion per table.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_NAMES)
def test_example_creates_and_seeds_in_real_sqlite_with_row_counts(name: str) -> None:
    graph, sqlite_ddl, seed_sql = load_example(name)

    database_id = sample_db.create(sqlite_ddl)
    try:
        # Seeding is an administrative bulk load of our own generated,
        # trusted SQL (not user input) -- same trust level as the schema DDL
        # `sample_db.create` itself already executes via `executescript`.
        conn = sqlite3.connect(str(paths.database_path(database_id)))
        try:
            conn.executescript(seed_sql)
            conn.commit()
        finally:
            conn.close()

        for table in graph.tables:
            result = sample_db.query(database_id, f'SELECT COUNT(*) AS n FROM "{table.name}"')
            (count,) = result.rows[0]
            assert count >= 1, f"{name}.{table.name} has no seed rows at all"
            assert count <= 500, f"{name}.{table.name} seeded implausibly large ({count} rows)"
    finally:
        sample_db.destroy(database_id)


# ---------------------------------------------------------------------------
# deliverable: the circular FK case (question_answer) works.
# ---------------------------------------------------------------------------


def test_question_answer_circular_fk_folds_from_alter_table() -> None:
    source = (EXAMPLES_DIR / "question_answer" / "question_answer.postgres.sql").read_text()
    result = parse_ddl(source, "postgres")
    by_name = {t.name: t for t in result.graph.tables}

    questions_fk = next(fk for fk in by_name["questions"].foreign_keys if fk.ref_table == "answers")
    assert questions_fk.columns == ["best_answer_id"]
    assert questions_fk.ref_columns == ["id"]

    answers_fk = next(fk for fk in by_name["answers"].foreign_keys if fk.ref_table == "questions")
    assert answers_fk.columns == ["question_id"]
    assert answers_fk.ref_columns == ["id"]

    # Both halves of the cycle came from separate ALTER TABLE statements in
    # the source (neither table can forward-reference the other inline) --
    # confirm the *committed* example also round-trips into real SQLite.
    graph, sqlite_ddl, seed_sql = load_example("question_answer")
    database_id = sample_db.create(sqlite_ddl)
    try:
        conn = sqlite3.connect(str(paths.database_path(database_id)))
        try:
            conn.executescript(seed_sql)
            conn.commit()
        finally:
            conn.close()
        result = sample_db.query(
            database_id, "SELECT COUNT(*) FROM questions WHERE best_answer_id IS NOT NULL"
        )
        assert result.rows[0][0] > 0, "expected at least one question with a chosen best answer"
    finally:
        sample_db.destroy(database_id)


# ---------------------------------------------------------------------------
# deliverable: the tic_tac_toe duplicate constraint is deduped, not raising.
# ---------------------------------------------------------------------------


def test_tic_tac_toe_duplicate_unique_constraint_is_deduped_not_raised() -> None:
    source = (EXAMPLES_DIR / "tic_tac_toe" / "tic_tac_toe.postgres.sql").read_text()
    assert source.count("ADD UNIQUE (game_id, position)") == 2, (
        "fixture assumption: upstream source literally repeats this ALTER TABLE"
    )

    result = parse_ddl(source, "postgres")
    turns = next(t for t in result.graph.tables if t.name == "turns")

    matching = [uq for uq in turns.unique_constraints if uq.columns == ["game_id", "position"]]
    assert len(matching) == 1, "duplicate ALTER TABLE ... ADD UNIQUE should fold to one graph entry"

    # A distinct (not duplicate) constraint from the same source -- also
    # present, also survives, proving dedup is identity-based and not
    # "collapse everything on this table."
    assert any(uq.columns == ["game_id"] for uq in turns.unique_constraints)

    # And it actually renders + creates in SQLite -- a naive re-emission of
    # the duplicate ALTER as a second identical UNIQUE constraint/index would
    # raise `sqlite3.OperationalError: index ... already exists`.
    graph, sqlite_ddl, seed_sql = load_example("tic_tac_toe")
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(sqlite_ddl)
        conn.executescript(seed_sql)
    finally:
        conn.close()
