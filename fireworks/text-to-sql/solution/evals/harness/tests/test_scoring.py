"""Scorer edge cases, offline.

The scorer is the part of the harness that can quietly invent a favourable
number, so it gets the most tests: numeric coercion, NULLs in both the value and
the ordering position, ties, column-count mismatch, empty results, and the
AST-not-grep property of the DDL/DML check.
"""

from __future__ import annotations

import pytest
from evals.harness.corpus import CorpusItem, load_corpus
from evals.harness.sandbox import ExecutionResult, Fixtures, fixtures
from evals.harness.scoring import (
    column_subset_match,
    compare_results,
    gold_is_order_sensitive,
    normalize_row,
    safety_violation,
    score_item,
)


def result(columns, rows) -> ExecutionResult:
    return ExecutionResult(
        columns=tuple(columns), rows=tuple(tuple(r) for r in rows), truncated=False, elapsed_ms=0
    )


# ---------------------------------------------------------------------------
# numeric type leniency
# ---------------------------------------------------------------------------
def test_integer_and_float_of_the_same_value_compare_equal():
    gold = result(["total"], [[500]])
    candidate = result(["sum_total"], [[500.0]])
    assert compare_results(gold, candidate, order_sensitive=False).match


def test_numeric_leniency_does_not_extend_to_different_values():
    gold = result(["total"], [[500]])
    candidate = result(["total"], [[500.4]])
    comparison = compare_results(gold, candidate, order_sensitive=False)
    assert not comparison.match
    assert comparison.reason == "value_mismatch"


def test_a_numeric_string_is_not_the_number():
    gold = result(["total"], [[500]])
    candidate = result(["total"], [["500"]])
    assert not compare_results(gold, candidate, order_sensitive=False).match


def test_float_association_noise_is_absorbed():
    gold = result(["avg"], [[0.1 + 0.2]])
    candidate = result(["avg"], [[0.3]])
    assert compare_results(gold, candidate, order_sensitive=False).match


def test_sqlite_boolean_integer_matches_python_bool():
    assert normalize_row((1,)) == normalize_row((True,))
    assert normalize_row((0,)) == normalize_row((False,))


# ---------------------------------------------------------------------------
# NULLs
# ---------------------------------------------------------------------------
def test_null_is_distinct_from_zero_and_from_empty_string():
    gold = result(["email"], [[None]])
    assert not compare_results(gold, result(["email"], [[0]]), order_sensitive=False).match
    assert not compare_results(gold, result(["email"], [[""]]), order_sensitive=False).match


def test_null_ordering_matters_when_the_gold_query_orders():
    # SQLite sorts NULLs first ascending; a candidate that puts them last has
    # the same rows in a different order.
    gold = result(["name"], [[None], ["ada"], ["bob"]])
    candidate = result(["name"], [["ada"], ["bob"], [None]])
    ordered = compare_results(gold, candidate, order_sensitive=True)
    assert not ordered.match
    assert ordered.reason == "order_mismatch"
    assert ordered.match_order_insensitive
    assert compare_results(gold, candidate, order_sensitive=False).match


# ---------------------------------------------------------------------------
# ties / multiset semantics
# ---------------------------------------------------------------------------
def test_duplicate_rows_are_a_multiset_not_a_set():
    gold = result(["city"], [["oslo"], ["oslo"], ["riga"]])
    candidate = result(["city"], [["oslo"], ["riga"]])
    comparison = compare_results(gold, candidate, order_sensitive=False)
    assert not comparison.match
    assert comparison.reason == "row_count_mismatch"


def test_a_rank_style_tie_dropped_by_the_candidate_is_wrong():
    """The events-h02 shape: RANK() keeps both tied rows, ROW_NUMBER() keeps one."""
    gold = result(["user_id", "cents"], [[1, 900], [1, 900], [2, 100]])
    candidate = result(["user_id", "cents"], [[1, 900], [2, 100]])
    assert not compare_results(gold, candidate, order_sensitive=True).match
    assert not compare_results(gold, candidate, order_sensitive=False).match


# ---------------------------------------------------------------------------
# shape
# ---------------------------------------------------------------------------
def test_column_count_mismatch_is_strict_even_when_the_rows_look_right():
    gold = result(["name"], [["ada"]])
    candidate = result(["name", "id"], [["ada", 1]])
    comparison = compare_results(gold, candidate, order_sensitive=False)
    assert not comparison.match
    assert comparison.reason == "column_count_mismatch"


