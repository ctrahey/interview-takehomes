"""Aggregate a run into the numbers design §7 asks for.

Everything in here is arithmetic over recorded facts. No metric is estimated, no
item is excluded: ``n_items`` per arm always equals the number of items the run
was asked to execute, and items that errored in the harness are counted as
failures with their exception text preserved.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from evals.harness.runner import ArmResult, ItemRun, RunResult

__all__ = ["ArmMetrics", "Ratio", "RunMetrics", "percentile", "summarize"]

GOLD_TIERS: tuple[str, ...] = ("easy", "medium", "hard")
ADVERSARIAL_TIER = "adversarial"
P50, P95 = 0.50, 0.95


def percentile(values: list[int], q: float) -> int:
    """Nearest-rank percentile. With ~53 samples per arm, interpolation would be
    false precision."""
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass(frozen=True, slots=True)
class Ratio:
    numerator: int
    denominator: int

    @property
    def pct(self) -> float:
        return 100.0 * self.numerator / self.denominator if self.denominator else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"n": self.numerator, "of": self.denominator, "pct": round(self.pct, 1)}

    def __str__(self) -> str:
        if not self.denominator:
            return "n/a"
        return f"{self.pct:.1f}% ({self.numerator}/{self.denominator})"


@dataclass(slots=True)
class ArmMetrics:
    arm: str
    role: str
    model: str
    loop: bool
    n_items: int
    execution_accuracy: Ratio
    accuracy_by_tier: dict[str, Ratio]
    strict_name_accuracy: Ratio
    order_insensitive_accuracy: Ratio
    abstention_accuracy: Ratio
    corpus_accuracy: Ratio
    valid_sql_rate: Ratio
    queries_emitted: int
    safety_violations: int
    catalog_queries: int
    gold_errors: int
    harness_errors: int
    latency_p50_ms: int
    latency_p95_ms: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    api_calls: int
    wall_clock_s: float
    attempt_histogram: dict[str, int] = field(default_factory=dict)
    repaired_items: int = 0
    repaired_and_correct: int = 0
    repair_exhausted: int = 0
    failure_reasons: dict[str, int] = field(default_factory=dict)
    #: Which of the four repairable classes rejected a candidate, counted over
    #: every attempt in the arm. This is what says *why* the loop mattered:
    #: ``binder`` is a SQL bug, ``envelope_invariant``/``invalid_json`` is the
    #: model failing to honour the response contract.
    rejected_attempt_kinds: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "arm": self.arm,
            "role": self.role,
            "model": self.model,
            "loop": self.loop,
            "n_items": self.n_items,
            "execution_accuracy": self.execution_accuracy.as_dict(),
            "accuracy_by_tier": {k: v.as_dict() for k, v in self.accuracy_by_tier.items()},
            "strict_name_accuracy": self.strict_name_accuracy.as_dict(),
            "order_insensitive_accuracy": self.order_insensitive_accuracy.as_dict(),
            "abstention_accuracy": self.abstention_accuracy.as_dict(),
            "corpus_accuracy": self.corpus_accuracy.as_dict(),
            "valid_sql_rate": self.valid_sql_rate.as_dict(),
            "queries_emitted": self.queries_emitted,
            "safety_violations": self.safety_violations,
            "catalog_queries": self.catalog_queries,
            "gold_errors": self.gold_errors,
            "harness_errors": self.harness_errors,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "tokens": {
                "prompt": self.prompt_tokens,
                "completion": self.completion_tokens,
                "total": self.total_tokens,
            },
            "api_calls": self.api_calls,
            "wall_clock_s": round(self.wall_clock_s, 1),
            "attempt_histogram": self.attempt_histogram,
            "repaired_items": self.repaired_items,
            "repaired_and_correct": self.repaired_and_correct,
            "repair_exhausted": self.repair_exhausted,
            "failure_reasons": self.failure_reasons,
            "rejected_attempt_kinds": self.rejected_attempt_kinds,
        }
        return data


@dataclass(slots=True)
class RunMetrics:
    run_id: str
    started_at: str
    live: bool
    n_items: int
    item_ids: list[str]
    arms: list[ArmMetrics]

    def by_name(self, name: str) -> ArmMetrics | None:
        return next((arm for arm in self.arms if arm.arm == name), None)

    def roles(self) -> list[str]:
        seen: list[str] = []
        for arm in self.arms:
            if arm.role not in seen:
                seen.append(arm.role)
        return seen

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "live": self.live,
            "n_items": self.n_items,
            "item_ids": self.item_ids,
            "arms": [arm.as_dict() for arm in self.arms],
        }


def _ratio(runs: list[ItemRun], predicate: str) -> Ratio:
    return Ratio(sum(1 for r in runs if r.score and r.score[predicate]), len(runs))


def _summarize_arm(result: ArmResult) -> ArmMetrics:
    runs = result.runs
    gold = [r for r in runs if r.tier in GOLD_TIERS]
    adversarial = [r for r in runs if r.tier == ADVERSARIAL_TIER]
    emitted = [r for r in runs if r.score and r.score["emitted_query"]]
    executes = [r for r in emitted if r.score and r.score["sql_executes"]]

    reasons = Counter(r.score["reason"] for r in runs if r.score and not r.score["correct"])
    attempts = Counter(str(r.attempt_count) for r in runs)
    rejected = Counter(
        str(a.get("failure_kind") or "unknown") for r in runs for a in r.attempts if not a.get("ok")
    )
    repaired = [r for r in runs if r.winning_attempt is not None and r.winning_attempt > 0]

    latencies = [r.latency_ms for r in runs if r.harness_error is None]

    return ArmMetrics(
        arm=result.arm.name,
        role=result.arm.role,
        model=result.arm.model,
        loop=result.arm.loop,
        n_items=len(runs),
        execution_accuracy=_ratio(gold, "correct"),
        accuracy_by_tier={
            tier: _ratio([r for r in gold if r.tier == tier], "correct") for tier in GOLD_TIERS
        },
        strict_name_accuracy=_ratio(gold, "correct_strict_names"),
        order_insensitive_accuracy=_ratio(gold, "correct_order_insensitive"),
        abstention_accuracy=_ratio(adversarial, "correct"),
        corpus_accuracy=_ratio(runs, "correct"),
        valid_sql_rate=Ratio(len(executes), len(emitted)),
        queries_emitted=len(emitted),
        safety_violations=sum(1 for r in runs if r.score and r.score["safety_violation"]),
        catalog_queries=sum(1 for r in runs if r.score and r.score["catalog_access"]),
        gold_errors=sum(
            1 for r in runs if r.score and str(r.score["reason"]).startswith("gold_error")
        ),
        harness_errors=sum(1 for r in runs if r.harness_error is not None),
        latency_p50_ms=percentile(latencies, P50),
        latency_p95_ms=percentile(latencies, P95),
        prompt_tokens=sum(r.prompt_tokens for r in runs),
        completion_tokens=sum(r.completion_tokens for r in runs),
        total_tokens=sum(r.total_tokens for r in runs),
        api_calls=result.api_calls,
        wall_clock_s=result.wall_clock_s,
        attempt_histogram=dict(sorted(attempts.items())),
        repaired_items=len(repaired),
        repaired_and_correct=sum(1 for r in repaired if r.correct),
        repair_exhausted=sum(
            1 for r in runs if (r.error_code or "").startswith("repair_exhausted")
        ),
        failure_reasons=dict(reasons.most_common()),
        rejected_attempt_kinds=dict(rejected.most_common()),
    )


def summarize(run: RunResult) -> RunMetrics:
    return RunMetrics(
        run_id=run.run_id,
        started_at=run.started_at,
        live=run.live,
        n_items=len(run.item_ids),
        item_ids=list(run.item_ids),
        arms=[_summarize_arm(arm) for arm in run.arms],
    )
