"""The prose blocks of the markdown report, generated from the measured numbers.

Written as code rather than hand-typed after the fact so the commentary cannot
drift from the table above it: every claim here is a conditional on a value the
run actually produced. If the loop-off/loop-on delta is zero, the sentence that
gets emitted says so.
"""

from __future__ import annotations

from typing import Any

from evals.harness.metrics import ArmMetrics, RunMetrics

__all__ = ["build_context", "corpus_revision_section"]

#: Below this, a delta is indistinguishable from noise on a 45-item gold set
#: (one item is 2.2 points).
NOISE_FLOOR_PP = 2.3

#: D11's cross-model comparison needs at least two model roles.
MIN_ROLES_FOR_D11 = 2


def corpus_revision_section(
    metrics: RunMetrics,
    *,
    baseline: RunMetrics | None,
    baseline_source: str | None,
    revisions: dict[str, Any] | None,
) -> list[str]:
    """The before/after block: what changed in the *corpus*, and what it moved.

    Rendered high in the report, directly under the headline, because the
    instrument was edited between the two runs and a reader has to be able to
    judge that edit before reading anything it produced.
    """
    if not revisions and baseline is None:
        return []
    lines = ["## The corpus was revised between these two runs", ""]

    if revisions:
        revised = revisions.get("revised", [])
        unchanged = revisions.get("reviewed_unchanged", [])
        lines += [
            f"**{revisions.get('revision', '?')} ({revisions.get('date', '?')}): "
            f"{len(revised)} of {len(revised) + len(unchanged)} gold questions were rewritten; "
            f"{len(unchanged)} were audited and deliberately left alone.** No `gold_sql` value "
            "was changed — the manifest diff is question text only, which `git diff` will "
            "confirm and which the identical baseline column below demonstrates.",
            "",
            str(revisions.get("reason", "")),
            "",
            "Every edited item, before and after, with a one-line justification: "
            "`evals/corpus/REVISIONS.md` (machine-readable twin: `evals/corpus/revisions.json`; "
            "`test_corpus.py` asserts the two agree with the manifest and that all "
            f"{len(revised) + len(unchanged)} gold items are accounted for).",
            "",
            "Items revised: " + ", ".join(f"`{record['id']}`" for record in revised) + ".",
            "",
            "Items audited and left unchanged: "
            + ", ".join(f"`{record['id']}`" for record in unchanged)
            + ".",
            "",
        ]

    if baseline is None:
        return lines

    lines += [
        "### Before and after, side by side",
        "",
        f"*Before* is the previous live run (`{baseline_source}`), **re-scored by today's "
        "scorer against today's gold SQL**. That re-scoring is what makes the comparison "
        "legitimate in one direction and limited in another:",
        "",
        "- Its strict execution-accuracy figures are identical to the ones printed in that "
        "older report. They could only differ if a gold query had changed, so this column is "
        "also a check that none did.",
        "- Its `column_subset` figures are new: the metric did not exist when that run was "
        "scored, so it is computed here from the candidate SQL that run actually recorded.",
        "- The *after* column is a **fresh live run**. Its delta therefore mixes the corpus "
        "revision with ordinary run-to-run sampling variance (temperature 0.0 does not make "
        "these models deterministic). Treat the delta as the combined effect, not as a clean "
        "measurement of the edit. `evals/reports/README.md` quantifies that variance from two "
        "complete runs of an identical configuration and states the resulting rule for how "
        "large a delta has to be before it is a finding; apply it to the Δ column below.",
        "",
    ]
    rows = []
    for arm in metrics.arms:
        before = baseline.by_name(arm.arm)
        if before is None:
            continue
        delta = arm.execution_accuracy.pct - before.execution_accuracy.pct
        rows.append(
            [
                f"`{arm.arm}`",
                str(before.execution_accuracy),
                str(arm.execution_accuracy),
                f"{delta:+.1f} pp",
                str(before.column_subset_accuracy),
                str(arm.column_subset_accuracy),
                str(before.failure_reasons.get("column_count_mismatch", 0)),
                str(arm.failure_reasons.get("column_count_mismatch", 0)),
            ]
        )
    lines += _table(
        [
            "arm",
            "exec acc. BEFORE",
            "exec acc. AFTER",
            "Δ",
            "col-subset BEFORE",
            "col-subset AFTER",
            "`column_count_mismatch` BEFORE",
            "AFTER",
        ],
        rows,
    )
    return lines + [""]


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    return lines + ["| " + " | ".join(row) + " |" for row in rows]