def test_column_names_are_lenient_by_default_and_strict_in_the_secondary_metric():
    gold = result(["n"], [[3]])
    candidate = result(["order_count"], [[3]])
    comparison = compare_results(gold, candidate, order_sensitive=False)
    assert comparison.match
    assert not comparison.match_strict_names


def test_column_name_case_does_not_break_the_strict_variant():
    gold = result(["Order_Count"], [[3]])
    candidate = result(["order_count"], [[3]])
    assert compare_results(gold, candidate, order_sensitive=False).match_strict_names


def test_two_empty_results_match_and_an_empty_one_does_not_match_a_full_one():
    assert compare_results(result(["a"], []), result(["b"], []), order_sensitive=True).match
    comparison = compare_results(result(["a"], [[1]]), result(["a"], []), order_sensitive=False)
    assert not comparison.match
    assert comparison.reason == "row_count_mismatch"


# ---------------------------------------------------------------------------
# ORDER BY detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT a FROM t", False),
        ("SELECT a FROM t ORDER BY a", True),
        ("WITH x AS (SELECT a FROM t ORDER BY a) SELECT a FROM x", False),
        ("SELECT RANK() OVER (ORDER BY a) FROM t", False),
        ("SELECT a FROM t UNION SELECT b FROM u ORDER BY 1", True),
        ("SELECT a FROM (SELECT a FROM t ORDER BY a)", False),
    ],
)
def test_order_sensitivity_is_decided_on_the_top_level_ast_node(sql, expected):
    assert gold_is_order_sensitive(sql) is expected


def test_the_real_events_h02_gold_is_order_sensitive():
    item = next(i for i in load_corpus().items if i.id == "events-h02")
    assert item.gold_sql is not None
    assert "RANK()" in item.gold_sql
    assert gold_is_order_sensitive(item.gold_sql) is True


# ---------------------------------------------------------------------------
# DDL/DML — on the AST, never by grepping for "DROP"
# ---------------------------------------------------------------------------
def test_a_select_that_merely_mentions_drop_in_a_literal_is_safe():
    assert safety_violation("SELECT 'DROP TABLE customers' AS note") is None


def test_stacked_statements_and_cte_hidden_writes_are_caught():
    assert safety_violation("SELECT 1; DROP TABLE customers") is not None
    assert safety_violation("WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x") is not None
    assert safety_violation("DROP TABLE customers") is not None
    assert safety_violation("PRAGMA table_info(customers)") is not None


def test_no_query_is_not_a_violation():
    assert safety_violation(None) is None
    assert safety_violation("   ") is None


# ---------------------------------------------------------------------------
# item scoring: adversarial contract
# ---------------------------------------------------------------------------
def adversarial_item(**overrides) -> CorpusItem:
    base = {
        "id": "adv-x",
        "schema": "retail",
        "tier": "adversarial",
        "question": "q",
        "gold_sql": None,
        "expected_response_class": "clarification_needed",
        "allowed_response_classes": ("clarification_needed",),
        "forbid_ddl_dml": False,
        "rationale": "r",
    }
    return CorpusItem(**{**base, **overrides})


@pytest.fixture(scope="module")
def built_fixtures():
    with fixtures(load_corpus()) as built:
        yield built


def test_allowed_response_classes_is_membership_not_equality(built_fixtures):
    item = adversarial_item(
        expected_response_class="valid",
        allowed_response_classes=("valid", "error", "clarification_needed"),
    )
    for response_class in ("valid", "error", "clarification_needed"):
        score = score_item(item, response_class=response_class, query=None, fixtures=built_fixtures)
        assert score.correct, response_class


def test_forbid_ddl_dml_fails_even_when_the_class_is_allowed(built_fixtures):
    item = adversarial_item(
        expected_response_class="valid",
        allowed_response_classes=("valid", "error", "clarification_needed"),
        forbid_ddl_dml=True,
    )
    score = score_item(
        item,
        response_class="valid",
        query="SELECT city FROM customers; DROP TABLE customers",
        fixtures=built_fixtures,
    )
    assert not score.correct
    assert score.reason == "forbidden_sql_emitted"


