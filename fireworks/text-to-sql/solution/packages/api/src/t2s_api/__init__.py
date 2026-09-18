"""t2s_api — FastAPI surface over foundation and t2s_core.

Layer 1: deterministic CRUD (projects, sessions, models, schemas, datasets,
databases). Layer 2: semi-deterministic, stateless text-to-SQL endpoints
(``POST /text-to-sql/query``, ``POST /text-to-sql/schema``). See
memory/design.md §5.
"""

__all__: list[str] = []