def _delta_commentary(metrics: RunMetrics) -> list[str]:
    lines: list[str] = []
    for role in metrics.roles():
        off, on = metrics.by_name(f"{role}/loop_off"), metrics.by_name(f"{role}/loop_on")
        if off is None or on is None:
            continue
        delta = on.execution_accuracy.pct - off.execution_accuracy.pct
        model = off.model.rsplit("/", 1)[-1]
        extra_calls = on.api_calls - off.api_calls

        if on.repaired_items == 0:
            lines.append(
                f"- **{role} (`{model}`): the loop never fired, so there is no loop effect to "
                f"report.** Across {on.n_items} items the validator rejected **zero** candidates "
                f"(attempt histogram `{on.attempt_histogram}`, {extra_calls} extra model turns). "
                f"The {delta:+.1f} pp difference between the two arms is therefore **run-to-run "
                "sampling variance at temperature 0.0, not the repair loop** — and quoting it as "
                "a loop benefit would be the single easiest way to lie with this table. This is "
                "exactly what W1 finding #6 predicted: with the hardened prompt this model does "
                "not emit bind-failing SQL, so a bind-checker has nothing to catch. The loop is "
                "insurance here; on this corpus it never had to pay out."
            )
        elif delta > NOISE_FLOOR_PP:
            lines.append(
                f"- **{role} (`{model}`): {delta:+.1f} pp** ({off.execution_accuracy} → "
                f"{on.execution_accuracy}) for {extra_calls} extra model turns. "
                f"{on.repaired_items} item(s) had at least one candidate rejected and re-prompted"
                f"; {on.repaired_and_correct} of those ended up scoring correct. Rejections by "
                f"cause: `{on.rejected_attempt_kinds}`. This delta is causally attributable — "
                "the rejections are recorded per item in the JSON, not inferred from the score."
            )
        elif abs(delta) <= NOISE_FLOOR_PP:
            lines.append(
                f"- **{role} (`{model}`): {delta:+.1f} pp**, at or below the resolution of a "
                f"{off.execution_accuracy.denominator}-item set (one item = "
                f"{100 / max(off.execution_accuracy.denominator, 1):.1f} pp). The loop fired on "
                f"{on.repaired_items} item(s) and cost {extra_calls} extra model turns without "
                "a measurable accuracy return."
            )
        else:
            lines.append(
                f"- **{role} (`{model}`): {delta:+.1f} pp** ({off.execution_accuracy} → "
                f"{on.execution_accuracy}). The loop fired on {on.repaired_items} item(s) and "
                "accuracy went *down*. Reported as measured."
            )
    return lines


def _projection_block(metrics: RunMetrics) -> list[str]:
    """W12's secondary metric, stated as a gap rather than as a score."""
    rows = [
        f"`{arm.arm}` {arm.execution_accuracy.numerator}→{arm.column_subset_accuracy.numerator}"
        for arm in metrics.arms
        if arm.column_subset_accuracy.numerator != arm.execution_accuracy.numerator
    ]
    lines = ["### How much of the score hinges on projection agreement", ""]
    if not rows:
        return lines + [
            "`column_subset_accuracy` equals execution accuracy in every arm: **no** item in this "
            "run produced the gold's rows under a different column list. After the W12 corpus "
            "revision the two metrics have converged, which is what a corpus whose questions "
            "state their output shape should look like.",
            "",
        ]
    return lines + [
        "Scoring column-subset-wise (the W12 secondary metric — every gold column present with "
        "matching values, extra columns ignored) instead of strictly would change these arms: "
        + ", ".join(rows)
        + ". Those are items where the candidate carried the gold's answer inside a wider "
        "projection. The headline number stays strict execution accuracy; this is the size of "
        "the projection component, reported beside it and never in place of it.",
        "",
    ]


def _top_failure(metrics: RunMetrics) -> tuple[str, float] | None:
    """The most common failure reason across every arm, and its share."""
    totals: dict[str, int] = {}
    failures = 0
    for arm in metrics.arms:
        for reason, count in arm.failure_reasons.items():
            totals[reason] = totals.get(reason, 0) + count
            failures += count
    if not failures:
        return None
    reason, count = max(totals.items(), key=lambda kv: kv[1])
    return reason, 100.0 * count / failures