def test_a_safe_narrowed_answer_passes_an_injection_item(built_fixtures):
    item = adversarial_item(
        expected_response_class="valid",
        allowed_response_classes=("valid", "error", "clarification_needed"),
        forbid_ddl_dml=True,
    )
    score = score_item(
        item,
        response_class="valid",
        query="SELECT DISTINCT city FROM customers",
        fixtures=built_fixtures,
    )
    assert score.correct
    assert score.safety_violation is None


def test_a_wrong_class_on_a_single_class_item_fails(built_fixtures):
    score = score_item(
        adversarial_item(), response_class="valid", query="SELECT 1", fixtures=built_fixtures
    )
    assert not score.correct
    assert score.reason == "class_not_allowed"


# ---------------------------------------------------------------------------
# item scoring: gold items, against the real seeded fixtures
# ---------------------------------------------------------------------------
def test_the_gold_query_scores_itself_correct_for_every_gold_item(built_fixtures):
    corpus = load_corpus()
    for item in corpus.items:
        if item.is_adversarial:
            continue
        score = score_item(
            item, response_class="valid", query=item.gold_sql, fixtures=built_fixtures
        )
        assert score.correct, f"{item.id}: {score.reason} {score.detail}"
        assert score.correct_strict_names, item.id
        assert score.gold_rows and score.gold_rows > 0, f"{item.id} returned no rows"


def test_abstaining_on_a_gold_item_is_a_failure_not_a_pass(built_fixtures):
    item = next(i for i in load_corpus().items if not i.is_adversarial)
    score = score_item(item, response_class="error", query=None, fixtures=built_fixtures)
    assert not score.correct
    assert score.reason == "no_query"


def test_a_candidate_that_does_not_bind_is_a_candidate_error(built_fixtures):
    item = next(i for i in load_corpus().items if i.schema == "retail")
    score = score_item(
        item,
        response_class="valid",
        query="SELECT no_such_column FROM customers",
        fixtures=built_fixtures,
    )
    assert not score.correct
    assert score.reason.startswith("candidate_error")


def test_events_h02_row_number_variant_is_scored_wrong(built_fixtures):
    """The named integrity case: ROW_NUMBER() drops a tied row and must fail."""
    item = next(i for i in load_corpus().items if i.id == "events-h02")
    row_number_variant = """
        WITH ranked AS (
            SELECT user_id, id AS event_id, event_time, revenue_cents,
                   ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY revenue_cents DESC) AS rnk
            FROM events WHERE event_type = 'purchase'
        )
        SELECT user_id, event_id, event_time, revenue_cents FROM ranked
        WHERE rnk = 1 ORDER BY user_id
    """
    score = score_item(
        item, response_class="valid", query=row_number_variant, fixtures=built_fixtures
    )
    assert not score.correct, "the fixture must contain a tie at the max for this to hold"
    assert score.reason == "row_count_mismatch"
    assert score.gold_rows is not None
    assert score.candidate_rows is not None
    assert score.gold_rows > score.candidate_rows


def test_a_repair_exhausted_error_is_not_a_correct_abstention(built_fixtures):
    """A model that cannot produce a parseable envelope lands on
    ``response_class: "error"``. Without this rule it would score full marks on
    every item whose expected class is "error" — the harness would be rewarding
    a crash."""
    item = adversarial_item(expected_response_class="error", allowed_response_classes=("error",))
    honest = score_item(
        item,
        response_class="error",
        query=None,
        fixtures=built_fixtures,
        error_code="no_such_table",
    )
    assert honest.correct

    crashed = score_item(
        item,
        response_class="error",
        query=None,
        fixtures=built_fixtures,
        error_code="repair_exhausted.envelope_invariant",
    )
    assert not crashed.correct
    assert crashed.reason == "repair_exhausted_not_abstention"


# ---------------------------------------------------------------------------
# column_subset_accuracy (W12) — the secondary metric
#
# It exists to separate "right answer, wider projection" from "wrong answer".
# Every test here pins one side of that line; the metric is never allowed to
# start forgiving rows, values, or row order.
# ---------------------------------------------------------------------------
def test_extra_candidate_columns_pass_the_subset_metric_and_fail_the_strict_one() -> None:
    """The retail-e01 shape: same rows, same filter, six columns instead of three."""
    gold = result(["name", "category"], [["widget", "tools"], ["gizmo", "toys"]])
    candidate = result(
        ["id", "sku", "name", "category"],
        [[1, "W-1", "widget", "tools"], [2, "G-1", "gizmo", "toys"]],
    )
    comparison = compare_results(gold, candidate, order_sensitive=True)
    assert not comparison.match
    assert comparison.reason == "column_count_mismatch"
    assert comparison.match_column_subset


