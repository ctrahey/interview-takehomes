"""The activity log as the orchestrator emits it, and as a surface consumes it (D14).

Chris: *"things are really quite slow and it's hard to know what is going on."*
The fix is not a faster system, it is a legible one, and these are the
properties that make it legible:

* every step of a turn appends a ``begin`` and, later, an ``end``;
* the ``begin`` is on disk **before** the work starts, so a step that dies
  leaves evidence instead of vanishing;
* the log is never mutated, asserted by watching the SQL the ORM emits;
* a listener sees each transition as it happens, and a listener that raises
  cannot cost the user their turn.
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime
import sqlite3
from pathlib import Path

import pytest
from nl_doubles import ExplodingClient, ScriptedClient, envelope_payload, router_payload
from sqlalchemy import event

from foundation.repositories import ActivityRepository
from t2s_nl.activity import ACTIVITY_KINDS, ActivityEmitter, ActivityRecord, record_of
from t2s_nl.chat import run_command
from t2s_nl.live import LiveActivityDisplay, format_end
from t2s_nl.orchestrator import Orchestrator
from t2s_nl.store import Store

DDL = (
    "CREATE TABLE authors (id INTEGER NOT NULL PRIMARY KEY, name TEXT NOT NULL);\n"
    "CREATE TABLE books (id INTEGER NOT NULL PRIMARY KEY, author_id INTEGER NOT NULL, "
    "title TEXT NOT NULL, FOREIGN KEY (author_id) REFERENCES authors (id));\n"
)

DATA = {
    "tables": [
        {"name": "authors", "columns": ["id", "name"], "rows": [["1", "Ursula Le Guin"]]},
        {
            "name": "books",
            "columns": ["id", "author_id", "title"],
            "rows": [["1", "1", "A Wizard of Earthsea"]],
        },
    ],
    "notes": "one author, one book",
}

SQL = (
    "SELECT a.name, COUNT(*) AS n FROM authors a JOIN books b ON b.author_id = a.id GROUP BY a.name"
)


class Recorder:
    """A listener that remembers everything it was told."""

    def __init__(self) -> None:
        self.begins: list[ActivityRecord] = []
        self.ends: list[ActivityRecord] = []

    def on_begin(self, record: ActivityRecord) -> None:
        self.begins.append(record)

    def on_end(self, record: ActivityRecord) -> None:
        self.ends.append(record)


def _money_path_client() -> ScriptedClient:
    return ScriptedClient(
        router=[
            router_payload("create_schema", text="a bookstore"),
            router_payload("load_data", row_count=1),
            router_payload("query", text="which author has the most books?"),
            router_payload("execute"),
            router_payload("corrective", text="titles are unique"),
        ],
        envelope=[
            envelope_payload("valid", query=DDL, prose="Two tables."),
            envelope_payload("valid", query=SQL, prose="Authors by book count."),
            envelope_payload("valid", query=SQL, prose="Authors by book count, again."),
        ],
        data=[DATA],
    )


def _drive(store: Store, listener: object | None = None) -> Orchestrator:
    orch = Orchestrator(
        client=_money_path_client(),
        store=store,
        listeners=[listener] if listener is not None else (),  # type: ignore[list-item]
    )
    orch.run("a bookstore")
    orch.run("load it with data")
    orch.run("which author has the most books?")
    orch.run("run that")
    orch.run("actually titles are unique")
    return orch


def _drive_lifecycle(store: Store) -> Orchestrator:
    """`_drive`, then W17's destructive lifecycle: clear, then destroy.

    Its own helper rather than more turns on `_drive`, because `_drive` is
    shared and its contract is "ends with a loaded sample database" -- a
    destruction at the end of it would quietly break every caller that relies on
    that, starting with the export test.

    The scripted client is rebuilt with only the two lifecycle classifications
    in its router queue, and *no* payload for the confirmations: "yes" is read
    by `t2s_nl.confirmation` and never reaches a model, which is the property
    being exercised as much as the logging is.
    """
    orch = _drive(store)
    orch.client = ScriptedClient(  # type: ignore[assignment]
        router=[router_payload("clear_data"), router_payload("destroy")]
    )
    orch.run("empty that database")
    orch.run("yes")
    orch.run("delete that database")
    orch.run("yes")
    return orch


def _log_records(store: Store):  # type: ignore[no-untyped-def]
    with store.scope() as db:
        return [record_of(r) for r in ActivityRepository(db).list_for_session(store.session_id)]


def _rows(store: Store) -> list[tuple[str, str, str]]:
    with store.scope() as db:
        return [
            (r.kind, r.phase, r.status)
            for r in ActivityRepository(db).list_for_session(store.session_id)
        ]


# -- the taxonomy ----------------------------------------------------------
def test_every_kind_d14_names_is_actually_emitted(store: Store, sample_db_dir: Path) -> None:
    """Every kind in the taxonomy comes out of a real path.

    D14 named eight; W17 added four for the destructive lifecycle. Two are
    excluded here and have their own tests: ``query.repair`` needs a rejected
    candidate, and ``database.export`` is not on this path at all.
    """
    _drive_lifecycle(store)
    emitted = {kind for kind, _, _ in _rows(store)}
    assert emitted == set(ACTIVITY_KINDS) - {"query.repair", "database.export"}
    assert set(ACTIVITY_KINDS) >= emitted


def test_a_destruction_logs_the_request_and_the_outcome_separately(
    store: Store, sample_db_dir: Path
) -> None:
    """D14 on the destructive path: who asked, what they were told, what happened.

    A log that recorded only ``db.destroy`` would say a database was deleted and
    not that anyone agreed to it. ``confirm.request`` carries what the user was
    shown -- including the row count they were shown -- and ``confirm.resolve``
    carries their answer.
    """
    _drive_lifecycle(store)
    rows = [
        r
        for r in _log_records(store)
        if r.kind in {"confirm.request", "confirm.resolve", "db.clear", "db.destroy"}
        and r.phase == "end"
    ]
    kinds = [r.kind for r in rows]
    assert kinds == [
        "confirm.request",
        "confirm.resolve",
        "db.clear",
        "confirm.request",
        "confirm.resolve",
        "db.destroy",
    ], kinds
    requested = rows[0]
    assert requested.detail is not None
    assert requested.detail["action"] == "clear_data"
    assert "rows" in requested.detail and "tables" in requested.detail
    assert all(r.status == "ok" for r in rows)


def test_every_step_is_a_begin_and_an_end_in_that_order(store: Store, sample_db_dir: Path) -> None:
    _drive(store)
    rows = _rows(store)
    open_kinds: dict[str, int] = {}
    for kind, phase, status in rows:
        if phase == "begin":
            assert status == "running"
            open_kinds[kind] = open_kinds.get(kind, 0) + 1
        else:
            assert open_kinds.get(kind, 0) > 0, f"{kind} ended without beginning"
            open_kinds[kind] -= 1
    assert all(v == 0 for v in open_kinds.values()), f"unclosed: {open_kinds}"

    with store.scope() as db:
        assert ActivityRepository(db).unfinished(store.session_id) == []


def test_the_log_is_appended_and_never_updated(store: Store, sample_db_dir: Path) -> None:
    """Watch the SQL, not the API. A stray ``row.status = "ok"`` would show here."""
    statements: list[str] = []

    @event.listens_for(store.engine, "before_cursor_execute")
    def _capture(*args):  # type: ignore[no-untyped-def]
        statements.append(" ".join(args[2].split()).upper())

    try:
        _drive(store)
    finally:
        event.remove(store.engine, "before_cursor_execute", _capture)

    touching = [s for s in statements if "ACTIVITIES" in s]
    assert touching, "the run must actually have written activities"
    assert not [s for s in touching if s.startswith(("UPDATE", "DELETE"))]


def test_an_inference_step_records_which_model_answered(store: Store, sample_db_dir: Path) -> None:
    """D14's honest fix for "p95 includes provider queueing": the breakdown exists."""
    _drive(store)
    with store.scope() as db:
        ends = [
            r for r in ActivityRepository(db).list_for_session(store.session_id) if r.phase == "end"
        ]
    inference = [r for r in ends if r.kind in {"router.classify", "schema.generate"}]
    assert inference
    for row in inference:
        assert row.model == "scripted/test-model"
        assert row.tokens == 20
        assert row.duration_ms is not None and row.duration_ms >= 0

    # ...and a purely local step records a duration but no model.
    load = next(r for r in ends if r.kind == "data.load")
    assert load.model is None
    assert load.duration_ms is not None