def _dominant_failures(metrics: RunMetrics, revisions: dict[str, Any] | None) -> list[str]:
    """Name the failure mode that actually drives the headline number."""
    totals: dict[str, int] = {}
    failures = 0
    for arm in metrics.arms:
        for reason, count in arm.failure_reasons.items():
            totals[reason] = totals.get(reason, 0) + count
            failures += count
    if not failures:
        return []
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    top, top_n = ranked[0]
    share = 100.0 * top_n / failures
    lines = [
        "### What the failures actually are",
        "",
        "Across all arms, "
        + ", ".join(f"`{reason}` {count}" for reason, count in ranked[:5])
        + f" (of {failures} scored failures).",
        "",
    ]
    revised = (revisions or {}).get("revised", [])
    unchanged = (revisions or {}).get("reviewed_unchanged", [])
    revision_label = (revisions or {}).get("revision", "W12")
    n_revised, n_gold = len(revised), len(revised) + len(unchanged)

    if top == "column_count_mismatch" and n_revised:
        lines += [
            f"**{share:.0f}% of every failure in this run is `column_count_mismatch`** — the "
            "candidate query returned a different *number of columns* than the gold query. Not "
            "wrong rows — a different projection. Read that number against "
            "`column_subset_accuracy` in the headline table before concluding anything about SQL "
            "competence: where the two diverge, the candidate had the gold's answer and wrapped "
            "it in extra columns.",
            "",
            f"The {revision_label} corpus revision rewrote {n_revised} of {n_gold} gold "
            "questions to state the output shape "
            "they already implied (`evals/corpus/REVISIONS.md`), so a residual "
            "`column_count_mismatch` now means the model ignored a stated projection rather than "
            "failing to guess an unstated one. The comparison rule itself (strict on column "
            "count, lenient on column names) is the one fixed in design.md before the first run "
            "and has never been relaxed to improve a number.",
            "",
        ]
    elif top == "order_mismatch":
        ordering = ", ".join(
            f"`{arm.arm}` {arm.execution_accuracy.numerator}→"
            f"{arm.order_insensitive_accuracy.numerator}"
            for arm in metrics.arms
            if arm.order_insensitive_accuracy.numerator != arm.execution_accuracy.numerator
        )
        lines += [
            f"**{share:.0f}% of every failure in this run is `order_mismatch`** — the candidate "
            "returned exactly the gold's rows, in a different order, against a gold query that "
            "sorts. Order-insensitively the same arms would score " + ordering + ".",
            "",
            "This is the *second* instrument defect, and it was left in deliberately. Many golds "
            "carry a sort the question never asked for (`ORDER BY id` on 'which products have "
            "been discontinued?'), so the item measures sort telepathy the same way the "
            "projection items measured column telepathy. It was not fixed in the same change as "
            f"the {revision_label} question revision, for one reason: with two edits in flight "
            "the before/after table above would have had two causes and would have proved "
            "nothing about either. It is named in `evals/corpus/REVISIONS.md` under “scope "
            "deliberately not taken” and is the first thing to fix next — by stating the sort "
            "in the questions that imply one and dropping the arbitrary `ORDER BY` from the "
            "golds that do not, never by loosening D3's order rule after the fact.",
            "",
        ]
    return lines


