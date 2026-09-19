"""Working out *what* a destructive request means: a database, or a model (W17/W18).

Deterministic, like ``t2s_nl.inspection`` and for the same reason: the model's
whole contribution to "delete the sports league database" is the intent
``destroy`` and the user's own words ``"the sports league database"``. Turning
those words into one specific instance is a lookup, and a lookup is the last
place a guess belongs when the outcome is irreversible.

A sample database has no name of its own -- it is a file named by a UUID (D9,
no client-supplied path ever reaches the filesystem). What users name is the
*model*: "the sports league database" means "the sample database built from the
data model I called sports-league". So resolution walks
``Database → Schema → DataModelVersion → DataModel`` and matches on the model's
name.

Three outcomes, and the two that are not a match matter as much as the one that
is:

* exactly one candidate matches -> that is the target;
* several match -> :class:`Ambiguous`, and the caller asks which. Picking the
  most recent would be a coin flip with a permanent loser;
* none match -> :class:`NotFound`, carrying what *does* exist, because "no such
  database" without a list is a dead end.

Matching is token containment over a normalised name, with the words that mean
"database" stripped: "the sports league database" and "sports-league" agree;
"the database" on its own names nothing and falls back to the session's current
database. A UUID prefix works too, because that is what the tables on screen
show.

**W18: the same words can name the model instead.** "Delete the sports league
model" and "delete the sports league database" resolve against the *same* name
to two different objects with very different blast radii, and W17 only had the
second. :func:`resolve_destroy` takes the scope the router extracted and answers
with a :class:`Target` (a database), a :class:`ModelTarget` (the design and
everything derived from it), or -- when the user named something without saying
which kind -- :class:`NeedsScope`, which is the question rather than a guess.
``"both"`` resolves to the model, because model deletion cascades; that is the
practical payoff of the cascade decision documented in
``foundation.deletion``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from foundation import deletion, sample_db
from foundation.graph import EntityGraph
from foundation.models import Database, DatabaseStatus, DataModel, DataModelVersion, Schema
from t2s_nl.turns import DataTable

__all__ = [
    "Ambiguous",
    "ModelTarget",
    "NeedsScope",
    "NotFound",
    "Resolution",
    "Target",
    "candidates",
    "describe",
    "describe_model",
    "model_candidates",
    "resolve",
    "resolve_destroy",
]

#: Words that are part of how people refer to a database rather than part of its
#: name. Stripped from both sides before matching, so "the sports league
#: database" and the model named "sports-league" are the same tokens.
_NOISE: frozenset[str] = frozenset(
    {
        "a", "all", "an", "data", "database", "databases", "db", "dbs", "entire",
        "instance", "instances", "it", "me", "model", "models", "my", "one", "sample",
        "schema", "schemas", "that", "the", "this", "whole",
    }
)  # fmt: skip

_WORD = re.compile(r"[a-z0-9]+")
_SHORT_ID = 8


@dataclass(frozen=True, slots=True)
class Target:
    """One resolvable sample database, with everything a confirmation must say."""

    database_id: uuid.UUID
    status: str
    model_name: str
    model_id: uuid.UUID | None
    version_id: uuid.UUID | None
    schema_id: uuid.UUID
    tables: tuple[str, ...] = ()

    @property
    def short_id(self) -> str:
        return str(self.database_id)[:_SHORT_ID]

    @property
    def label(self) -> str:
        """A name-only description, safe to put in a prompt.

        No UUID, deliberately. This string reaches the router as conversational
        history (``t2s_nl.history``), and a prompt carrying a value that is
        freshly random on every run is a prompt no recorded fixture can match
        twice (D8). The short id goes on screen and into the activity log, both
        of which are ours.
        """
        return f"the sample database for data model {self.model_name!r}"

    @property
    def exists(self) -> bool:
        return sample_db.exists(self.database_id)


@dataclass(frozen=True, slots=True)
class ModelTarget:
    """One resolvable data model (W18).

    Deliberately thin. Working out what deleting it would cost means reading
    every sample database file underneath it (:func:`describe_model`), and doing
    that for every candidate just to *list* the candidates would make "which one
    did you mean?" the expensive branch.
    """

    data_model_id: uuid.UUID
    name: str
    version_count: int = 0
    database_count: int = 0

    @property
    def short_id(self) -> str:
        return str(self.data_model_id)[:_SHORT_ID]

    @property
    def label(self) -> str:
        """Name only -- no UUID. Same D8 constraint as :attr:`Target.label`."""
        return f"the data model {self.name!r}"


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """More than one thing answers to the name the user used.

    Exactly one of ``options`` / ``models`` is populated: the resolver knows
    which kind of thing it was resolving, and a caller that had to guess from a
    mixed list would be back where the user started.
    """

    named: str
    options: tuple[Target, ...] = field(default_factory=tuple)
    models: tuple[ModelTarget, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class NeedsScope:
    """The name resolves, but not to one *kind* of thing (W18).

    This is Chris's question, made answerable. "Delete the sports model" names a
    data model that has a sample database hanging off it, and "the model" and
    "its database" are different deletions with very different blast radii. W17
    would have quietly destroyed the database; the honest answer is to ask, and
    -- since deleting the model cascades -- every one of the three possible
    answers is now executable.
    """

    named: str
    model: ModelTarget
    databases: tuple[Target, ...]


@dataclass(frozen=True, slots=True)
class NotFound:
    """Nothing answers to it. ``available`` is what does exist, for the reply."""

    named: str | None
    available: tuple[Target, ...] = field(default_factory=tuple)
    models: tuple[ModelTarget, ...] = field(default_factory=tuple)


Resolution = Target | ModelTarget | NeedsScope | Ambiguous | NotFound


def candidates(db: OrmSession, project_id: uuid.UUID) -> tuple[Target, ...]:
    """Every live sample database in the project, newest first.

    Databases already marked destroyed are excluded: they are not targets, and
    offering one as a choice would invite a user to confirm the deletion of
    something that is already gone.
    """
    rows = db.execute(
        select(Database, Schema, DataModelVersion, DataModel)
        .join(Schema, Database.schema_id == Schema.id)
        .join(DataModelVersion, Schema.data_model_version_id == DataModelVersion.id)
        .join(DataModel, DataModelVersion.data_model_id == DataModel.id)
        .where(
            DataModel.project_id == project_id,
            Database.status != DatabaseStatus.DESTROYED,
        )
        .order_by(Database.created_at.desc())
    ).all()
    return tuple(
        Target(
            database_id=database.id,
            status=str(database.status),
            model_name=model.name or "(unnamed)",
            model_id=model.id,
            version_id=version.id,
            schema_id=schema.id,
            tables=_tables_of(version),
        )
        for database, schema, version, model in rows
    )


def resolve(  # noqa: PLR0911
    db: OrmSession,
    project_id: uuid.UUID,
    *,
    named: str | None,
    current_database_id: uuid.UUID | None = None,
) -> Resolution:
    """Which database the user meant.

    With a name, only the name decides -- the session's current database never
    silently wins an argument with the words the user typed. Without one, the
    current database is the referent, and failing that a lone candidate is
    unambiguous by arithmetic.
    """
    available = candidates(db, project_id)
    if not available:
        return NotFound(named=named, available=())

    if named and (tokens := _tokens(named)):
        matched = tuple(t for t in available if _matches_database(tokens, named, t))
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            return Ambiguous(named=named, options=matched)
        return NotFound(named=named, available=available)

    if current_database_id is not None:
        for target in available:
            if target.database_id == current_database_id:
                return target
    if len(available) == 1:
        return available[0]
    return Ambiguous(named=named or "", options=available)


def model_candidates(db: OrmSession, project_id: uuid.UUID) -> tuple[ModelTarget, ...]:
    """Every data model in the project, newest first, with its counts.

    Unlike :func:`candidates` nothing is filtered out: a model with no versions,
    no schema and no database is still a model the user can name and still a row
    that deleting removes. The counts are what let a "which one?" question say
    how big each choice is.
    """
    rows = db.execute(
        select(DataModel)
        .where(DataModel.project_id == project_id)
        .order_by(DataModel.created_at.desc())
    ).scalars()
    live = candidates(db, project_id)
    targets: list[ModelTarget] = []
    for model in rows:
        version_count = len(
            db.execute(
                select(DataModelVersion.id).where(DataModelVersion.data_model_id == model.id)
            )
            .scalars()
            .all()
        )
        targets.append(
            ModelTarget(
                data_model_id=model.id,
                name=model.name or "(unnamed)",
                version_count=version_count,
                database_count=sum(1 for t in live if t.model_id == model.id),
            )
        )
    return tuple(targets)


def resolve_destroy(  # noqa: PLR0911
    db: OrmSession,
    project_id: uuid.UUID,
    *,
    named: str | None,
    scope: str = "unspecified",
    current_database_id: uuid.UUID | None = None,
    current_model_id: uuid.UUID | None = None,
) -> Resolution:
    """Which *thing* a ``destroy`` directive means: a model, or one of its databases (W18).

    The scope comes from the router (``Parameters.delete_scope``) and is the only
    thing that decides the *kind*; the name decides the instance. Splitting it
    that way is what makes "both" answerable -- it is a value of the scope, not a
    third object to resolve.

    ``"both"`` resolves to the model, and that is not a shortcut: deleting the
    model cascades to its databases, so model-deletion already *is* both. The
    confirmation still enumerates the databases by id and row count, so nobody
    confirms "both" without seeing the second half of it.

    ``"unspecified"`` with a name is the case Chris hit. If the name is a model
    with nothing built from it, there is only one possible reading and we take
    it; if it has live sample databases, the two readings differ by a whole
    database and we return :class:`NeedsScope` rather than picking. Without a
    name it is a pronoun ("delete that database"), and the session's current
    database is the referent exactly as it was in W17 -- changing that would
    turn every existing "delete it" into a question about a model the user never
    mentioned.
    """
    if scope == "database":
        return resolve(db, project_id, named=named, current_database_id=current_database_id)

    models = model_candidates(db, project_id)
    if scope in ("model", "both"):
        return _resolve_model(
            db, project_id, models, named=named, current_model_id=current_model_id
        )

    if not named or not _tokens(named):
        return resolve(db, project_id, named=named, current_database_id=current_database_id)

    matched = tuple(m for m in models if _matches(_tokens(named), named, m.name))
    if len(matched) > 1:
        return Ambiguous(named=named, models=matched)
    if len(matched) == 1:
        model = matched[0]
        attached = tuple(t for t in candidates(db, project_id) if t.model_id == model.data_model_id)
        if not attached:
            # Nothing is built from it, so "the model" and "its database" cannot
            # disagree. Asking here would be ceremony, not care.
            return model
        return NeedsScope(named=named, model=model, databases=attached)

    # The name is not a model. It may still be a database (a UUID prefix, or a
    # model name we no longer hold), so the database resolver gets the last word
    # -- including its own NotFound, which lists what does exist.
    return resolve(db, project_id, named=named, current_database_id=None)


def _resolve_model(  # noqa: PLR0911
    db: OrmSession,
    project_id: uuid.UUID,
    models: tuple[ModelTarget, ...],
    *,
    named: str | None,
    current_model_id: uuid.UUID | None,
) -> Resolution:
    """A model by name, by the session's current pointer, or by being the only one."""
    if not models:
        return NotFound(named=named, available=candidates(db, project_id))

    if named and (tokens := _tokens(named)):
        matched = tuple(m for m in models if _matches(tokens, named, m.name))
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            return Ambiguous(named=named, models=matched)
        return NotFound(named=named, available=(), models=models)

    if current_model_id is not None:
        for model in models:
            if model.data_model_id == current_model_id:
                return model
    if len(models) == 1:
        return models[0]
    return Ambiguous(named=named or "", models=models)