def test_a_step_records_what_it_learned_not_just_that_it_happened(
    store: Store, sample_db_dir: Path
) -> None:
    _drive(store)
    with store.scope() as db:
        repo = ActivityRepository(db)
        generated = repo.latest(store.session_id, kind="query.generate")
        executed = repo.latest(store.session_id, kind="query.execute")
    assert generated is not None and generated.detail is not None
    assert generated.detail["sql"] == SQL
    assert executed is not None and executed.detail is not None
    assert executed.detail["row_count"] == 1


# -- crash evidence --------------------------------------------------------
def test_a_step_that_raises_closes_as_an_error_and_re_raises(store: Store) -> None:
    emitter = ActivityEmitter(store, store.session_id)
    with pytest.raises(RuntimeError, match="kaboom"), emitter.step("data.generate"):
        raise RuntimeError("kaboom")

    rows = _rows(store)
    assert rows == [("data.generate", "begin", "running"), ("data.generate", "end", "error")]
    with store.scope() as db:
        end = ActivityRepository(db).latest(store.session_id, kind="data.generate", status="error")
    assert end is not None and "kaboom" in (end.summary or "")


def test_the_begin_is_on_disk_before_the_body_runs(store: Store) -> None:
    """The reason `begin` gets its own transaction.

    If it shared one with the work, a crash would roll it back and the log
    would hold no trace of the step that died -- the one case it exists for.
    """
    emitter = ActivityEmitter(store, store.session_id)
    with emitter.step("data.generate"):
        # Mid-body: a *different* connection can already see the begin row.
        assert _rows(store) == [("data.generate", "begin", "running")]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork to kill a process mid-step")
