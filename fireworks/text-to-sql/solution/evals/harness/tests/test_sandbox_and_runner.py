"""The sandbox's D9 guarantees, and the runner's resilience, offline.

The runner tests use :class:`~evals.harness.fake.FakeOracleClient` rather than a
network, so ``make check`` exercises the whole pipeline — generate → validate →
score → aggregate → render — without a key.
"""

from __future__ import annotations

import sqlite3

import pytest
from evals.harness import narrative
from evals.harness.corpus import load_corpus
from evals.harness.fake import FakeOracleClient
from evals.harness.metrics import percentile, summarize
from evals.harness.report import render_markdown
from evals.harness.runner import build_arms, run_arms
from evals.harness.sandbox import ExecutionError, fixtures

from t2s_core.models import Usage
from t2s_core.ports import InferenceResponse

EXPECTED_ARMS = 4
SMALL_LIMIT = 6


@pytest.fixture(scope="module")
def corpus():
    return load_corpus()


@pytest.fixture(scope="module")
def built(corpus):
    with fixtures(corpus) as fx:
        yield fx


# ---------------------------------------------------------------------------
# sandbox
# ---------------------------------------------------------------------------
def test_every_schema_is_seeded(built, corpus):
    for name in corpus.schemas:
        rows = built.execute(name, "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").rows
        assert rows[0][0] > 0


def test_a_write_is_refused_by_the_gate_not_by_the_engine(built):
    with pytest.raises(ExecutionError) as excinfo:
        built.execute("retail", "DELETE FROM customers")
    assert excinfo.value.kind == "safety_gate"


def test_the_connection_is_read_only_underneath_the_gate(built):
    # Belt and suspenders: even if the gate were bypassed, the connection cannot write.
    connection = sqlite3.connect(f"file:{built.paths['retail']}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM customers")
    finally:
        connection.close()


def test_a_row_cap_hit_is_an_error_not_a_truncated_comparison(built):
    with pytest.raises(ExecutionError) as excinfo:
        built.execute("retail", "SELECT 1 FROM customers, orders, order_items", row_cap=5)
    assert excinfo.value.kind == "truncated"


def test_a_binder_error_surfaces_as_an_engine_error(built):
    with pytest.raises(ExecutionError) as excinfo:
        built.execute("retail", "SELECT nope FROM customers")
    assert excinfo.value.kind == "engine"


# ---------------------------------------------------------------------------
# metrics helpers
# ---------------------------------------------------------------------------
def test_percentile_is_nearest_rank_and_survives_an_empty_list():
    assert percentile([], 0.5) == 0
    assert percentile([10], 0.95) == 10
    assert percentile([1, 2, 3, 4], 0.5) == 2
    assert percentile([1, 2, 3, 4], 0.95) == 4


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
def test_build_arms_produces_loop_off_and_loop_on_per_model():
    arms = build_arms({"primary": "m/a", "weak": "m/b"})
    assert len(arms) == EXPECTED_ARMS
    off = next(a for a in arms if a.name == "primary/loop_off")
    on = next(a for a in arms if a.name == "primary/loop_on")
    assert (off.max_repair_attempts, off.validator_name) == (0, "noop")
    assert (on.max_repair_attempts, on.validator_name) == (2, "ephemeral_sqlite")


def test_arms_can_be_filtered_by_name_or_by_role():
    assert [a.name for a in build_arms({"primary": "m"}, only=["primary/loop_on"])] == [
        "primary/loop_on"
    ]
    assert len(build_arms({"primary": "m", "weak": "n"}, only=["weak"])) == 2


def test_an_oracle_client_scores_a_perfect_gold_run(corpus, built):
    items = tuple(i for i in corpus.items if not i.is_adversarial)[:SMALL_LIMIT]
    client = FakeOracleClient(corpus)
    run = run_arms(
        corpus,
        build_arms({"primary": "fake/oracle"}, only=["primary/loop_off"]),
        items=items,
        client_for=lambda _m: client,
        fixtures=built,
        concurrency=2,
        run_id="unit",
        live=False,
    )
    metrics = summarize(run)
    assert metrics.arms[0].execution_accuracy.numerator == len(items)


