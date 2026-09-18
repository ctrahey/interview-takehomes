"""foundation — stateful persistence and sample database lifecycle.

SQLAlchemy + SQLite. Entities: Project, Session, DataModel, DataModelVersion,
Schema, Query, Dataset, Database (see memory/design.md §4). This package may
depend on ``t2s_core`` (e.g. to inject a validator) but ``t2s_core`` must never
depend back on this package (D4).

Submodules:
- `foundation.graph` -- the dialect-neutral entity graph (pydantic), D6.
- `foundation.ddl` -- pure `graph -> DDL` rendering and the partial inverse,
  via sqlglot.
- `foundation.models` -- SQLAlchemy 2.x ORM models.
- `foundation.db` -- engine/session factory for foundation's own metadata store.
- `foundation.bootstrap` -- default project/session (MAIN.md clarification 2).
- `foundation.repositories` -- thin typed data-access layer.
- `foundation.paths` / `foundation.security` / `foundation.sample_db` --
  the sample database lifecycle and its D9 security guarantees.
"""

__all__: list[str] = []
