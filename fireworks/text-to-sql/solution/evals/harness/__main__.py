"""``python -m evals.harness`` — the live eval entry point (``make eval``).

    python -m evals.harness run   [--arms ...] [--limit N] [--model role=id]
    python -m evals.harness smoke [--models a,b,c] [--limit N]

``run`` executes the corpus across the D11 arms and writes JSON + markdown into
``evals/reports/``. ``smoke`` is the cheap probe that picks the weak arm: it runs
a handful of items, loop off, against each candidate model and reports which of
them actually produce failures — a weak arm with zero failures teaches nothing,
so the choice is made by measurement, not by the model's name (D11).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evals.harness import narrative, report
from evals.harness.corpus import Corpus, load_corpus
from evals.harness.fake import FakeOracleClient
from evals.harness.metrics import summarize
from evals.harness.runner import DEFAULT_CONCURRENCY, build_arms, rebuild, run_arms
from evals.harness.sandbox import fixtures
from evals.harness.scoring import score_item
from pydantic import SecretStr

from t2s_core import FireworksClient, FireworksConfig, InferenceClient
from t2s_core.config import DEFAULT_MODEL

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"
REVISIONS_PATH = Path(__file__).resolve().parent.parent / "corpus" / "revisions.json"

#: D11's candidate list for the weak arm, in its stated preference order.
WEAK_CANDIDATES: tuple[str, ...] = (
    "accounts/fireworks/models/muse-glimmer-30b",
    "accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b",
    "accounts/fireworks/models/glm-5p3-flash",
)

SMOKE_ITEM_COUNT = 5

#: Recorded here, not in a commit message, because the report quotes it. Written
#: after reading `evals/reports/weak-model-smoke.json`; re-run `smoke` and revise
#: this if the candidate list or the platform changes.
WEAK_ARM_RATIONALE: tuple[str, ...] = (
    "**Picked: `muse-glimmer-30b`** — D11's first-preference candidate, and the probe backs the "
    "preference up on both criteria that matter.",
    "",
    "1. *It fails enough to be informative.* 4 of 5 probe items failed, against 2/5 for "
    "`nemotron-lightning-3p5-30b-a3b` and 3/5 for `glm-5p3-flash`.",
    "2. *It fails in a way the loop can act on.* Its failures are **envelope-invariant** "
    "failures: it omits the required `response_class` field from the JSON envelope, despite "
    "`response_format: {type: json_schema, strict: true}` being set on every call. The other "
    "two candidates' failures were almost all semantic (wrong column projection, wrong row "
    "order) — real errors, but ones no validator can detect and no repair turn can fix, so "
    "either would have produced a near-zero loop delta for an uninformative reason.",
    "",
    "Stating that plainly, because it cuts both ways: the weak arm's loop delta below is driven "
    "largely by *response-contract* repair rather than by *SQL* repair. That is still the same "
    "loop and the same code path — W1's design deliberately routes all four repairable failure "
    "classes through one repair turn — but a reader should not take the delta as evidence that "
    "the loop fixes bad joins. On this corpus it mostly fixes malformed envelopes.",
    "",
    "It is also a finding in its own right, and one worth more than the eval it enabled: "
    "**`strict: true` JSON-schema enforcement is not uniform across models on this platform.** "
    "Four models honoured it during W1 probing (`memory/status.md`); this one does not. Any "
    "system that treats structured output as a guarantee rather than as a strong hint will "
    "break the first time someone swaps the model id in config.",
)
GOLD_TIERS = ("easy", "medium", "hard")


class _TruncationCounter(logging.Handler):
    """Finding #1/#5: ``FireworksClient`` escalates once on a truncated
    completion. That is an extra HTTP request the model-turn counts cannot see,
    so it is counted here and reported rather than quietly absorbed."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.truncations = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "truncated" in record.getMessage():
            self.truncations += 1


