"""Registry over the out-of-the-box example schema library (W11).

Ships 8 example schemas vendored from `codeunion/examples-schema`
(MIT-licensed; see ``examples/LICENSE`` and ``examples/README.md`` for full
attribution) so a user has something to ask questions about immediately,
without first authoring or uploading a schema.

Every example was ingested through the same D6 pipeline this codebase uses
for any user-supplied schema -- it is a live demonstration that the entity
graph is real, not just a claim:

    vendored Postgres DDL --`foundation.ddl.parse_ddl`--> EntityGraph
             EntityGraph  --`foundation.ddl.render_ddl`--> SQLite DDL

Upstream expresses several constraints via ``ALTER TABLE ... ADD UNIQUE`` /
``ADD FOREIGN KEY`` (including a genuinely circular FK pair in
``question_answer``, and literal duplicate constraints in ``tic_tac_toe``).
`parse_ddl` folds those into the graph as ordinary table properties and
dedupes structurally-identical duplicates there -- see `foundation.ddl`'s
module docstring ("ALTER-expressed constraints") for the mechanism. All 8
load and seed cleanly into real SQLite; see ``examples/schemas/*/NOTES.md``
for the parse warnings each one produced (mostly: Postgres's ``SERIAL``
pseudo-type is not a graph type and is captured as a plain integer, per
`foundation.ddl._resolve_column_type`).

Each example directory (``examples/schemas/<name>/``) holds four committed,
byte-reproducible files this module reads directly -- nothing here re-parses
DDL at import time:

- ``graph.json``  -- the `EntityGraph`, as JSON (`EntityGraph.model_validate_json`).
- ``sqlite.sql``  -- `render_ddl(graph, "sqlite")`, ready for `foundation.sample_db.create`.
- ``seed.sql``    -- deterministic seed `INSERT` statements (upstream ships no
  data at all); ready for `sqlite3.Connection.executescript` after the DDL.
- ``NOTES.md``    -- human-readable ingestion notes and row counts.

Public surface (deliberately tiny -- other packages, e.g. the orchestrator's
chat and the CLI, should only ever need these two calls):

    list_examples() -> list[ExampleInfo]
    load_example(name: str) -> LoadedExample  # NamedTuple: (graph, sqlite_ddl, seed_sql)
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import NamedTuple

from foundation.graph import EntityGraph

__all__ = ["ExampleInfo", "ExampleNotFoundError", "LoadedExample", "list_examples", "load_example"]

_REPO_ROOT = Path(__file__).resolve().parents[4]
EXAMPLES_DIR = _REPO_ROOT / "examples" / "schemas"

# Declaration order doubles as the stable, deterministic order `list_examples`
# returns -- roughly upstream's own README ordering.
_DESCRIPTIONS: dict[str, str] = {
    "blog_with_tags": "A basic blog where users publish articles and tag them with arbitrary tags.",
    "blog_with_likes": "A basic blog where users publish articles or like existing ones.",
    "question_answer": "A Q&A site a la Stack Overflow: users, questions, answers, and voting.",
    "surveys": "A survey site: users create surveys for other users to fill out.",
    "reddit": "A Reddit-like link aggregator: submissions, voting, and nested commenting.",
    "photo_gallery": "A basic photo gallery: users create albums and upload photos to them.",
    "hangman": "A player-vs-computer Hangman game: users, games, turns, and phrases.",
    "tic_tac_toe": "A collection of tic-tac-toe games: users, games, and turns.",
}


class ExampleNotFoundError(LookupError):
    """Raised by `load_example` for a name not in `list_examples()`."""


@dataclass(frozen=True, slots=True)
class ExampleInfo:
    """Enough to list an example in a picker without loading it."""

    name: str
    description: str
    table_count: int


class LoadedExample(NamedTuple):
    """`load_example`'s return value -- unpack as `(graph, sqlite_ddl, seed_sql)`."""

    graph: EntityGraph
    sqlite_ddl: str
    seed_sql: str


def _schema_dir(name: str) -> Path:
    path = EXAMPLES_DIR / name
    if name not in _DESCRIPTIONS or not path.is_dir():
        available = ", ".join(sorted(_DESCRIPTIONS))
        raise ExampleNotFoundError(f"no example schema named {name!r}; available: {available}")
    return path


@cache
def list_examples() -> tuple[ExampleInfo, ...]:
    """Every example available, in a stable order. Cheap: reads `graph.json` only."""
    infos = []
    for name, description in _DESCRIPTIONS.items():
        graph_path = _schema_dir(name) / "graph.json"
        graph = EntityGraph.model_validate_json(graph_path.read_text())
        infos.append(ExampleInfo(name=name, description=description, table_count=len(graph.tables)))
    return tuple(infos)


@cache
def load_example(name: str) -> LoadedExample:
    """Load one example's graph, rendered SQLite DDL, and seed data.

    Raises `ExampleNotFoundError` for an unknown name. Results are cached
    (these are small, immutable, committed files -- re-reading them per call
    buys nothing).
    """
    schema_dir = _schema_dir(name)
    graph = EntityGraph.model_validate_json((schema_dir / "graph.json").read_text())
    sqlite_ddl = (schema_dir / "sqlite.sql").read_text()
    seed_sql = (schema_dir / "seed.sql").read_text()
    return LoadedExample(graph=graph, sqlite_ddl=sqlite_ddl, seed_sql=seed_sql)
