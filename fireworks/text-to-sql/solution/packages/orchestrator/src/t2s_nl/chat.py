"""``t2s-chat`` — the conversational surface.

A plain readline REPL, deliberately not a full-screen TUI: the transcript has to
stay in the scrollback so SQL can be selected and copied, and so a session can
be pasted into a report.

Design notes that are requirements, not taste:

* Slash commands are a **shortcut, not the interface**. Every one of them has a
  plain-English equivalent that goes through the router. They exist because
  ``/dbs`` is faster to type than "show me my databases", and because they never
  call a model -- so the workbench stays usable when inference is down.
* ``clarification_needed`` and ``error`` are ordinary turns. Nothing here prints
  a traceback for an expected condition; unexpected exceptions print one line
  with the type and message, and ``T2S_DEBUG=1`` for the rest.
* **A slow step is visible while it runs** (D14). The REPL subscribes a
  :class:`~t2s_nl.live.LiveActivityDisplay` to the orchestrator's activity
  emitter, so "⋯ generating sample data  3.4s" ticks in place and resolves to
  "✓ generating sample data  7.0s". The rendering happens on a daemon thread
  and the work never waits on it. ``/log`` shows the history.
* **One utterance can produce several answers** (D15). A two-directive plan
  prints two turns, labelled ``[1/2]``/``[2/2]``, each as it completes.
* The API key is never printed, never logged, and never interpolated anywhere.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

try:  # pragma: no cover - present on every platform we target
    import readline
except ImportError:  # pragma: no cover - Windows without pyreadline
    readline = None  # type: ignore[assignment]

from t2s_nl import inspection
from t2s_nl.clients import is_offline, make_client
from t2s_nl.live import LiveActivityDisplay
from t2s_nl.orchestrator import HELP_TEXT, Orchestrator
from t2s_nl.render import colorize, pretty_sql, render_table, render_turn
from t2s_nl.store import Store
from t2s_nl.turns import Turn

__all__ = ["main", "run_command"]

HISTORY_FILE = Path.home() / ".t2s" / "chat_history"

SLASH_HELP = """\
  /state          where am I: current model, schema, database, last query
  /models         saved data models        /dbs        sample databases
  /schemas        saved schemas            /queries    queries in this session
  /schema [table] the current schema's columns
  /rows <table>   some rows from the loaded sample database
  /sql            the current SQL          /run        run it
  /log [n]        what I have been doing this session, and how long it took
  /fix <text>     record a corrective and re-ask the last question
  /correctives    what I've been told about this model
  /new            start a fresh data model in this session
  /help  /quit

