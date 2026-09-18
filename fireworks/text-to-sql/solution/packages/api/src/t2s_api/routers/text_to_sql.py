"""Layer 2: semi-deterministic, stateless text-to-SQL (design §5).

`POST /text-to-sql/query` is satisfiable entirely from the request payload --
no server state is consulted, matching the take-home's literal ask. Both
routes call straight into `t2s_core.generate_query` / `generate_schema`;
nothing here talks to `foundation`'s persistence layer.

`response_class in {"clarification_needed", "error"}` is a **200** carrying
that response_class -- the request succeeded, the answer is a question or a
refusal (design §5). The one exception is `error.code == "invalid_schema_ddl"`:
t2s_core's own preflight already rejected the request's DDL *before* calling
the model, and that is a client error, mapped to 422 here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import Field

from foundation.ddl import parse_ddl
from t2s_api.deps import InjectedInferenceClient, InjectedQueryValidator, InjectedSchemaValidator
from t2s_api.problems import ApiProblem
from t2s_core.generate import generate_query, generate_schema
from t2s_core.models import QueryRequest, QueryResult, SchemaRequest, SchemaResult

router = APIRouter(prefix="/text-to-sql", tags=["text-to-sql"])

#: `foundation.ddl.parse_ddl` only claims these two dialects (mirrors D5).
_GRAPH_CAPABLE_DIALECTS = frozenset({"sqlite", "postgres"})


class SchemaQueryResponse(SchemaResult):
    """`SchemaResult` plus the entity graph the design promises ("DDL + entity graph").

    Built here, in the API layer, by feeding the generated DDL back through
    `foundation.ddl.parse_ddl` -- t2s_core itself never imports `foundation`
    (D4), so this composition can only happen at this layer.
    """

    entity_graph: dict[str, Any] | None = Field(
        default=None,
        description="The dialect-neutral entity graph parsed back out of `query` (the "
        "generated DDL), when the dialect supports it and generation succeeded.",
    )
    entity_graph_warnings: list[str] = Field(
        default_factory=list,
        description="Constructs in the DDL that `parse_ddl` could not capture in the graph "
        "(see foundation.ddl's 'Known inversion limits').",
    )


@router.post("/query", response_model=QueryResult)
def text_to_sql_query(
    req: QueryRequest,
    client: InjectedInferenceClient,
    validator: InjectedQueryValidator,
) -> QueryResult:
    result = generate_query(req, client=client, validator=validator)
    if result.error is not None and result.error.code == "invalid_schema_ddl":
        raise ApiProblem(
            422,
            "invalid-schema-ddl",
            "Invalid Schema DDL",
            result.error.message,
            details=result.error.details,
        )
    return result


@router.post("/schema", response_model=SchemaQueryResponse)
def text_to_sql_schema(
    req: SchemaRequest,
    client: InjectedInferenceClient,
    validator: InjectedSchemaValidator,
) -> SchemaQueryResponse:
    result = generate_schema(req, client=client, validator=validator)
    entity_graph: dict[str, Any] | None = None
    warnings: list[str] = []
    if result.response_class == "valid" and result.query and req.dialect in _GRAPH_CAPABLE_DIALECTS:
        parsed = parse_ddl(result.query, req.dialect)
        entity_graph = parsed.graph.model_dump(mode="json")
        warnings = parsed.warnings
    return SchemaQueryResponse(
        **result.model_dump(), entity_graph=entity_graph, entity_graph_warnings=warnings
    )
