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

    metrics = summarize(run)
    old_context = payload.get("context", {})
    context = narrative.build_context(
        metrics,
        gold_items=sum(1 for i in items if not i.is_adversarial),
        adversarial_items=sum(1 for i in items if i.is_adversarial),
        weak_model_note=_weak_model_note(),
        cost_note=old_context.get("cost_note", []),
        repro=old_context.get("repro", []),
        extra_caveats=[],
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
    context = narrative.build_context(
        metrics,
        gold_items=len(gold),
        adversarial_items=len(adversarial),
        weak_model_note=_weak_model_note(),
        cost_note=_cost_note(counter.truncations, metrics),
        repro=_repro(args, models, run_id),
        extra_caveats=_selection_note(corpus, items),
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
    run.add_argument("--fake", action="store_true", help="offline oracle client; not evidence")
    run.add_argument("--fake-corrupt", type=int, default=0)
    run.set_defaults(func=cmd_run)

    render = sub.add_parser(
        "render", help="re-score and re-render a saved run JSON, offline, no API calls"
    )
    render.add_argument("report", help="path to an evals/reports/<run-id>.json")
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
