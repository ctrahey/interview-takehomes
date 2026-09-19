"""Runs the corpus across the D11 arms.

An **arm** is (model, repair-loop setting). D11 requires four of them: the
primary model and a deliberately weaker one, each with the loop off and on.

What "loop off" means here, precisely, because the number depends on it:

    loop off = ``NoOpValidator()`` **and** ``max_repair_attempts=0``
    loop on  = ``EphemeralSqliteValidator()`` and ``max_repair_attempts=2``

``NoOpValidator`` alone is not enough. Three of the four repairable failure
classes (``invalid_json``, ``envelope_invariant``, ``safety_gate``) are raised
before the validator is ever consulted, so a NoOp arm with a non-zero repair
budget would still silently repair — and the control arm would no longer be one
call per question. Setting the budget to zero makes the off arm exactly "what
the raw model said, once", which is the thing the loop is supposed to beat.

Resilience: an exception on a single item (transport, rate limit, a model that
will not produce parseable output at all) is recorded as a failed item carrying
the exception text. It never aborts the run and it is never dropped.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

from evals.harness.corpus import Corpus, CorpusItem
from evals.harness.sandbox import Fixtures
from evals.harness.scoring import ItemScore, score_item

from t2s_core import (
    EphemeralSqliteValidator,
    InferenceClient,
    NoOpValidator,
    QueryRequest,
    QueryResult,
    generate_query,
)

__all__ = [
    "ARM_SPECS",
    "Arm",
    "ArmResult",
    "ItemRun",
    "RunResult",
    "build_arms",
    "rebuild",
    "run_arms",
]

DEFAULT_CONCURRENCY = 6
LOOP_ON_REPAIR_ATTEMPTS = 2

#: (suffix, loop-enabled) — the two settings every model is run under.
ARM_SPECS: tuple[tuple[str, bool], ...] = (("loop_off", False), ("loop_on", True))


@dataclass(frozen=True, slots=True)
class Arm:
    name: str
    role: str
    model: str
    loop: bool

    @property
    def max_repair_attempts(self) -> int:
        return LOOP_ON_REPAIR_ATTEMPTS if self.loop else 0

    @property
    def validator_name(self) -> str:
        return "ephemeral_sqlite" if self.loop else "noop"


def build_arms(models: dict[str, str], *, only: Sequence[str] | None = None) -> tuple[Arm, ...]:
    """``models`` maps role ("primary"/"weak") to a Fireworks model id."""
    arms = tuple(
        Arm(name=f"{role}/{suffix}", role=role, model=model, loop=loop)
        for role, model in models.items()
        for suffix, loop in ARM_SPECS
    )
    if only is None:
        return arms
    wanted = set(only)
    return tuple(arm for arm in arms if arm.name in wanted or arm.role in wanted)


@dataclass(slots=True)
class ItemRun:
    """One (arm, item) outcome: what the model said, what it cost, how it scored."""

    arm: str
    item_id: str
    tier: str
    schema: str
    response_class: str | None = None
    query: str | None = None
    prose: str = ""
    error_code: str | None = None
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    attempt_count: int = 0
    repairs_used: int = 0
    #: index of the attempt that finally passed validation, or None if none did
    winning_attempt: int | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    harness_error: str | None = None
    score: dict[str, Any] | None = None

    def attach(self, score: ItemScore) -> None:
        self.score = asdict(score)

    @property
    def correct(self) -> bool:
        return bool(self.score and self.score["correct"])


@dataclass(slots=True)
class ArmResult:
    arm: Arm
    runs: list[ItemRun]
    wall_clock_s: float = 0.0
    api_calls: int = 0


@dataclass(slots=True)
class RunResult:
    run_id: str
    started_at: str
    arms: list[ArmResult]
    item_ids: list[str]
    live: bool
    notes: list[str] = field(default_factory=list)


def rebuild(payload: dict[str, Any]) -> RunResult:
    """Reconstruct a :class:`RunResult` from a report JSON.

    Exists so the markdown can be re-rendered after a wording or metric change
    without re-spending a single API call — which is also what keeps the prose
    honest, since re-running to get a nicer number is never the cheap option.
    """
    metrics = payload["metrics"]
    arms = [
        ArmResult(
            arm=Arm(name=arm["arm"], role=arm["role"], model=arm["model"], loop=bool(arm["loop"])),
            runs=[ItemRun(**run) for run in arm["runs"]],
            wall_clock_s=float(arm.get("wall_clock_s", 0.0)),
            api_calls=sum(int(run.get("attempt_count", 0)) for run in arm["runs"]),
        )
        for arm in payload["arms"]
    ]
    return RunResult(
        run_id=metrics["run_id"],
        started_at=metrics["started_at"],
        arms=arms,
        item_ids=list(metrics["item_ids"]),
        live=bool(metrics["live"]),
    )


def _record(arm: Arm, item: CorpusItem, result: QueryResult) -> ItemRun:
    attempts = [a.model_dump() for a in result.metadata.attempts]
    winning = next((a["index"] for a in attempts if a["ok"]), None)
    return ItemRun(
        arm=arm.name,
        item_id=item.id,
        tier=item.tier,
        schema=item.schema,
        response_class=result.response_class,
        query=result.query,
        prose=result.prose,
        error_code=result.error.code if result.error else None,
        latency_ms=result.metadata.latency_ms,
        prompt_tokens=result.metadata.usage.prompt_tokens,
        completion_tokens=result.metadata.usage.completion_tokens,
        total_tokens=result.metadata.usage.total_tokens,
        attempt_count=len(attempts),
        repairs_used=result.metadata.repairs_used,
        winning_attempt=winning,
        attempts=attempts,
    )


def _run_one(arm: Arm, item: CorpusItem, corpus: Corpus, client: InferenceClient) -> ItemRun:
    try:
        request = QueryRequest(
            question=item.question,
            schema_ddl=corpus.ddl_for(item),
            dialect="sqlite",
            max_repair_attempts=arm.max_repair_attempts,
        )
        validator = EphemeralSqliteValidator() if arm.loop else NoOpValidator()
        result = generate_query(request, client=client, validator=validator)
    except Exception as exc:  # noqa: BLE001 - one bad item must not abort the run
        return ItemRun(
            arm=arm.name,
            item_id=item.id,
            tier=item.tier,
            schema=item.schema,
            harness_error=f"{type(exc).__name__}: {exc}",
        )
    return _record(arm, item, result)


def run_arms(
    corpus: Corpus,
    arms: Sequence[Arm],
    *,
    items: Sequence[CorpusItem],
    client_for: Callable[[str], InferenceClient],
    fixtures: Fixtures,
    concurrency: int = DEFAULT_CONCURRENCY,
    run_id: str,
    live: bool,
    progress: Callable[[str], None] = lambda _: None,
) -> RunResult:
    """Generate concurrently, score serially.

    Scoring is deliberately not parallel: it is milliseconds of local SQLite and
    keeping it single-threaded removes every question about sharing sqlite3
    handles across threads. The expensive part — the network — is the part that
    fans out.
    """
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    arm_results: list[ArmResult] = []

    for arm in arms:
        progress(f"arm {arm.name}: {len(items)} item(s) on {arm.model}")
        client = client_for(arm.model)
        began = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            runs = list(pool.map(lambda it, a=arm, c=client: _run_one(a, it, corpus, c), items))
        elapsed = time.perf_counter() - began

        for run, item in zip(runs, items, strict=True):
            if run.harness_error is not None:
                run.attach(
                    ItemScore(
                        item_id=item.id,
                        tier=item.tier,
                        schema=item.schema,
                        correct=False,
                        correct_strict_names=False,
                        correct_order_insensitive=False,
                        correct_column_subset=False,
                        reason="harness_error",
                        detail=run.harness_error,
                        response_class=None,
                        emitted_query=False,
                        sql_executes=False,
                        safety_violation=None,
                        catalog_access=False,
                        order_sensitive=None,
                        gold_rows=None,
                        candidate_rows=None,
                    )
                )
            else:
                run.attach(
                    score_item(
                        item,
                        response_class=run.response_class,
                        query=run.query,
                        fixtures=fixtures,
                        error_code=run.error_code,
                    )
                )

        correct = sum(1 for run in runs if run.correct)
        progress(
            f"arm {arm.name}: {correct}/{len(runs)} correct "
            f"in {elapsed:.0f}s ({sum(r.attempt_count for r in runs)} API calls)"
        )
        arm_results.append(
            ArmResult(
                arm=arm,
                runs=runs,
                wall_clock_s=elapsed,
                api_calls=sum(run.attempt_count for run in runs),
            )
        )

    return RunResult(
        run_id=run_id,
        started_at=started_at,
        arms=arm_results,
        item_ids=[item.id for item in items],
        live=live,
    )