#: Written after the W12 audit of all 45 gold items, and repeated in
#: `evals/corpus/REVISIONS.md`. These are ways the *corpus* is still ill-posed
#: that W12 deliberately did not fix, because each one changes which rows or
#: values are correct rather than which columns to return — and fixing more
#: than one thing at a time would have made the before/after comparison
#: uninterpretable. Listed here so the report carries them rather than leaving
#: a reader to infer them from the failure table.
KNOWN_OPEN_DEFECTS: tuple[str, ...] = (
    "### Known open defects in the corpus (audited, not fixed)",
    "",
    "The W12 audit read all 45 gold items. It fixed exactly one class of ill-posedness — "
    "questions that did not state their output shape — and found three more that it left alone. "
    "They are named here because they are now what most of the remaining gap is made of, and "
    "because a reader should not have to reverse-engineer them from the failure counts:",
    "",
    "1. **Row order.** Several golds sort on something the question never asked for (`ORDER BY "
    "id`). See the order-insensitive diagnostic above for the size of this.",
    '2. **Row inclusion.** `library-m01` ("how many books in each category") and `library-m04` '
    "do not say whether a category with zero books should appear; the gold says no, an inner "
    "join, and a candidate using an outer join returns 13 rows against the gold's 9. `events-m01` "
    "has the same shape for users with no purchases.",
    "3. **Rounding and units.** `retail-h05`, `library-h05`, `library-m04` and `events-m05` round "
    "in the gold (`ROUND(x, 2)`) where the question says nothing about precision, so an unrounded "
    "candidate is a `value_mismatch` at the sixth decimal place. `events-m01`'s "
    "cents-versus-dollars question is deliberately left to a domain corrective (D13), not to the "
    "question text.",
    "",
    "Each of these is the same kind of defect as the one W12 fixed, and each would move the "
    "headline number. They are open, dated and attributable rather than quietly absorbed.",
    "",
)


def _api_key() -> str:
    key = os.environ.get("FIREWORKS_API_KEY", "")
    if key:
        return key
    path = Path.home() / ".fireworks-key"
    if path.exists():
        # Opaque data. Read, never sourced (see memory/status.md, W0 incident).
        return path.read_text(encoding="utf-8").strip()
    raise SystemExit(
        "FIREWORKS_API_KEY is not set and ~/.fireworks-key does not exist. "
        "The live eval needs one; `make test` runs the offline suite instead."
    )


def _revisions() -> dict[str, Any] | None:
    """The corpus revision record, if the corpus has ever been revised."""
    if not REVISIONS_PATH.exists():
        return None
    data: dict[str, Any] = json.loads(REVISIONS_PATH.read_text(encoding="utf-8"))
    return data


def _rescore(payload: dict[str, Any], corpus: Corpus, built: Any) -> Any:
    """Score a saved run's recorded candidates with *today's* scorer.

    Used for the baseline column of a before/after report. Scoring never reads
    the question — only the gold SQL and the candidate — so re-scoring an older
    run against a corpus whose questions were revised is exactly the right
    comparison, and its strict accuracy is unchanged unless a gold query moved.
    """
    run = rebuild(payload)
    by_id = {item.id: item for item in corpus.items}
    for arm in run.arms:
        for item_run in arm.runs:
            if item_run.harness_error is not None or item_run.item_id not in by_id:
                continue
            item_run.attach(
                score_item(
                    by_id[item_run.item_id],
                    response_class=item_run.response_class,
                    query=item_run.query,
                    fixtures=built,
                    error_code=item_run.error_code,
                )
            )
    return summarize(run)


def _latency_note(concurrency: int) -> list[str]:
    return [
        "### What the latency numbers do and do not include",
        "",
        f"Every arm ran its items with a concurrency of {concurrency}, and the p50/p95 figures "
        "are wall-clock per item measured client-side. They therefore include **provider-side "
        "queueing under our own load**, not just model compute: {concurrency} of our requests "
        "are in flight at once against a shared endpoint. These are not single-request latency "
        "numbers and must not be quoted as a serving SLO. The comparison between arms is still "
        "fair — every arm was measured the same way — but the absolute p95 would be lower for "
        "an unloaded single request.".replace("{concurrency}", str(concurrency)),
        "",
    ]


