"""Click entry point for the ``t2s`` command line interface (design §6).

This is the live-demo surface, so legibility is the point: ``t2s query`` shows
the repair loop working (rejected candidate → engine error → fix), every
outcome (``valid`` / ``clarification_needed`` / ``error``) renders clearly
instead of crashing, and nothing here ever prints a raw traceback for an
expected condition (bad file path, unparseable DDL, missing API key).

Exit codes (stable, scriptable):
    0  valid                 -- a working query/schema was produced
    2  clarification_needed  -- the model needs more information
    3  error                 -- the model (or the safety/binder gate) refused
    1  unexpected failure    -- CLI-level problem: bad args, bad file, no key

Offline demo (D8): set ``--offline`` or ``T2S_OFFLINE=1`` to answer entirely
from ``t2s_core``'s shipped ``RecordedClient`` fixtures -- no network, no API
key. This is what makes the CLI safe to demo on conference wifi or against a
rotated key.
"""

from __future__ import annotations

import json
import sqlite3
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import NoReturn

import click

from foundation import sample_db
from foundation.ddl import EXECUTABLE_DIALECTS
from foundation.paths import managed_directory
from foundation.security import UnsafeQueryError
from t2s_cli.render import pretty_sql, render_result_lines
from t2s_core import (
    FireworksClient,
    FireworksConfig,
    FixtureNotFound,
    InferenceClient,
    NoOpValidator,
    QueryRequest,
    QueryResult,
    SchemaRequest,
    SchemaResult,
    T2SError,
    generate_query,
    generate_schema,
)
from t2s_core.fixtures import recorded_client

EXIT_VALID = 0
EXIT_CLARIFICATION = 2
EXIT_ERROR = 3
EXIT_FAILURE = 1

_EXIT_CODES = {"valid": EXIT_VALID, "clarification_needed": EXIT_CLARIFICATION, "error": EXIT_ERROR}

_DIALECT_CHOICES = ["sqlite", "postgres", "mysql"]


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------
def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _fail(message: str, *, code: int = EXIT_FAILURE) -> NoReturn:
    """A clear one-line error, never a traceback, for an expected condition."""
    click.secho(f"Error: {message}", fg="red", err=True)
    sys.exit(code)


def _fail_for_inference(exc: T2SError) -> NoReturn:
    """An inference failure, said in the user's terms.

    ``--offline`` replays fixtures, and every command in this CLI is a
    *generation* command -- there is no keyword stand-in for writing SQL, and we
    do not invent one. So an unrecorded request is a boundary, not a bug, and it
    is worth saying which. (``t2s-chat``, layer 3, can route without a model
    because routing is a choice over a small enum; generation never is.)
    """
    if isinstance(exc, FixtureNotFound):
        _fail(
            "there is no recorded fixture for this exact request, and offline mode will "
            "not invent SQL or DDL. Run without --offline with FIREWORKS_API_KEY set, or "
            "try one of the recorded demo questions."
        )
    _fail(str(exc))


