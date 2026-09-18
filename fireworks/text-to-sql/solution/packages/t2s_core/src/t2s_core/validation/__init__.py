"""Validators and the D9 safety gate."""

from t2s_core.models import Dialect, Verdict
from t2s_core.validation.ephemeral_sqlite import EphemeralSqliteValidator
from t2s_core.validation.noop import NoOpValidator
from t2s_core.validation.safety import assert_ddl_only, assert_read_only_select
from t2s_core.validation.sqlglot_validator import SqlglotValidator

__all__ = [
    "EphemeralSqliteValidator",
    "NoOpValidator",
    "SqlglotValidator",
    "Verdict",
    "assert_ddl_only",
    "assert_read_only_select",
    "default_validator",
]


def default_validator(dialect: Dialect) -> EphemeralSqliteValidator | SqlglotValidator:
    """SQLite gets the executing validator; everything else gets parse+transpile (D5)."""
    if dialect == "sqlite":
        return EphemeralSqliteValidator()
    return SqlglotValidator()