def _live_client_factory(key: str) -> Any:
    cache: dict[str, InferenceClient] = {}

    def factory(model: str) -> InferenceClient:
        if model not in cache:
            cache[model] = FireworksClient(
                FireworksConfig(api_key=SecretStr(key), model=model, max_retries=4)
            )
        return cache[model]

    return factory


def _parse_models(values: Sequence[str] | None, weak_default: str) -> dict[str, str]:
    models = {
        "primary": os.environ.get("T2S_MODEL", DEFAULT_MODEL),
        "weak": os.environ.get("T2S_WEAK_MODEL", weak_default),
    }
    for value in values or ():
        role, _, model = value.partition("=")
        if not model:
            models["primary"] = role
        else:
            models[role] = model
    return models


def _echo(message: str) -> None:
    print(f"[eval] {message}", flush=True)  # noqa: T201 - this is a CLI


# ---------------------------------------------------------------------------
# smoke — pick the weak arm by measurement
# ---------------------------------------------------------------------------
def cmd_smoke(args: argparse.Namespace) -> int:
    corpus = load_corpus()
    candidates = (
        [m.strip() for m in args.models.split(",") if m.strip()]
        if args.models
        else list(WEAK_CANDIDATES)
    )
    # A spread across tiers plus one adversarial: a candidate that fails only on
    # `easy` and one that fails only on `hard` are different animals.
    picks = _smoke_items(corpus, args.limit)
    _echo(f"smoke: {len(picks)} item(s) × {len(candidates)} model(s), loop OFF")

    factory = _live_client_factory(_api_key())
    results: list[dict[str, Any]] = []
    with fixtures(corpus) as built:
        for model in candidates:
            arms = build_arms({"candidate": model}, only=["candidate/loop_off"])
            run = run_arms(
                corpus,
                arms,
                items=picks,
                client_for=factory,
                fixtures=built,
                concurrency=min(args.concurrency, len(picks)),
                run_id=f"smoke-{int(time.time())}",
                live=True,
                progress=_echo,
            )
            arm = run.arms[0]
            failures = [
                {
                    "item": item.item_id,
                    "tier": item.tier,
                    "reason": (item.score or {}).get("reason"),
                    "detail": str((item.score or {}).get("detail") or "")[:200],
                }
                for item in arm.runs
                if not item.correct
            ]
            results.append(
                {
                    "model": model,
                    "correct": sum(1 for item in arm.runs if item.correct),
                    "n": len(arm.runs),
                    "failures": failures,
                    "wall_clock_s": round(arm.wall_clock_s, 1),
                    "api_calls": arm.api_calls,
                }
            )
            _echo(
                f"  {model.rsplit('/', 1)[-1]}: {results[-1]['correct']}/{results[-1]['n']} "
                f"correct, {len(failures)} failure(s)"
            )

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / "weak-model-smoke.json"
    out.write_text(
        json.dumps(
            {
                "purpose": (
                    "D11: pick the weak eval arm by measurement. A candidate that produces "
                    "zero failures on this probe teaches nothing about the repair loop."
                ),
                "items": [item.id for item in picks],
                "arm": "loop_off (NoOpValidator, max_repair_attempts=0)",
                "results": results,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _echo(f"wrote {out}")
    return 0


def _smoke_items(corpus: Corpus, limit: int) -> tuple[Any, ...]:
    picks = []
    for tier in (*GOLD_TIERS, "adversarial"):
        tiered = [i for i in corpus.items if i.tier == tier]
        if tiered:
            picks.append(tiered[0])
    # Top up with the hardest remaining items until we hit the budget.
    for item in corpus.items:
        if len(picks) >= limit:
            break
        if item.tier == "hard" and item not in picks:
            picks.append(item)
    return tuple(picks[:limit])


# ---------------------------------------------------------------------------
# render — re-score and re-render a finished run, offline
# ---------------------------------------------------------------------------
def cmd_render(args: argparse.Namespace) -> int:
    """Rebuild the report from a saved run, with zero API calls.

    Every input the scorer needs is already in the JSON — the response class,
    the candidate SQL, the error code — and the fixtures are deterministic. So a
    scorer fix or a wording change is re-applied to the *same* measurements
    rather than being an excuse to re-roll the dice on a fresh run.
    """
    source = Path(args.report)
    payload = json.loads(source.read_text(encoding="utf-8"))
    run = rebuild(payload)
    corpus = load_corpus()
    by_id = {item.id: item for item in corpus.items}
    items = [by_id[item_id] for item_id in run.item_ids]

    baseline = None
    with fixtures(corpus) as built:
        for arm in run.arms:
            for item_run in arm.runs:
                if item_run.harness_error is not None:
                    continue
                item_run.attach(
                    score_item(
                        by_id[item_run.item_id],
                        response_class=item_run.response_class,
                        query=item_run.query,
                        fixtures=built,
                        error_code=item_run.error_code,
                    )
                )
        if args.baseline:
            baseline = _rescore(
                json.loads(Path(args.baseline).read_text(encoding="utf-8")), corpus, built
            )

    metrics = summarize(run)
    old_context = payload.get("context", {})
    revisions = _revisions()
    corpus_revision = (
        narrative.corpus_revision_section(
            metrics,
            baseline=baseline,
            baseline_source=Path(args.baseline).name,
            revisions=revisions,
        )
        if args.baseline
        else old_context.get("corpus_revision", [])
    )
    context = narrative.build_context(
        metrics,
        gold_items=sum(1 for i in items if not i.is_adversarial),
        adversarial_items=sum(1 for i in items if i.is_adversarial),
        weak_model_note=_weak_model_note(),
        cost_note=old_context.get("cost_note", []),
        repro=old_context.get("repro", []),
        extra_caveats=list(KNOWN_OPEN_DEFECTS) + _latency_note(DEFAULT_CONCURRENCY),
        revisions=revisions,
        corpus_revision=corpus_revision,
    )
    json_path, md_path = report.write_reports(
        run, metrics, directory=source.parent, context=context
    )
    for summary in metrics.arms:
        _echo(
            f"{summary.arm}: {summary.execution_accuracy} gold, "
            f"{summary.abstention_accuracy} adversarial"
        )
    _echo(f"re-rendered {md_path} and {json_path} (no API calls)")
    return 0


# ---------------------------------------------------------------------------
# run — the real thing
# ---------------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    corpus = load_corpus()
    models = _parse_models(args.model, args.weak_model)
    arms = build_arms(models, only=args.arms.split(",") if args.arms else None)
    if not arms:
        raise SystemExit(f"--arms {args.arms!r} selected no arms; valid roles: {list(models)}")

    items = corpus.select(limit=args.limit)
    gold = [i for i in items if not i.is_adversarial]
    adversarial = [i for i in items if i.is_adversarial]

    counter = _TruncationCounter()
    logging.getLogger("t2s_core.inference").addHandler(counter)
    logging.getLogger("t2s_core.inference").setLevel(logging.WARNING)

    if args.fake:
        client = FakeOracleClient(corpus, corrupt_pct=args.fake_corrupt)
        factory: Any = lambda _model: client  # noqa: E731 - one-liner test double
        live = False
    else:
        factory = _live_client_factory(_api_key())
        live = True

    run_id = args.run_id or time.strftime("run-%Y%m%d-%H%M%S")
    _echo(f"{run_id}: {len(items)} item(s) × {len(arms)} arm(s) — live={live}")
    for arm in arms:
        _echo(
            f"  {arm.name}: {arm.model} (validator={arm.validator_name}, "
            f"max_repair_attempts={arm.max_repair_attempts})"
        )

    with fixtures(corpus) as built:
        result = run_arms(
            corpus,
            arms,
            items=items,
            client_for=factory,
            fixtures=built,
            concurrency=args.concurrency,
            run_id=run_id,
            live=live,
            progress=_echo,
        )
        metrics = summarize(result)
        baseline = None
        if args.baseline:
            baseline_payload = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            baseline = _rescore(baseline_payload, corpus, built)
            _echo(f"baseline {Path(args.baseline).name}: re-scored with today's scorer")

    revisions = _revisions()
    context = narrative.build_context(
        metrics,
        gold_items=len(gold),
        adversarial_items=len(adversarial),
        weak_model_note=_weak_model_note(),
        cost_note=_cost_note(counter.truncations, metrics),
        repro=_repro(args, models, run_id),
        extra_caveats=_selection_note(corpus, items)
        + list(KNOWN_OPEN_DEFECTS)
        + _latency_note(args.concurrency),
        revisions=revisions,
        corpus_revision=narrative.corpus_revision_section(
            metrics,
            baseline=baseline,
            baseline_source=Path(args.baseline).name if args.baseline else None,
            revisions=revisions,
        ),
    )
    json_path, md_path = report.write_reports(
        result, metrics, directory=Path(args.out), context=context
    )
    _echo(f"wrote {md_path}")
    _echo(f"wrote {json_path}")
    return 0


def _selection_note(corpus: Corpus, items: Sequence[Any]) -> list[str]:
    if len(items) == len(corpus.items):
        return []
    skipped = [i.id for i in corpus.items if i not in items]
    return [
        "### Partial run",
        "",
        f"This run executed {len(items)} of {len(corpus.items)} corpus items "
        f"(`--limit`). Not executed, and therefore in no denominator: "
        + ", ".join(f"`{i}`" for i in skipped),
        "",
    ]


def _weak_model_note() -> list[str]:
    smoke = REPORTS_DIR / "weak-model-smoke.json"
    if not smoke.exists():
        return []
    data = json.loads(smoke.read_text(encoding="utf-8"))
    rows = []
    for r in data.get("results", []):
        causes = ", ".join(sorted({f"`{f['reason']}`" for f in r["failures"]})) or "—"
        rows.append(
            f"| `{r['model'].rsplit('/', 1)[-1]}` | {r['correct']}/{r['n']} | "
            f"{len(r['failures'])} | {causes} |"
        )
    return [
        "### How the weak arm was chosen",
        "",
        "D11 names three candidates in preference order and requires the pick to be made by a "
        "smoke test rather than by name, because a weak arm with zero failures teaches nothing. "
        f"Probe: items `{', '.join(data.get('items', []))}`, loop off, one turn each.",
        "",
        "| candidate | correct | failures | failure reasons |",
        "|---|---|---|---|",
        *rows,
        "",
        *WEAK_ARM_RATIONALE,
        "",
        "Raw probe output: `evals/reports/weak-model-smoke.json`.",
        "",
    ]


def _cost_note(truncations: int, metrics: Any) -> list[str]:
    overheads = []
    for role in metrics.roles():
        off, on = metrics.by_name(f"{role}/loop_off"), metrics.by_name(f"{role}/loop_on")
        if off and on and off.api_calls:
            extra = 100.0 * (on.api_calls - off.api_calls) / off.api_calls
            overheads.append(f"`{role}` +{extra:.0f}% model turns")
    return [
        f"Truncation escalations observed (finding #1/#5 — one extra HTTP request each, not "
        f"counted as a model turn above): **{truncations}**.",
        "",
        "**The token totals above under-count, and by an amount this harness cannot recover.** "
        "When a completion is truncated at `max_tokens`, `FireworksClient` raises before it "
        "builds an `InferenceResponse` and retries once at a larger budget; the tokens the "
        "truncated attempt actually burned are never surfaced to the caller, so they are in the "
        "bill but not in this table. The floor on the shortfall is "
        f"{truncations} truncated completion(s) × that attempt's `max_tokens`. Reported rather "
        "than quietly rounded away; the direction of the error is known (totals are too low) "
        "even though its size is not.",
        "",
        "**Dollar cost is NOT measured.** The Fireworks `/v1/models` endpoint returns no pricing "
        "field for these model IDs and this harness does not read a price list, so quoting a "
        "dollar figure would be a number we made up. What is measured is the token count in the "
        "table above; multiply by whatever per-token rate the account is actually billed at. "
        "The engineering point does not need the dollar figure: the loop's incremental cost is "
        "measured directly as extra model turns over the loop-off baseline — "
        + (", ".join(overheads) if overheads else "not comparable in this run")
        + ".",
    ]


def _repro(args: argparse.Namespace, models: dict[str, str], run_id: str) -> list[str]:
    return [
        "```sh",
        "# offline: the harness's own unit tests (scorer edge cases, sandbox, runner)",
        "make check UV=$HOME/.local/bin/uv",
        "",
        "# live: this run",
        "make eval UV=$HOME/.local/bin/uv   # == python -m evals.harness run",
        "",
        "# live, with the before/after section this report carries:",
        f"python -m evals.harness run --baseline {args.baseline}"
        if args.baseline
        else "# (no --baseline was passed to this run)",
        "```",
        "",
        f"Models: primary `{models.get('primary')}`, weak `{models.get('weak')}`. "
        f"Concurrency {args.concurrency}. Run id `{run_id}`. Temperature 0.0 on every call "
        "(`t2s_core` pins it), but these are large MoE models served at scale — identical "
        "inputs are not guaranteed to produce identical outputs, so a re-run will land near "
        "these numbers, not exactly on them.",
        "",
        "Every item's full trace — each attempt's candidate SQL, the validator verdict that "
        f"rejected it, tokens and latency — is in `evals/reports/{run_id}.json`.",
    ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.harness")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the corpus across the D11 arms")
    run.add_argument("--arms", help="comma-separated arm or role names, e.g. 'primary/loop_on'")
    run.add_argument("--limit", type=int, help="run only the first N corpus items")
    run.add_argument(
        "--model",
        action="append",
        help="role=model-id (repeatable), or a bare model id for the primary role",
    )
    run.add_argument("--weak-model", default=WEAK_CANDIDATES[0])
    run.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    run.add_argument("--out", default=str(REPORTS_DIR))
    run.add_argument("--run-id")
    run.add_argument(
        "--baseline",
        help="path to an earlier evals/reports/<run-id>.json; its recorded candidates are "
        "re-scored with today's scorer to render a before/after section",
    )
    run.add_argument("--fake", action="store_true", help="offline oracle client; not evidence")
    run.add_argument("--fake-corrupt", type=int, default=0)
    run.set_defaults(func=cmd_run)

    render = sub.add_parser(
        "render", help="re-score and re-render a saved run JSON, offline, no API calls"
    )
    render.add_argument("report", help="path to an evals/reports/<run-id>.json")
    render.add_argument(
        "--baseline",
        help="re-derive the before/after section against this earlier run's JSON; without it, "
        "the section stored in the report being re-rendered is carried through unchanged",
    )
    render.set_defaults(func=cmd_render)

    smoke = sub.add_parser("smoke", help="probe candidate weak models (D11)")
    smoke.add_argument("--models", help="comma-separated model ids")
    smoke.add_argument("--limit", type=int, default=SMOKE_ITEM_COUNT)
    smoke.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    smoke.set_defaults(func=cmd_smoke)

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["run", *(argv or [])])
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