def describe_model(db: OrmSession, target: ModelTarget) -> tuple[DataTable, deletion.ModelDeletion]:
    """The full blast radius of deleting one model: a table, and the plan behind it.

    Both halves come from the same :func:`foundation.deletion.plan` call, so the
    sentence the user reads and the rows the table shows cannot disagree with
    each other -- and the same type comes back out of the deletion itself, so
    they cannot disagree with what actually happened either.
    """
    losses = deletion.plan(db, target.data_model_id)
    if losses is None:  # pragma: no cover - resolved from a row read moments ago
        losses = deletion.ModelDeletion(data_model_id=target.data_model_id, name=target.name)

    rows: list[list[str]] = [["data model", f"{losses.name} (id {target.short_id})"]]
    rows.append(["versions", str(losses.versions)])
    for schema in losses.schemas:
        rows.append(["schema", schema.label])
    for database in losses.databases:
        rows.append(["sample database", database.label])
    for name in losses.datasets:
        rows.append(["dataset", name])
    if losses.correctives:
        rows.append(["correctives", str(losses.correctives)])
    if losses.queries_unlinked:
        rows.append(
            ["saved queries", f"{losses.queries_unlinked} kept, unlinked from the deleted schema"]
        )
    caption = (
        f"everything that would be deleted with data model {losses.name!r} — "
        f"{losses.total_rows} row(s) across {len(losses.live_databases)} live "
        "sample database(s)"
    )
    return DataTable(columns=["what", "detail"], rows=rows, caption=caption), losses


