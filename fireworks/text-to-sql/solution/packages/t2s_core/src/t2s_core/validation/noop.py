"""The validator that does nothing -- and it is load-bearing.

Passing ``NoOpValidator()`` disables the repair loop end to end, which is exactly
the control arm the eval harness needs: accuracy with the loop off vs. on is the
headline number of the submission (D4). It also makes the *cost* of the loop
visible, since the off arm is one call per question.
"""

from __future__ import annotations

from dataclasses import dataclass

from t2s_core.models import Verdict

__all__ = ["NoOpValidator"]


@dataclass(slots=True)
class NoOpValidator:
    @property
    def name(self) -> str:
        return "noop"

    def check(self, sql: str, schema_ddl: str, dialect: str) -> Verdict:
        return Verdict.passed(stage="skipped")

    def check_ddl(self, ddl: str, dialect: str) -> Verdict:
        return Verdict.passed(stage="skipped")