def test_a_crashed_process_leaves_a_begin_with_no_end(tmp_path: Path) -> None:
    """The real thing: a child process dies inside a step and never unwinds.

    ``os._exit`` skips atexit hooks, ``finally`` blocks and context-manager
    ``__exit__`` -- exactly what a ``SIGKILL`` or a closed laptop does. A status
    table would be left with a row saying "running" forever and no way to tell
    that from a step still in flight. The log says: began at T, never ended.
    """
    url = f"sqlite:///{tmp_path / 'foundation.sqlite3'}"
    Store(url)  # create the schema before forking, so the child only writes

    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns to pytest
        try:
            child_store = Store(url)
            emitter = ActivityEmitter(child_store, child_store.session_id)
            with emitter.step("data.generate", summary="generating sample data"):
                os._exit(0)
        except BaseException:
            os._exit(1)
        os._exit(2)

    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0, "the child did not reach the step"

    store = Store(url)
    with store.scope() as db:
        repo = ActivityRepository(db)
        rows = repo.list_for_session(store.session_id)
        unfinished = repo.unfinished(store.session_id)
    assert [(r.kind, r.phase) for r in rows] == [("data.generate", "begin")]
    assert [r.kind for r in unfinished] == ["data.generate"]
    assert rows[0].status == "running"


# -- listeners -------------------------------------------------------------
def test_a_listener_sees_every_transition_as_it_happens(store: Store, sample_db_dir: Path) -> None:
    recorder = Recorder()
    _drive(store, recorder)

    assert len(recorder.begins) == len(recorder.ends)
    assert [r.kind for r in recorder.begins] == [
        kind for kind, phase, _ in _rows(store) if phase == "begin"
    ]
    # The end record carries the duration, which is what a "✓ 7.0s" line needs.
    assert all(r.duration_ms is not None for r in recorder.ends)
    assert all(r.phase == "begin" for r in recorder.begins)


def test_a_listener_that_raises_cannot_cost_the_user_their_turn(
    store: Store, sample_db_dir: Path
) -> None:
    """Observability is not allowed to be a new failure mode."""

    class Broken:
        def on_begin(self, record: ActivityRecord) -> None:
            raise RuntimeError("renderer exploded")

        def on_end(self, record: ActivityRecord) -> None:
            raise RuntimeError("renderer exploded again")

    orch = Orchestrator(client=_money_path_client(), store=store, listeners=[Broken()])  # type: ignore[list-item]
    turn = orch.handle("a bookstore")
    assert turn.kind == "answer"
    assert turn.ddl is not None
    assert [kind for kind, phase, _ in _rows(store) if phase == "end"]


def _record(phase: str, **kw: object) -> ActivityRecord:
    return ActivityRecord(
        seq=kw.pop("seq", 1),  # type: ignore[arg-type]
        session_id=uuid.uuid4(),
        kind="data.generate",
        phase=phase,
        status=kw.pop("status", "ok" if phase == "end" else "running"),  # type: ignore[arg-type]
        at=datetime.now(UTC),
        summary="generating sample data",
        **kw,  # type: ignore[arg-type]
    )


def test_the_live_display_resolves_a_step_to_a_mark_and_a_duration() -> None:
    line = format_end(_record("end", seq=2, duration_ms=7042))
    assert "generating sample data" in line
    assert "7.0s" in line


def test_the_live_display_writes_nothing_until_the_step_resolves(capsys) -> None:  # type: ignore[no-untyped-def]
    """A pipe, a CI log, a redirected transcript: one plain line, no spinner."""
    display = LiveActivityDisplay(stream=sys.stdout, animate=False)

    display.on_begin(_record("begin"))
    assert capsys.readouterr().out == "", "nothing is drawn until the step resolves"

    display.on_end(_record("end", seq=2, duration_ms=7000))
    out = capsys.readouterr().out
    assert "generating sample data" in out
    assert "7.0s" in out
    assert "\r" not in out, "a non-tty must not receive carriage returns"
    display.close()