def _read_text_or_fail(path: str, *, what: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        _fail(f"{what} not found: {path}")
    except IsADirectoryError:
        _fail(f"{what} is a directory, not a file: {path}")
    except OSError as exc:
        _fail(f"could not read {what} ({path}): {exc}")


def _parse_uuid_or_fail(raw: str, *, what: str = "database id") -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        _fail(f"not a valid {what}: {raw!r}")


def _repo_root() -> Path:
    """Walk up from this file to the workspace root (has both packages/ and
    evals/ as siblings). Falls back to cwd if the layout ever changes."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "evals").is_dir() and (parent / "packages").is_dir():
            return parent
    return Path.cwd()


class Obj:
    """Shared per-invocation state, attached to ``click.Context.obj``."""

    def __init__(self, *, offline: bool, no_color: bool) -> None:
        self.offline = offline
        self.no_color = no_color

    def resolve_color(self) -> bool:
        """A definite bool: ``--no-color``/``NO_COLOR`` always wins; otherwise
        colorize only when stdout is a real terminal (degrades cleanly for
        pipes, redirects, and ``CliRunner`` in tests)."""
        if self.no_color:
            return False
        return sys.stdout.isatty()


def _client_for(obj: Obj) -> InferenceClient:
    """An ``InferenceClient``, offline (recorded fixtures) or live.

    Live construction can fail with a clear, expected message -- most notably
    a missing ``FIREWORKS_API_KEY`` -- which must never surface as a
    traceback (D9: the key itself is never echoed either way).
    """
    use_color = obj.resolve_color()
    if obj.offline:
        click.secho(
            "[offline] answering from recorded fixtures — no network call made",
            fg="cyan",
            dim=True,
            err=True,
            color=use_color,
        )
        return recorded_client()
    try:
        config = FireworksConfig.from_env()
    except RuntimeError as exc:
        _fail(f"{exc} (or pass --offline / set T2S_OFFLINE=1 to demo without one)")
    return FireworksClient(config)


def _emit(
    result: QueryResult | SchemaResult,
    *,
    as_json: bool,
    dialect: str,
    use_color: bool,
    code_label: str,
) -> None:
    if as_json:
        # --json: the raw envelope on stdout, nothing else (scriptable).
        click.echo(result.model_dump_json())
        return
    for line in render_result_lines(
        result, dialect=dialect, use_color=use_color, code_label=code_label
    ):
        click.echo(line, color=use_color)


def _exit_for(response_class: str) -> NoReturn:
    sys.exit(_EXIT_CODES.get(response_class, EXIT_FAILURE))


# ---------------------------------------------------------------------------
# Top-level group
# ---------------------------------------------------------------------------
@click.group()
@click.option(
    "--offline",
    is_flag=True,
    default=False,
    help="Answer from recorded fixtures instead of calling Fireworks live "
    "(same as T2S_OFFLINE=1). Safe for demos without network or an API key.",
)
@click.option(
    "--no-color",
    is_flag=True,
    default=False,
    help="Disable ANSI colors in output (same as setting NO_COLOR).",
)
@click.version_option(package_name="t2s_cli", prog_name="t2s")
@click.pass_context
def cli(ctx: click.Context, offline: bool, no_color: bool) -> None:
    """t2s — text-to-SQL command line interface."""
    resolved_offline = offline or _env_truthy("T2S_OFFLINE")
    resolved_no_color = no_color or "NO_COLOR" in os.environ
    ctx.obj = Obj(offline=resolved_offline, no_color=resolved_no_color)


# ---------------------------------------------------------------------------
# t2s query
# ---------------------------------------------------------------------------
@cli.command()
@click.option(
    "--schema", "schema_path", required=True, help="Path to a DDL file describing the database."
)
@click.option("--question", required=True, help="The natural-language question to answer.")
@click.option("--dialect", type=click.Choice(_DIALECT_CHOICES), default="sqlite", show_default=True)
@click.option(
    "--no-repair",
    is_flag=True,
    default=False,
    help="Disable the repair loop (D4's loop-off arm): the first candidate is "
    "returned as-is, unvalidated.",
)
@click.option("--session-summary", default=None, help="Optional prior-session context.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit only the raw JSON envelope on stdout.",
)
@click.pass_obj
def query(
    obj: Obj,
    *,
    schema_path: str,
    question: str,
    dialect: str,
    no_repair: bool,
    session_summary: str | None,
    as_json: bool,
) -> None:
    """Generate one read-only SQL query for QUESTION against SCHEMA."""
    schema_ddl = _read_text_or_fail(schema_path, what="schema file")
    req = QueryRequest(
        question=question,
        schema_ddl=schema_ddl,
        dialect=dialect,  # type: ignore[arg-type]
        session_summary=session_summary,
    )
    client = _client_for(obj)
    validator = NoOpValidator() if no_repair else None
    try:
        result = generate_query(req, client=client, validator=validator)
    except T2SError as exc:
        _fail_for_inference(exc)
    finally:
        if isinstance(client, FireworksClient):
            client.close()
    _emit(result, as_json=as_json, dialect=dialect, use_color=obj.resolve_color(), code_label="SQL")
    _exit_for(result.response_class)


# ---------------------------------------------------------------------------
# t2s schema
# ---------------------------------------------------------------------------
@cli.command()
@click.option("--describe", required=True, help="Natural-language description of the domain.")
@click.option("--dialect", type=click.Choice(_DIALECT_CHOICES), default="sqlite", show_default=True)
@click.option("--out", "out_path", default=None, help="Write the generated DDL to this file.")
@click.option("--session-summary", default=None, help="Optional prior-session context.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit only the raw JSON envelope on stdout.",
)
@click.pass_obj
def schema(
    obj: Obj,
    *,
    describe: str,
    dialect: str,
    out_path: str | None,
    session_summary: str | None,
    as_json: bool,
) -> None:
    """Generate a DDL script for a domain described in natural language."""
    req = SchemaRequest(description=describe, dialect=dialect, session_summary=session_summary)  # type: ignore[arg-type]
    client = _client_for(obj)
    try:
        result = generate_schema(req, client=client)
    except T2SError as exc:
        _fail_for_inference(exc)
    finally:
        if isinstance(client, FireworksClient):
            client.close()

    use_color = obj.resolve_color()
    if out_path and result.response_class == "valid" and result.query:
        try:
            Path(out_path).write_text(result.query, encoding="utf-8")
        except OSError as exc:
            _fail(f"could not write DDL to {out_path}: {exc}")
        if not as_json:
            click.echo(f"Wrote DDL to {out_path}", color=use_color)

    _emit(result, as_json=as_json, dialect=dialect, use_color=use_color, code_label="DDL")
    _exit_for(result.response_class)


# ---------------------------------------------------------------------------
# t2s db create|load|query|list|destroy  (via foundation.sample_db)
# ---------------------------------------------------------------------------
@cli.group()
def db() -> None:
    """Manage sample SQLite databases (foundation's sample-database lifecycle)."""


@db.command("create")
@click.option("--schema", "schema_path", required=True, help="Path to a concrete DDL file.")
@click.option("--dialect", type=click.Choice(_DIALECT_CHOICES), default="sqlite", show_default=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def db_create(schema_path: str, dialect: str, as_json: bool) -> None:
    """Create a new sample database from a DDL file."""
    if dialect not in EXECUTABLE_DIALECTS:
        _fail(
            f"cannot create a live sample database for dialect {dialect!r}; only "
            f"{sorted(EXECUTABLE_DIALECTS)} are executable (D5)"
        )
    ddl_text = _read_text_or_fail(schema_path, what="schema file")
    try:
        database_id = sample_db.create(ddl_text, dialect=dialect)
    except (sample_db.SampleDatabaseError, ValueError) as exc:
        _fail(str(exc))
    if as_json:
        click.echo(json.dumps({"database_id": str(database_id)}))
    else:
        click.echo(f"Created database {database_id}")


@db.command("load")
@click.argument("database_id")
@click.argument("dataset_path")
@click.option("--json", "as_json", is_flag=True, default=False)
def db_load(database_id: str, dataset_path: str, as_json: bool) -> None:
    """Load DATASET_PATH (a JSON {table: [row, ...]} file) into DATABASE_ID."""
    db_uuid = _parse_uuid_or_fail(database_id)
    raw = _read_text_or_fail(dataset_path, what="dataset file")
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError as exc:
        _fail(f"{dataset_path} is not valid JSON: {exc}")
    if not isinstance(rows, dict):
        _fail(f"{dataset_path} must contain a JSON object of {{table: [row, ...]}}")
    try:
        inserted = sample_db.load(db_uuid, rows)
    except sample_db.SampleDatabaseError as exc:
        _fail(str(exc))
    if as_json:
        click.echo(json.dumps({"database_id": str(db_uuid), "rows_inserted": inserted}))
    else:
        click.echo(f"Loaded {inserted} row(s) into database {db_uuid}")


@db.command("query")
@click.argument("database_id")
@click.argument("sql")
@click.option("--dialect", type=click.Choice(_DIALECT_CHOICES), default="sqlite", show_default=True)
@click.option(
    "--allow-catalog",
    is_flag=True,
    default=False,
    help="Allow querying sqlite_master/sqlite_schema (denied by default, D12).",
)
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_obj
def db_query(
    obj: Obj,
    *,
    database_id: str,
    sql: str,
    dialect: str,
    allow_catalog: bool,
    as_json: bool,
) -> None:
    """Run a read-only SQL query against a sample database (D9-gated)."""
    db_uuid = _parse_uuid_or_fail(database_id)
    try:
        result = sample_db.query(db_uuid, sql, dialect=dialect, allow_catalog=allow_catalog)
    except UnsafeQueryError as exc:
        _fail(f"query rejected by the safety gate: {exc}")
    except sample_db.DatabaseNotFoundError:
        _fail(f"no sample database with id {db_uuid}")
    except sample_db.QueryTimeoutError as exc:
        _fail(str(exc))
    except sample_db.SampleDatabaseError as exc:
        _fail(str(exc))

    if as_json:
        click.echo(
            json.dumps(
                {
                    "columns": result.columns,
                    "rows": [list(r) for r in result.rows],
                    "row_count": result.row_count,
                    "truncated": result.truncated,
                }
            )
        )
        return

    use_color = obj.resolve_color()
    click.echo(pretty_sql(sql, dialect), color=use_color)
    click.echo("", color=use_color)
    _print_table(result.columns, result.rows, use_color=use_color)
    if result.truncated:
        click.secho(f"(truncated to {result.row_count} rows)", fg="yellow", err=True)


def _print_table(columns: list[str], rows: list[tuple], *, use_color: bool) -> None:
    if not columns:
        click.echo("(no columns)", color=use_color)
        return
    str_rows = [[str(v) for v in row] for row in rows]
    widths = [len(c) for c in columns]
    for row in str_rows:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(v))
    header = "  ".join(c.ljust(widths[i]) for i, c in enumerate(columns))
    click.echo(click.style(header, bold=True) if use_color else header, color=use_color)
    click.echo("  ".join("-" * w for w in widths), color=use_color)
    for row in str_rows:
        click.echo("  ".join(v.ljust(widths[i]) for i, v in enumerate(row)), color=use_color)
    if not str_rows:
        click.echo("(0 rows)", color=use_color)


@db.command("list")
@click.option("--json", "as_json", is_flag=True, default=False)
def db_list(as_json: bool) -> None:
    """List sample databases in the managed directory."""
    directory = managed_directory()
    entries = []
    for path in sorted(directory.glob("*.sqlite3")):
        try:
            db_uuid = uuid.UUID(hex=path.stem)
        except ValueError:
            continue
        stat = path.stat()
        entries.append({"database_id": str(db_uuid), "size_bytes": stat.st_size})
    if as_json:
        click.echo(json.dumps(entries))
        return
    if not entries:
        click.echo(f"No sample databases in {directory}")
        return
    for entry in entries:
        click.echo(f"{entry['database_id']}  {entry['size_bytes']} bytes")


@db.command("path")
@click.argument("database_id")
@click.option("--sql", default=None, help="Print a ready-to-run sqlite3 command for this SQL.")
def db_path(database_id: str, sql: str | None) -> None:
    """Print the file path of a sample database, so you can query it by hand.

    Accepts the short id the chat displays (`536c7b02`) as well as a full UUID,
    because the point of this command is to be usable from what is already on
    your screen. Verifying the workbench's answer against the file itself, with
    a tool the workbench does not control, is the strongest check available.
    """
    directory = managed_directory()
    wanted = database_id.replace("-", "").lower()
    matches = sorted(p for p in directory.glob("*.sqlite3") if p.stem.startswith(wanted))
    if not matches:
        raise click.ClickException(
            f"No sample database starting with {database_id!r} in {directory}"
        )
    if len(matches) > 1:
        listed = "\n  ".join(m.stem[:12] for m in matches)
        raise click.ClickException(f"{database_id!r} is ambiguous:\n  {listed}")

    path = matches[0]
    if sql is None:
        click.echo(str(path))
        return
    # -readonly so an independent check can never be the thing that mutates
    # the data it is checking.
    click.echo(f'sqlite3 -readonly -header -box "{path}" "{sql}"')


@db.command("export")
@click.argument("database_id")
@click.option(
    "--out", "out_path", default=None, help="Where to write it (default: ./<id>.sqlite3)."
)
def db_export(database_id: str, out_path: str | None) -> None:
    """Write a standalone copy of a sample database to a file you own.

    The point of this command is to stop being involved. What it produces is an
    ordinary SQLite file that DB Browser, the `sqlite3` shell, or anything else
    opens without knowing this project exists -- which is what makes checking
    the workbench's answer against it worth anything. A validation performed
    with the tool under test validates very little.

    Uses `VACUUM INTO`, not a file copy: it takes a consistent snapshot through
    SQLite itself, so a database mid-write cannot produce a torn file.
    """
    directory = managed_directory()
    wanted = database_id.replace("-", "").lower()
    matches = sorted(p for p in directory.glob("*.sqlite3") if p.stem.startswith(wanted))
    if not matches:
        raise click.ClickException(
            f"No sample database starting with {database_id!r} in {directory}"
        )
    if len(matches) > 1:
        listed = "\n  ".join(m.stem[:12] for m in matches)
        raise click.ClickException(f"{database_id!r} is ambiguous:\n  {listed}")

    source = matches[0]
    destination = Path(out_path) if out_path else Path.cwd() / f"{source.stem[:8]}.sqlite3"
    if destination.exists():
        raise click.ClickException(f"{destination} already exists; refusing to overwrite it")
    destination.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        conn.execute("VACUUM INTO ?", (str(destination),))
    finally:
        conn.close()

    size = destination.stat().st_size
    click.echo(f"{destination}  ({size} bytes)")
    click.echo("A plain SQLite file. Open it with anything -- this project is no longer involved.")


@db.command("destroy")
@click.argument("database_id")
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
def db_destroy(database_id: str, yes: bool) -> None:
    """Delete a sample database file. Idempotent."""
    db_uuid = _parse_uuid_or_fail(database_id)
    if not sample_db.exists(db_uuid):
        click.echo(f"No sample database with id {db_uuid} (nothing to do)")
        return
    if not yes:
        click.confirm(f"Destroy sample database {db_uuid}?", abort=True)
    sample_db.destroy(db_uuid)
    click.echo(f"Destroyed database {db_uuid}")


# ---------------------------------------------------------------------------
# t2s eval run  -- thin delegation to evals/harness, if it exists
# ---------------------------------------------------------------------------
@cli.group("eval")
def eval_group() -> None:
    """Delegate to the eval harness."""


@eval_group.command("run", context_settings={"ignore_unknown_options": True})
@click.argument("harness_args", nargs=-1, type=click.UNPROCESSED)
def eval_run(harness_args: tuple[str, ...]) -> None:
    """Run the eval harness (evals/harness), if it has been built yet."""
    root = _repo_root()
    harness_dir = root / "evals" / "harness"
    has_runner = (harness_dir / "__main__.py").exists() or (harness_dir / "run.py").exists()
    if not has_runner:
        _fail(
            "eval harness not available yet: evals/harness/ has no runner module "
            "(__main__.py or run.py). Run 'make test' for the offline suite instead.",
            code=EXIT_FAILURE,
        )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "evals.harness", *harness_args],
        cwd=root,
        check=False,
    )
    sys.exit(completed.returncode)


if __name__ == "__main__":
    cli()