All of these can also just be said in English."""


def _banner(orch: Orchestrator) -> str:
    mode = "offline (recorded fixtures)" if is_offline() else f"model {orch.client.model}"
    return "\n".join(
        [
            colorize("bold", "text-to-SQL — conversational workbench"),
            colorize("dim", f"  {mode}"),
            colorize("dim", f"  session {orch.store.session_id} · state in {orch.store.url}"),
            colorize("dim", "  describe a domain, ask a question, or /help"),
            "",
        ]
    )


#: Slash command -> the deterministic catalogue read it stands for. Every one of
#: these is also reachable in plain English through the router; the shortcut
#: exists because it is faster to type and because it keeps working when
#: inference does not.
_CATALOGUE_COMMANDS: dict[str, str] = {
    "/state": "state",
    "/models": "models",
    "/dbs": "databases",
    "/schemas": "schemas",
    "/queries": "queries",
    "/correctives": "correctives",
}

_TABLE_COMMANDS: dict[str, str] = {
    "/schema": "schema_detail",
    "/rows": "sample_rows",
}


def _inspection_turn(orch: Orchestrator, target: str, argument: str | None = None) -> Turn:
    return Turn(
        intent="inspect",
        table=orch.catalogue(target, table=argument),
        deterministic_answer=True,
    )


def run_command(orch: Orchestrator, line: str) -> Turn | str | None:
    """Handle a slash command. Returns a Turn, a plain string, or None to quit.

    Deterministic all the way through -- not one of these makes an inference
    call, which is what makes them the right thing to reach for when the model
    or the network is misbehaving.
    """
    command, _, argument = line.partition(" ")
    argument = argument.strip()

    if command in _CATALOGUE_COMMANDS:
        return _inspection_turn(orch, _CATALOGUE_COMMANDS[command])
    if command in _TABLE_COMMANDS:
        return _inspection_turn(orch, _TABLE_COMMANDS[command], argument or None)

    handlers: dict[str, Callable[[], Turn | str | None]] = {
        "/quit": lambda: None,
        "/exit": lambda: None,
        "/q": lambda: None,
        "/help": lambda: HELP_TEXT + "\n\n" + SLASH_HELP,
        "/sql": lambda: _current_sql(orch),
        "/run": orch.run_current_sql,
        "/log": lambda: _log(orch, argument),
        "/fix": lambda: _fix(orch, argument),
        "/new": lambda: _new(orch),
    }
    handler = handlers.get(command)
    if handler is None:
        return f"  unknown command {command} — /help"
    return handler()


def _current_sql(orch: Orchestrator) -> Turn | str:
    sql = orch.current_sql()
    if not sql:
        return "  no current SQL"
    return Turn(intent="inspect", sql=sql, deterministic_answer=True)


def _log(orch: Orchestrator, argument: str) -> Turn:
    """``/log [n]`` -- the append-only activity log (D14).

    The answer to "why did that take nine seconds": the log has the per-step
    breakdown, so the wait can be attributed to a named step rather than
    guessed at.
    """
    try:
        limit = int(argument) if argument else inspection.ACTIVITY_LOG_LIMIT
    except ValueError:
        limit = inspection.ACTIVITY_LOG_LIMIT
    return Turn(
        intent="inspect",
        table=orch.activity_table(limit=max(1, limit)),
        deterministic_answer=True,
    )


def _fix(orch: Orchestrator, argument: str) -> Turn | str:
    if not argument:
        return "  usage: /fix <a fact about your domain>"
    return orch.add_corrective(argument)


def _new(orch: Orchestrator) -> str:
    orch.start_new_model()
    return "  started a fresh data model; describe the domain you want"


def _setup_readline() -> None:
    if readline is None:  # pragma: no cover - Windows
        return
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError, ValueError):
        readline.read_history_file(HISTORY_FILE)
    readline.set_history_length(1000)
    atexit.register(_save_history, readline)


def _save_history(readline_module: ModuleType) -> None:  # pragma: no cover - atexit
    with contextlib.suppress(OSError, AttributeError):
        readline_module.write_history_file(HISTORY_FILE)


def _emit(value: Turn | list[Turn] | str | None, *, dialect: str, max_rows: int) -> None:
    if value is None:
        return
    if isinstance(value, list):
        for turn in value:
            _emit(turn, dialect=dialect, max_rows=max_rows)
        return
    if isinstance(value, str):
        print(value, flush=True)
        return
    if value.plan_length > 1:
        # A multi-directive plan (D15): two answers to one sentence have to
        # read as two answers, not one confusing one.
        print(
            colorize("dim", f"  [{value.plan_position}/{value.plan_length}] {value.intent}"),
            flush=True,
        )
    if value.table is not None and value.table.caption and not value.text:
        # An empty table renders as the caption sentence itself; printing the
        # caption here too would say it twice.
        if value.table.rows:
            print(colorize("dim", f"  {value.table.caption}"), flush=True)
        for line in render_table(value.table, max_rows=max_rows):
            print(line, flush=True)
        return
    if value.sql and not value.text and value.table is None:
        for line in pretty_sql(value.sql, dialect).splitlines():
            print(f"    {line}", flush=True)
        return
    print(render_turn(value, dialect=dialect, max_rows=max_rows), flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="t2s-chat", description="Converse with the text-to-SQL workbench."
    )
    parser.add_argument("--session", help="Named session slug; omit for the default session.")
    parser.add_argument("--db-url", help="Override the foundation metadata store URL.")
    parser.add_argument("--dialect", default="sqlite", choices=["sqlite", "postgres", "mysql"])
    parser.add_argument("--max-rows", type=int, default=25, help="Rows to print per table.")
    parser.add_argument("--ask", action="append", help="Run one utterance and exit; repeatable.")
    parser.add_argument(
        "--no-live",
        dest="live",
        action="store_false",
        help="Suppress the live activity line (it is already off on a non-tty).",
    )
    args = parser.parse_args(argv)

    try:
        client = make_client()
    except RuntimeError as exc:
        print(f"t2s-chat: {exc}", file=sys.stderr)
        return 2

    store = Store(args.db_url, session_slug=args.session)
    # D14: the live line. It animates only on a tty, so a piped or redirected
    # run (the transcripts in the report, CI) gets one plain line per step
    # instead of a wall of carriage returns.
    live = LiveActivityDisplay(animate=None if args.live else False)
    orch = Orchestrator(client=client, store=store, dialect=args.dialect, listeners=[live])

    def printer(turn: Turn) -> None:
        _emit(turn, dialect=args.dialect, max_rows=args.max_rows)

    if args.ask:
        for utterance in args.ask:
            print(colorize("cyan", f"› {utterance}"), flush=True)
            result = _one_line(orch, utterance, on_turn=printer)
            if result is None:  # a /quit in the middle of a scripted run
                live.close()
                return 0
            if not isinstance(result, list):  # a slash command, not yet printed
                _emit(result, dialect=args.dialect, max_rows=args.max_rows)
        live.close()
        return 0

    _setup_readline()
    print(_banner(orch), flush=True)

    while True:
        try:
            line = input(colorize("cyan", "› ")).strip()
        except (EOFError, KeyboardInterrupt):
            print(flush=True)
            live.close()
            return 0
        if not line:
            continue
        result = _one_line(orch, line, on_turn=printer)
        if result is None:
            live.close()
            return 0
        if not isinstance(result, list):  # a slash command, not yet printed
            _emit(result, dialect=args.dialect, max_rows=args.max_rows)


def _one_line(
    orch: Orchestrator, line: str, on_turn: Callable[[Turn], None] | None = None
) -> Turn | list[Turn] | str | None:
    """Dispatch one line of input. ``None`` means "the user asked to quit".

    Slash commands take this branch in *both* the interactive loop and ``--ask``
    mode -- scripted runs must take the same deterministic path an interactive
    user does, or a transcript stops being evidence of what the REPL does.
    """
    if line.startswith("/"):
        return run_command(orch, line)
    # Streamed when a printer is supplied: each directive's answer goes out the
    # moment it completes, so the first half of a compound request is on screen
    # while the second half is still running (D15). The list is returned either
    # way, so a caller without a printer keeps the old shape.
    return _safely(orch, line, on_turn=on_turn)


def _safely(
    orch: Orchestrator, utterance: str, on_turn: Callable[[Turn], None] | None = None
) -> list[Turn]:
    """Last line of defence. Expected conditions are already Turns by here."""
    try:
        return orch.run(utterance, on_turn=on_turn)
    except Exception as exc:  # noqa: BLE001 - a chat loop must not die on one turn
        if os.environ.get("T2S_DEBUG"):
            traceback.print_exc()
        broken = Turn.error(
            f"Something went wrong handling that ({type(exc).__name__}: {exc}). "
            "The session is intact; try again or use /state."
        )
        if on_turn is not None:
            on_turn(broken)
        return [broken]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