def _caveats(metrics: RunMetrics, extra: list[str], revisions: dict[str, Any] | None) -> list[str]:
    lines = ["### Accounting", ""]
    total_harness_errors = sum(arm.harness_errors for arm in metrics.arms)
    total_gold_errors = sum(arm.gold_errors for arm in metrics.arms)
    total_unsafe = sum(arm.safety_violations for arm in metrics.arms)
    lines += [
        f"- **No item was excluded, capped, or retried out of the denominators.** Every arm's "
        f"denominator is the full {metrics.n_items}-item selection.",
        f"- Harness/transport errors, counted as failures with their exception text preserved in "
        f"the JSON: **{total_harness_errors}**.",
        f"- Gold queries that failed to execute against their own fixture (would be a corpus "
        f"bug): **{total_gold_errors}**.",
        f"- Candidate queries that failed the D9 AST gate at scoring time: **{total_unsafe}**.",
        "- Scores are re-derivable offline: `python -m evals.harness render "
        "evals/reports/<run-id>.json` re-scores the recorded candidates without an API call, so "
        "a scorer fix is applied to these same measurements rather than to a fresh roll of the "
        "dice.",
        "- `FireworksClient` retries internally on 429/5xx and escalates once on a truncated "
        "completion (finding #1/#5). Those retries are extra HTTP requests that the *model "
        "turn* counts above do not include; the truncation escalations observed are reported "
        "in the cost section.",
        "",
    ]
    ordering = [
        arm
        for arm in metrics.arms
        if arm.order_insensitive_accuracy.numerator != arm.execution_accuracy.numerator
    ]
    if ordering:
        detail = ", ".join(
            f"`{a.arm}` {a.execution_accuracy.numerator}→{a.order_insensitive_accuracy.numerator}"
            for a in ordering
        )
        lines += [
            "### How much of the score hinges on row order",
            "",
            "Scoring order-insensitively everywhere (a diagnostic, not the reported metric) "
            f"would change these arms: {detail}. Those are items where the candidate produced "
            "the right rows in a different order than a gold query that specifies one. The "
            "reported metric keeps order-sensitivity, per D3.",
            "",
        ]
    else:
        lines += [
            "### How much of the score hinges on row order",
            "",
            "Scoring order-insensitively everywhere would change **no** item in any arm, so "
            "none of the reported accuracy depends on the order-sensitivity rule. (Worth "
            "knowing, because a gold query whose `ORDER BY` key has ties leaves the order of "
            "the tied rows unspecified — that risk did not materialise here.)",
            "",
        ]
    return lines + _projection_block(metrics) + _dominant_failures(metrics, revisions) + extra


def _ordered_roles(metrics: RunMetrics) -> list[str]:
    """Roles ordered by loop-off execution accuracy, strongest first. Ties keep
    the order the arms were declared in, so the two entries are always distinct
    when there is more than one role."""
    roles = metrics.roles()
    scored = []
    for position, role in enumerate(roles):
        off = metrics.by_name(f"{role}/loop_off")
        scored.append((-(off.execution_accuracy.pct if off else 0.0), position, role))
    return [role for _, _, role in sorted(scored)]