def describe(target: Target) -> tuple[DataTable, int]:
    """What is in it, right now: a per-table row count and the total.

    Read from the file itself rather than from any stored dataset record. A
    confirmation that describes what we *think* was loaded, when the truth is on
    disk and cheap to read, is a confirmation that can be wrong about the thing
    it exists to protect.
    """
    counts = sample_db.row_counts(target.database_id, target.tables) if target.exists else {}
    rows = [[table, str(counts.get(table, 0))] for table in target.tables]
    total = sum(counts.values())
    caption = (
        f"database {target.short_id} · model {target.model_name!r} · "
        f"{total} row(s) across {len(target.tables)} table(s)"
        if target.exists
        else f"database {target.short_id} · model {target.model_name!r} · "
        "no file on disk (already gone)"
    )
    return DataTable(columns=["table", "rows"], rows=rows, caption=caption), total


def _tables_of(version: DataModelVersion) -> tuple[str, ...]:
    try:
        graph = EntityGraph.model_validate(version.graph)
    except ValueError:  # pragma: no cover - a stored graph that no longer parses
        return ()
    return tuple(table.name for table in graph.tables)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(text.lower()) if w not in _NOISE)


def _matches(tokens: frozenset[str], named: str, candidate_name: str) -> bool:
    """Token containment either way round, over the noise-stripped names.

    Takes a *name* rather than a target so that a data model and a sample
    database are matched by exactly the same rule -- users say "the sports
    league database" and "the sports league model" about things with the same
    name, and two matchers would eventually disagree about which one they meant.
    """
    candidate = _tokens(candidate_name)
    if not candidate:
        return False
    return tokens <= candidate or candidate <= tokens


def _matches_database(tokens: frozenset[str], named: str, target: Target) -> bool:
    """The model-name rule, plus the UUID prefix the on-screen tables show."""
    stripped = named.strip().lower()
    if len(stripped) >= _SHORT_ID and str(target.database_id).startswith(stripped[:_SHORT_ID]):
        return True
    return _matches(tokens, named, target.model_name)
