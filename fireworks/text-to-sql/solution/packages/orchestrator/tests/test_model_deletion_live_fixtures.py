"""W18: Chris's three utterances, replayed against what the live model said.

``test_model_deletion.py`` scripts the router and proves the orchestration. This
one proves the *classification*: the model really does read "can you delete the
sports model?" as a ``destroy`` whose scope is open, and "remove the sports
league data model" as a ``destroy`` whose scope is the model. Those two were the
whole of the defect -- the first used to produce a question the system could not
honour and the second used to produce "I don't have a way to delete data
models".

"both" never reaches the model at all: it is read by ``t2s_nl.confirmation``.
That is asserted here too, because a scenario replay is the only place the whole
thing runs together and it is where an accidental extra call would show up.

Nothing is destroyed by this replay. The script never says yes, so every
destructive turn stops at a description -- which is itself the property worth
replaying end to end.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foundation.models import DataModel
from foundation.repositories import PendingActionRepository
from t2s_nl.clients import offline_client
from t2s_nl.orchestrator import Orchestrator
from t2s_nl.scenarios import LIFECYCLE_SCRIPT
from t2s_nl.store import Store
from t2s_nl.turns import Turn


@pytest.fixture
def replayed(store: Store, sample_db_dir: Path) -> tuple[Orchestrator, list[Turn]]:
    orch = Orchestrator(client=offline_client(), store=store)
    return orch, [orch.handle(utterance) for utterance in LIFECYCLE_SCRIPT]


def test_nothing_in_the_replay_is_an_error(replayed: tuple[Orchestrator, list[Turn]]) -> None:
    _, turns = replayed
    assert [(t.kind, t.text) for t in turns if t.kind == "error"] == []


def test_the_model_is_recognised_as_the_subject(
    replayed: tuple[Orchestrator, list[Turn]],
) -> None:
    """Turn 2: "can you delete the sports model?"

    Measured, not hoped for. The live router reads "the sports **model**" as
    naming the design and answers ``delete_scope="model"`` outright, so the
    scope question is not asked -- and that is the better turn: the ambiguity
    Chris hit was the system's, not the sentence's. The scope question is still
    reached whenever the wording genuinely leaves it open, which is covered with
    a scripted router in ``test_model_deletion.py``.

    What the old system did with this utterance was ask a question and then fail
    to honour either answer. What matters here is the second half of that: the
    description is a real blast radius and the next turn can act on it.
    """
    _, turns = replayed
    asked = turns[1]
    assert asked.intent == "destroy"
    assert asked.kind == "clarification_needed"
    assert "permanently delete the data model" in asked.text
    assert asked.table is not None
    assert {row[0] for row in asked.table.rows} >= {"data model", "versions", "schema"}


def test_both_is_answered_without_a_model(replayed: tuple[Orchestrator, list[Turn]]) -> None:
    """Turn 3: "both" -- read in code, and it produces a blast radius, not a deletion.

    The old system answered this with "what two things would you like me to do
    or show?". It is now the widest of the three answers, handled by
    ``t2s_nl.confirmation`` with no model involved, and it leaves a pending
    *model* deletion -- which still has to be confirmed.
    """
    orch, turns = replayed
    described = turns[2]
    assert described.kind == "clarification_needed"
    assert "permanently delete the data model" in described.text
    assert "cannot be undone" in described.text
    # Not read as "you moved on": nothing was cancelled, it was widened.
    assert not any("nothing was deleted" in note for note in described.notes)
    with orch.store.scope() as db:
        pending = PendingActionRepository(db).get(orch.store.session_id)
        assert pending is not None
        assert pending.action == "destroy_model"


def test_removing_the_data_model_is_classified_as_such(
    replayed: tuple[Orchestrator, list[Turn]],
) -> None:
    """Turn 4: the utterance that used to be met with "I don't have a way to do that"."""
    _, turns = replayed
    assert turns[3].intent == "destroy"
    assert turns[3].kind == "clarification_needed"
    assert "data model" in turns[3].text


def test_the_replay_destroys_nothing(replayed: tuple[Orchestrator, list[Turn]]) -> None:
    """Three destructive turns, no confirmation, nothing gone."""
    orch, _ = replayed
    with orch.store.scope() as db:
        models = db.query(DataModel).all()
    assert len(models) == 1
    assert models[0].name
