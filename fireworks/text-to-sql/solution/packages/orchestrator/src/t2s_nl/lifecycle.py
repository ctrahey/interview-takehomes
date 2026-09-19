"""Working out *which* sample database a destructive request means (W17).

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
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from foundation import sample_db
from foundation.graph import EntityGraph
from foundation.models import Database, DatabaseStatus, DataModel, DataModelVersion, Schema
from t2s_nl.turns import DataTable

__all__ = [
    "Ambiguous",
    "NotFound",
    "Resolution",
    "Target",
    "candidates",
    "describe",
    "resolve",
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
class Ambiguous:
    """More than one database answers to the name the user used."""

    named: str
    options: tuple[Target, ...]


@dataclass(frozen=True, slots=True)
class NotFound:
    """Nothing answers to it. ``available`` is what does exist, for the reply."""

    named: str | None
    available: tuple[Target, ...] = field(default_factory=tuple)


Resolution = Target | Ambiguous | NotFound


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
        matched = tuple(t for t in available if _matches(tokens, named, t))
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


def _matches(tokens: frozenset[str], named: str, target: Target) -> bool:
    if (
        str(target.database_id).startswith(named.strip().lower()[:_SHORT_ID])
        and len(named.strip()) >= _SHORT_ID
    ):
        return True
    candidate = _tokens(target.model_name)
    if not candidate:
        return False
    return tokens <= candidate or candidate <= tokens