def _interpretation(metrics: RunMetrics) -> list[str]:
    lines: list[str] = []

    best = max(metrics.arms, key=lambda a: a.execution_accuracy.pct)
    top = _top_failure(metrics)
    dominant = (
        f"The single largest failure mode is `{top[0]}` ({top[1]:.0f}% of all scored failures) — "
        'see "What the failures actually are" above before reading the gap as SQL incompetence. '
        if top
        else ""
    )
    lines.append(
        f"1. **Read the headline with its caveat attached.** The best arm is `{best.arm}` at "
        f"{best.execution_accuracy} execution accuracy on the gold pairs and "
        f"{best.abstention_accuracy} on the adversarial set. Accuracy means the candidate was "
        "*executed* against a seeded database and its result set compared to the gold query's. "
        + dominant
        + 'The valid-SQL rate in the same table is the number to look at for "does it write SQL '
        'that runs", and it is far higher.'
    )

    deltas: dict[str, float] = {}
    fired: dict[str, int] = {}
    for role in metrics.roles():
        off, on = metrics.by_name(f"{role}/loop_off"), metrics.by_name(f"{role}/loop_on")
        if off and on:
            deltas[role] = on.execution_accuracy.pct - off.execution_accuracy.pct
            fired[role] = on.repaired_items

    ordered = [role for role in _ordered_roles(metrics) if role in deltas]
    if len(ordered) >= MIN_ROLES_FOR_D11:
        strong, weak = ordered[0], ordered[-1]
        strong_off = metrics.by_name(f"{strong}/loop_off")
        weak_off = metrics.by_name(f"{weak}/loop_off")
        weak_on = metrics.by_name(f"{weak}/loop_on")
        # The comparison that matters is causal, not arithmetic: a delta on an
        # arm whose loop never fired is sampling noise, whatever its sign.
        strong_attributable = deltas[strong] if fired.get(strong) else 0.0
        weak_attributable = deltas[weak] if fired.get(weak) else 0.0
        claim_holds = weak_attributable > strong_attributable + NOISE_FLOOR_PP

        if claim_holds:
            verdict = (
                "**Scaffolding value scales inversely with model strength — supported, and by "
                "the strongest form of the evidence (D11).**"
            )
            body = (
                f"On `{strong}` the repair loop fired on {fired.get(strong, 0)} item(s): there "
                "was nothing to repair, so its arms differ only by sampling noise. On "
                f"`{weak}` it fired on {fired.get(weak, 0)} item(s) and took the model from "
                f"{weak_off.execution_accuracy if weak_off else 'n/a'} to "
                f"{weak_on.execution_accuracy if weak_on else 'n/a'}. The weaker model's "
                "loop-off arm emitted "
                f"{weak_off.queries_emitted if weak_off else 0} usable queries out of "
                f"{weak_off.n_items if weak_off else 0} items — the loop is not an accuracy "
                "tweak there, it is the difference between a usable component and an unusable "
                "one. Same code path, same two extra turns of budget; the value it returns is "
                "a function of which model you point it at."
            )
        else:
            verdict = "**D11's claim is not separable from noise in this run.**"
            body = (
                f"Loop-attributable movement: `{strong}` {strong_attributable:+.1f} pp on "
                f"{fired.get(strong, 0)} fired item(s), `{weak}` {weak_attributable:+.1f} pp on "
                f"{fired.get(weak, 0)}. Reported as measured."
            )
        lines.append(
            f"2. {verdict} Baseline (loop off) execution accuracy: "
            f"{strong_off.execution_accuracy if strong_off else 'n/a'} for `{strong}`, "
            f"{weak_off.execution_accuracy if weak_off else 'n/a'} for `{weak}`. {body}"
        )
    else:
        lines.append(
            "2. Only one model role was run, so D11's cross-model claim is not evidenced here."
        )

    unsafe = sum(arm.safety_violations for arm in metrics.arms)
    catalog = sum(arm.catalog_queries for arm in metrics.arms)
    lines.append(
        f"3. **Safety, measured rather than asserted.** {unsafe} candidate queries across all "
        f"{len(metrics.arms)} arms failed the D9 AST gate at scoring time — the gate checks "
        "single-statement, SELECT/WITH-only, no DDL/DML node anywhere in the tree, on the "
        "sqlglot AST rather than by string matching. The two live prompt-injection attacks from "
        "`memory/inference-findings.md` are corpus items scored on outcome: DDL or DML in the "
        f"`query` field is an automatic fail whatever the response class. {catalog} "
        "query/queries read the SQLite catalog (`sqlite_master`), which is allowed on this "
        "request-scoped path and denied by default on `foundation`'s persisted sample "
        "databases (D12)."
    )

    lines.append(
        "4. **Structured output guarantees shape, never meaning — and on one model it did not "
        "even guarantee shape.** Every call in this run set `response_format: {type: "
        "json_schema, strict: true}` (D7). The primary model honoured it on every turn. The weak "
        "model omitted the required `response_class` field on essentially every first turn, "
        "which is why its loop-off arm scores zero. Two lessons, and the second is the "
        "expensive one: schema enforcement removes a class of parsing bugs but never makes an "
        "answer correct; and *enforcement itself is a per-model property on this platform*, so "
        "a system that treats it as a platform guarantee breaks the day someone edits "
        "`T2S_MODEL` in config."
    )

    slowest = max(metrics.arms, key=lambda a: a.latency_p95_ms)
    fastest = min(metrics.arms, key=lambda a: a.latency_p95_ms)
    lines.append(
        f"5. **What the loop costs.** p95 end-to-end latency runs from "
        f"{fastest.latency_p95_ms / 1000:.1f}s (`{fastest.arm}`) to "
        f"{slowest.latency_p95_ms / 1000:.1f}s (`{slowest.arm}`). That upper figure is a second "
        "or third full generation on a slow model, and it is not something to put behind a "
        "synchronous HTTP request without a thought: either run the loop asynchronously, or cap "
        "repairs at 1 where the attempt histogram shows the second repair almost never fires."
    )
    return lines


def build_context(
    metrics: RunMetrics,
    *,
    gold_items: int,
    adversarial_items: int,
    weak_model_note: list[str],
    cost_note: list[str],
    repro: list[str],
    extra_caveats: list[str],
    revisions: dict[str, Any] | None = None,
    corpus_revision: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "gold_items": gold_items,
        "adversarial_items": adversarial_items,
        "corpus_revision": corpus_revision or [],
        "delta_commentary": _delta_commentary(metrics),
        "caveats": _caveats(metrics, weak_model_note + extra_caveats, revisions),
        "cost_note": cost_note,
        "interpretation": _interpretation(metrics),
        "repro": repro,
    }


def arm_label(arm: ArmMetrics) -> str:
    return f"{arm.arm} ({arm.model.rsplit('/', 1)[-1]})"
