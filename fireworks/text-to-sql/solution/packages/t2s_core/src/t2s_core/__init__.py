"""t2s_core — stateless text-to-SQL generation, validation, and repair.

Hard rule (D4 / design §1): this package never imports from ``foundation``,
``t2s_api``, or ``t2s_cli``. It depends only on ``sqlglot``, ``pydantic`` and
``httpx``, plus the stdlib -- notably ``sqlite3``, which is what lets the default
validator build a real database from the DDL in the request without borrowing
anything from the persistence layer. CI enforces the boundary with import-linter.

Public surface (design §3) -- deliberately two functions::

    generate_query(req, *, client, validator=None) -> QueryResult
    generate_schema(req, *, client, validator=None) -> SchemaResult

Everything else is a port implementation you inject.
"""

from t2s_core.clients import FireworksClient, RecordedClient, RecordingClient
from t2s_core.config import FireworksConfig
from t2s_core.errors import (
    FixtureNotFound,
    InferenceError,
    RateLimited,
    SafetyViolation,
    T2SError,
    TruncatedResponse,
    UpstreamError,
)
from t2s_core.generate import generate_query, generate_schema
from t2s_core.models import (
    Attempt,
    Dialect,
    ErrorDetail,
    ModelEnvelope,
    QueryRequest,
    QueryResult,
    ResponseClass,
    ResultMetadata,
    SchemaRequest,
    SchemaResult,
    Usage,
    Verdict,
)
from t2s_core.ports import (
    InferenceClient,
    InferenceResponse,
    Message,
    QueryValidator,
    SchemaValidator,
)
from t2s_core.prompts import REGISTRY as PROMPTS
from t2s_core.validation import (
    EphemeralSqliteValidator,
    NoOpValidator,
    SqlglotValidator,
    default_validator,
)

__all__ = [
    "PROMPTS",
    "Attempt",
    "Dialect",
    "EphemeralSqliteValidator",
    "ErrorDetail",
    "FireworksClient",
    "FireworksConfig",
    "FixtureNotFound",
    "InferenceClient",
    "InferenceError",
    "InferenceResponse",
    "Message",
    "ModelEnvelope",
    "NoOpValidator",
    "QueryRequest",
    "QueryResult",
    "QueryValidator",
    "RateLimited",
    "RecordedClient",
    "RecordingClient",
    "ResponseClass",
    "ResultMetadata",
    "SafetyViolation",
    "SchemaRequest",
    "SchemaResult",
    "SchemaValidator",
    "SqlglotValidator",
    "T2SError",
    "TruncatedResponse",
    "UpstreamError",
    "Usage",
    "Verdict",
    "default_validator",
    "generate_query",
    "generate_schema",
]