def test_a_step_too_fast_to_see_is_not_printed(capsys) -> None:  # type: ignore[no-untyped-def]
    """A 4ms corrective.record flashing past is noise, and noise is the problem."""
    display = LiveActivityDisplay(stream=sys.stdout, animate=False)
    display.on_begin(_record("begin"))
    display.on_end(_record("end", seq=2, duration_ms=4))
    assert capsys.readouterr().out == ""
    display.close()


# -- the surface -----------------------------------------------------------
def test_slash_log_renders_the_history_without_a_model(store: Store, sample_db_dir: Path) -> None:
    orch = _drive(store)
    orch.client = ExplodingClient()
    result = run_command(orch, "/log")

    assert not isinstance(result, str) and result is not None
    assert not isinstance(result, list)
    assert result.table is not None
    assert result.deterministic_answer is True
    assert result.table.columns == ["#", "at", "kind", "status", "took", "what"]
    kinds = {row[2] for row in result.table.rows}
    assert {"router.classify", "query.generate", "query.execute"} <= kinds
    assert any(row[4].endswith("s") for row in result.table.rows), "durations are shown"


def test_slash_log_marks_a_step_that_never_ended(store: Store) -> None:
    orch = Orchestrator(client=ExplodingClient(), store=store)
    with store.scope() as db:
        ActivityRepository(db).append(
            store.session_id, kind="data.generate", phase="begin", status="running"
        )

    result = run_command(orch, "/log")
    assert result is not None and not isinstance(result, str | list)
    assert result.table is not None
    assert [row[3] for row in result.table.rows] == ["…no end"]


def test_a_rejected_candidate_appends_a_query_repair_activity(
    store: Store, sample_db_dir: Path
) -> None:
    """D14's eighth kind. The repair loop is the most interesting thing the
    system does and it was invisible in the log until now.

    The loop itself lives inside ``t2s_core`` behind the ``QueryValidator`` port
    (D4), so the pair is reconstructed from the recorded per-attempt metadata
    afterwards -- measured durations, not invented ones.
    """
    client = ScriptedClient(
        router=[
            router_payload("create_schema", text="a bookstore"),
            router_payload("query", text="which author has the most books?"),
        ],
        envelope=[
            envelope_payload("valid", query=DDL, prose="Two tables."),
            # Binds against nothing: the ephemeral validator rejects it and the
            # core re-prompts with the engine's own error text.
            envelope_payload("valid", query="SELECT * FROM shelves", prose="Oops."),
            envelope_payload("valid", query=SQL, prose="Fixed."),
        ],
    )
    orch = Orchestrator(client=client, store=store)
    orch.run("a bookstore")
    turn = orch.run("which author has the most books?")[0]
    assert len(turn.attempts) > 1, "the scripted candidate must actually have been rejected"

    with store.scope() as db:
        repairs = [
            r
            for r in ActivityRepository(db).list_for_session(store.session_id)
            if r.kind == "query.repair"
        ]
    assert [r.phase for r in repairs] == ["begin", "end"]
    end = repairs[1]
    assert end.status == "error"
    assert end.detail is not None
    assert end.detail["candidate_sql"] == "SELECT * FROM shelves"
    assert end.detail["failure_kind"]
    assert "rejected" in (end.summary or "")


def test_export_writes_a_standalone_file_and_logs_it(
    store: Store, sample_db_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`export` is the turn whose whole purpose is to end our involvement.

    What it produces has to open without us -- so this reads it back with a
    bare sqlite3 connection that knows nothing about the workbench.
    """
    monkeypatch.setenv("T2S_EXPORT_DIR", str(tmp_path))
    orch = _drive(store)  # leaves a loaded sample database on the session
    # `_drive`'s scripted router queue is exhausted by the turns above and
    # repeats its last entry, so give this turn its own classification.
    orch.client = ScriptedClient(router=[router_payload("export")])  # type: ignore[assignment]
    turn = orch.run("save a copy of this database locally")[-1]

    assert turn.intent == "export"
    written = sorted(tmp_path.glob("*.sqlite3"))
    assert len(written) == 1, f"expected one exported file, got {written}"
    assert str(written[0]) in turn.text

    connection = sqlite3.connect(written[0])
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()
    assert tables, "the exported file has no tables in it"

    assert "database.export" in {kind for kind, _, _ in _rows(store)}
