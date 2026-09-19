"""The append-only activity log (D14), at the persistence layer.

The property under test is not "rows can be written". It is that the table is a
**transition log and not a status table**: two rows per step, neither of them
ever updated, ordered by a per-session sequence, and durable across a restart.
That is what makes a crashed step legible -- its `begin` stands alone -- and
none of it survives a single well-meaning `UPDATE`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from foundation.bootstrap import get_or_create_default_project, get_or_create_default_session
from foundation.db import create_foundation_engine, init_db, make_session_factory, session_scope
from foundation.models import Activity
from foundation.repositories import ActivityRepository


@pytest.fixture
def session_id(db) -> uuid.UUID:  # type: ignore[no-untyped-def]
    project = get_or_create_default_project(db)
    return get_or_create_default_session(db, project).id


def _append_step(repo: ActivityRepository, session_id: uuid.UUID, kind: str, **end) -> None:  # type: ignore[no-untyped-def]
    repo.append(session_id, kind=kind, phase="begin", status="running", summary=kind)
    repo.append(session_id, kind=kind, phase="end", status="ok", summary=kind, **end)


def test_a_step_is_two_rows_and_the_begin_is_never_touched(db, session_id) -> None:  # type: ignore[no-untyped-def]
    repo = ActivityRepository(db)
    begin = repo.append(session_id, kind="query.generate", phase="begin", status="running")
    end = repo.append(session_id, kind="query.generate", phase="end", status="ok", duration_ms=1234)

    assert (begin.phase, begin.status, begin.duration_ms) == ("begin", "running", None)
    assert (end.phase, end.status, end.duration_ms) == ("end", "ok", 1234)
    assert end.seq == begin.seq + 1
    # The begin row still says "running" -- completion is recorded by the *next*
    # row, which is the whole point of a transition log.
    assert db.get(Activity, begin.id).status == "running"


def test_the_repository_offers_no_way_to_mutate_the_log() -> None:
    """A record of the past that can be rewritten is not a record.

    Asserted structurally rather than by convention: if someone adds an
    ``update`` or ``deactivate`` to this repository, this test fails and they
    have to argue for it in review.
    """
    surface = {n for n in dir(ActivityRepository) if not n.startswith("_")}
    forbidden = {"update", "set", "mark", "mark_done", "finish", "delete", "deactivate", "close"}
    assert surface & forbidden == set(), f"the activity log grew a mutation path: {surface}"
    assert "append" in surface


def test_no_update_or_delete_statement_ever_reaches_the_table(db, session_id) -> None:  # type: ignore[no-untyped-def]
    """The strongest form of the claim: watch the SQL, not the API.

    Every statement the ORM emits while a realistic sequence of activities is
    written is captured, and any UPDATE or DELETE naming ``activities`` fails
    the test. An ``append``-only API is easy to bypass with a stray
    ``row.status = "ok"``; this notices that.
    """
    statements: list[str] = []

    @event.listens_for(db.bind, "before_cursor_execute")
    def _capture(*args):  # type: ignore[no-untyped-def]
        statements.append(" ".join(args[2].split()).upper())

    repo = ActivityRepository(db)
    for kind in ("router.classify", "query.generate", "query.execute"):
        _append_step(repo, session_id, kind, duration_ms=5)
    db.commit()

    event.remove(db.bind, "before_cursor_execute", _capture)
    offending = [
        s for s in statements if "ACTIVITIES" in s and (s.startswith(("UPDATE", "DELETE")))
    ]
    assert offending == [], offending
    assert any(s.startswith("INSERT INTO ACTIVITIES") for s in statements)


def test_the_log_is_ordered_by_seq_not_by_clock(db, session_id) -> None:  # type: ignore[no-untyped-def]
    """Two activities inside one millisecond are common; `seq` is the total order."""
    repo = ActivityRepository(db)
    for kind in ("router.classify", "query.generate", "query.execute"):
        _append_step(repo, session_id, kind)

    rows = repo.list_for_session(session_id)
    assert [r.seq for r in rows] == list(range(1, 7))
    assert [(r.kind, r.phase) for r in rows] == [
        ("router.classify", "begin"),
        ("router.classify", "end"),
        ("query.generate", "begin"),
        ("query.generate", "end"),
        ("query.execute", "begin"),
        ("query.execute", "end"),
    ]
    # A tail is still in order, oldest first.
    assert [r.seq for r in repo.list_for_session(session_id, limit=3)] == [4, 5, 6]


def test_a_begin_without_an_end_is_reported_as_unfinished(db, session_id) -> None:  # type: ignore[no-untyped-def]
    """What a crashed process leaves behind, and how it is read back."""
    repo = ActivityRepository(db)
    _append_step(repo, session_id, "router.classify")
    repo.append(session_id, kind="data.generate", phase="begin", status="running")

    open_rows = repo.unfinished(session_id)
    assert [(r.kind, r.phase) for r in open_rows] == [("data.generate", "begin")]


def test_latest_of_a_kind_is_how_a_referent_resolves(db, session_id) -> None:  # type: ignore[no-untyped-def]
    repo = ActivityRepository(db)
    _append_step(repo, session_id, "query.generate")
    repo.append(
        session_id,
        kind="query.generate",
        phase="end",
        status="ok",
        detail={"sql": "SELECT 2"},
    )

    latest = repo.latest(session_id, kind="query.generate")
    assert latest is not None
    assert latest.detail == {"sql": "SELECT 2"}
    assert repo.latest(session_id, kind="schema.generate") is None


def test_a_duplicate_seq_is_an_integrity_error_not_a_silent_reorder(db, session_id) -> None:  # type: ignore[no-untyped-def]
    db.add(Activity(session_id=session_id, seq=1, kind="a", phase="begin", status="running"))
    db.add(Activity(session_id=session_id, seq=1, kind="b", phase="begin", status="running"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_a_bad_phase_is_refused(db, session_id) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="phase"):
        ActivityRepository(db).append(session_id, kind="x", phase="middle")


def test_the_log_survives_a_restart(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A journal that dies with the process is not a journal."""
    url = f"sqlite:///{tmp_path / 'foundation.sqlite3'}"

    engine = create_foundation_engine(url)
    init_db(engine)
    factory = make_session_factory(engine)
    with session_scope(factory) as db:
        project = get_or_create_default_project(db)
        session_id = get_or_create_default_session(db, project).id
        repo = ActivityRepository(db)
        _append_step(repo, session_id, "schema.generate", duration_ms=4200)
        repo.append(session_id, kind="data.generate", phase="begin", status="running")
    engine.dispose()

    reopened = create_foundation_engine(url)
    init_db(reopened)
    with session_scope(make_session_factory(reopened)) as db:
        rows = list(db.execute(select(Activity).order_by(Activity.seq)).scalars())
        assert [(r.kind, r.phase) for r in rows] == [
            ("schema.generate", "begin"),
            ("schema.generate", "end"),
            ("data.generate", "begin"),
        ]
        assert rows[1].duration_ms == 4200
        # ...and the interrupted step is still legible as interrupted.
        assert [r.kind for r in ActivityRepository(db).unfinished(session_id)] == ["data.generate"]