def test_the_loop_rescues_what_the_off_arm_loses(corpus, built):
    """Guards the arm definitions themselves: with a client that is broken on
    turn 1 and correct on turn 2, loop_off must fail and loop_on must recover."""
    items = tuple(i for i in corpus.items if not i.is_adversarial)[:SMALL_LIMIT]
    client = FakeOracleClient(corpus, corrupt_pct=100)
    run = run_arms(
        corpus,
        build_arms({"primary": "fake/oracle"}),
        items=items,
        client_for=lambda _m: client,
        fixtures=built,
        concurrency=2,
        run_id="unit",
        live=False,
    )
    metrics = summarize(run)
    off = metrics.by_name("primary/loop_off")
    on = metrics.by_name("primary/loop_on")
    assert off is not None and on is not None
    assert off.execution_accuracy.numerator == 0
    assert on.execution_accuracy.numerator == len(items)
    assert on.repaired_items == len(items)
    assert on.repaired_and_correct == len(items)


def test_one_exploding_item_is_recorded_as_a_failure_and_does_not_abort_the_run(corpus, built):
    items = tuple(corpus.items)[:SMALL_LIMIT]
    bad_id = items[0].id

    class Exploding(FakeOracleClient):
        def complete(self, messages, **kwargs):
            if any(items[0].question in m.content for m in messages):
                raise RuntimeError("simulated upstream 500")
            return super().complete(messages, **kwargs)

    run = run_arms(
        corpus,
        build_arms({"primary": "fake/oracle"}, only=["primary/loop_off"]),
        items=items,
        client_for=lambda _m: Exploding(corpus),
        fixtures=built,
        concurrency=2,
        run_id="unit",
        live=False,
    )
    runs = run.arms[0].runs
    assert len(runs) == len(items), "no item may be dropped"
    failed = next(r for r in runs if r.item_id == bad_id)
    assert failed.harness_error is not None
    assert "simulated upstream 500" in failed.harness_error
    assert failed.score is not None
    assert failed.score["reason"] == "harness_error"
    assert summarize(run).arms[0].harness_errors == 1


def test_a_truncated_completion_is_not_silently_scored(corpus, built):
    """finish_reason='length' raises TruncatedResponse inside FireworksClient;
    here the equivalent is an unparseable body, which must surface, not pass."""

    class Garbage(FakeOracleClient):
        def complete(self, messages, **kwargs):
            self.calls += 1
            return InferenceResponse(
                content='{"response_class": "valid", "query": "SELECT',
                model="fake/oracle",
                finish_reason="stop",
                usage=Usage(),
            )

    items = tuple(i for i in corpus.items if not i.is_adversarial)[:2]
    run = run_arms(
        corpus,
        build_arms({"primary": "fake/oracle"}, only=["primary/loop_off"]),
        items=items,
        client_for=lambda _m: Garbage(corpus),
        fixtures=built,
        concurrency=1,
        run_id="unit",
        live=False,
    )
    for item in run.arms[0].runs:
        assert item.response_class == "error"
        assert item.error_code is not None
        assert item.error_code.startswith("repair_exhausted")
        assert not item.correct


def test_the_markdown_report_renders_and_names_the_fake_client(corpus, built):
    items = tuple(corpus.items)[:SMALL_LIMIT]
    client = FakeOracleClient(corpus)
    run = run_arms(
        corpus,
        build_arms({"primary": "fake/oracle", "weak": "fake/oracle"}),
        items=items,
        client_for=lambda _m: client,
        fixtures=built,
        concurrency=2,
        run_id="unit",
        live=False,
    )
    metrics = summarize(run)
    context = narrative.build_context(
        metrics,
        gold_items=sum(1 for i in items if not i.is_adversarial),
        adversarial_items=sum(1 for i in items if i.is_adversarial),
        weak_model_note=[],
        cost_note=[],
        repro=[],
        extra_caveats=[],
    )
    markdown = render_markdown(run, metrics, context=context)
    assert "OFFLINE / FAKE CLIENT" in markdown
    assert "Headline" in markdown
    assert "Per-item matrix" in markdown
    for item in items:
        assert item.id in markdown