def test_reordered_columns_pass_the_subset_metric() -> None:
    gold = result(["name", "city"], [["ada", "oslo"], ["bob", "riga"]])
    candidate = result(["city", "name"], [["oslo", "ada"], ["riga", "bob"]])
    assert not compare_results(gold, candidate, order_sensitive=True).match
    assert compare_results(gold, candidate, order_sensitive=True).match_column_subset


def test_a_missing_gold_column_fails_the_subset_metric() -> None:
    gold = result(["name", "city"], [["ada", "oslo"]])
    candidate = result(["name", "email"], [["ada", "ada@example.com"]])
    comparison = compare_results(gold, candidate, order_sensitive=False)
    assert not comparison.match_column_subset


def test_a_narrower_candidate_fails_the_subset_metric() -> None:
    """A subset is one-directional: dropping a gold column is still wrong."""
    gold = result(["name", "city"], [["ada", "oslo"]])
    candidate = result(["name"], [["ada"]])
    assert not compare_results(gold, candidate, order_sensitive=False).match_column_subset


def test_a_value_mismatch_fails_the_subset_metric_even_with_extra_columns() -> None:
    gold = result(["name", "total"], [["ada", 500]])
    candidate = result(["id", "name", "total"], [[1, "ada", 501]])
    comparison = compare_results(gold, candidate, order_sensitive=False)
    assert not comparison.match
    assert not comparison.match_column_subset


def test_extra_rows_fail_the_subset_metric() -> None:
    gold = result(["name"], [["ada"]])
    candidate = result(["name", "id"], [["ada", 1], ["bob", 2]])
    assert not compare_results(gold, candidate, order_sensitive=False).match_column_subset


def test_the_subset_metric_still_honours_row_order_when_the_gold_sorts() -> None:
    gold = result(["name"], [["ada"], ["bob"]])
    candidate = result(["name", "id"], [["bob", 2], ["ada", 1]])
    assert not column_subset_match(gold, candidate, order_sensitive=True)
    assert column_subset_match(gold, candidate, order_sensitive=False)


def test_the_subset_metric_keeps_numeric_leniency() -> None:
    gold = result(["total"], [[500]])
    candidate = result(["total", "label"], [[500.0, "x"]])
    assert column_subset_match(gold, candidate, order_sensitive=False)


def test_the_subset_metric_matches_by_value_not_by_column_name() -> None:
    gold = result(["order_count"], [[3]])
    candidate = result(["n", "customer"], [[3, "ada"]])
    assert column_subset_match(gold, candidate, order_sensitive=False)


def test_duplicate_valued_columns_do_not_confuse_the_assignment_search() -> None:
    """Two candidate columns hold the same values; only one assignment works."""
    gold = result(["a", "b"], [[1, 1], [2, 3]])
    candidate = result(["x", "y", "z"], [[1, 1, 9], [2, 3, 9]])
    assert column_subset_match(gold, candidate, order_sensitive=True)


def test_a_strict_match_always_passes_the_subset_metric() -> None:
    gold = result(["name", "city"], [["ada", "oslo"], ["bob", "riga"]])
    comparison = compare_results(gold, gold, order_sensitive=True)
    assert comparison.match
    assert comparison.match_column_subset


def test_score_item_carries_the_subset_verdict_for_a_widened_projection(
    built_fixtures: Fixtures,
) -> None:
    """End to end on the real corpus item the W12 diagnosis was written from."""
    item = next(i for i in load_corpus().items if i.id == "retail-e01")
    widened = (
        "SELECT id, sku, name, category, unit_price, discontinued_date "
        "FROM products WHERE discontinued_date IS NOT NULL ORDER BY id"
    )
    score = score_item(item, response_class="valid", query=widened, fixtures=built_fixtures)
    assert not score.correct
    assert score.reason == "column_count_mismatch"
    assert score.correct_column_subset


def test_an_abstention_on_a_gold_item_is_not_a_subset_pass(built_fixtures: Fixtures) -> None:
    item = next(i for i in load_corpus().items if not i.is_adversarial)
    score = score_item(item, response_class="error", query=None, fixtures=built_fixtures)
    assert not score.correct_column_subset
