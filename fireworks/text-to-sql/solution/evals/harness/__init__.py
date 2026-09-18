"""The eval harness (design §7, D3/D4/D11) — the deliverable that gets graded.

Four modules, in dependency order:

* :mod:`evals.harness.corpus`  — loads ``evals/corpus`` (manifest + DDL + seed).
* :mod:`evals.harness.sandbox` — builds the seeded SQLite fixtures and executes
  gold and candidate SQL under the D9 guarantees (AST gate, read-only
  connection, wall-clock timeout, row cap).
* :mod:`evals.harness.scoring` — the scorer contract from design.md's addendum.
* :mod:`evals.harness.runner`  — runs the corpus across the D11 arms.
* :mod:`evals.harness.metrics` — aggregates a run into the reported numbers.
* :mod:`evals.harness.report`  — writes the JSON and markdown evidence.

Everything except :mod:`~evals.harness.runner`'s live client is offline and unit
tested; ``make test`` exercises the scorer's edge cases without a network.
"""

from evals.harness.corpus import Corpus, CorpusItem, load_corpus
from evals.harness.runner import ARM_SPECS, Arm, ItemRun, RunResult, run_arms
from evals.harness.scoring import ItemScore, score_item

__all__ = [
    "ARM_SPECS",
    "Arm",
    "Corpus",
    "CorpusItem",
    "ItemRun",
    "ItemScore",
    "RunResult",
    "load_corpus",
    "run_arms",
    "score_item",
]
