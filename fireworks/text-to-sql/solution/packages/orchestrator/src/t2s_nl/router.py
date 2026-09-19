"""One LLM call that classifies the utterance and extracts parameters.

One call, not a chain: the router's only job is to pick a branch and fill in
slots, and everything it picks is then executed by deterministic code. It is
given a *checklist* of what exists in the session (does a current model exist?
is a database loaded?) so pronouns resolve -- but never the contents of any of
it, so there is nothing for it to parrot back as fact.

Degradation, per inference-findings: the enum is enforced on the wire (D7) and
validated again on arrival; a truncated, unparseable or mislabelled answer
becomes ``unknown`` plus a clarifying question. Transport failure (the API is
down, the key is wrong) becomes ``unknown`` too, with the exception's message --
never a traceback into the chat loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from t2s_core.errors import T2SError
from t2s_core.ports import InferenceClient, Message
from t2s_nl.intents import ROUTER_SCHEMA, ROUTER_SCHEMA_NAME, IntentDecision, decision_from_payload
from t2s_nl.prompts import REGISTRY

__all__ = ["ROUTER_MAX_TOKENS", "RouterContext", "route"]

logger = logging.getLogger("t2s_nl.router")

#: The decision is a handful of short fields. Generous enough that finding #1's
#: truncation trap is not reachable here in practice, small enough to be cheap.
# Generous because the failure mode is a dead turn, not a cost: with
# reasoning disabled the router answers in ~110 tokens, so this ceiling is
# never approached in practice and exists only to bound a pathological reply.
ROUTER_MAX_TOKENS = 1500


@dataclass(frozen=True, slots=True)
class RouterContext:
    """What exists right now -- presence, never contents.

    ``table_names`` is the one borderline case and it is included deliberately:
    "show me some sample rows from orders" cannot be routed to a table without
    knowing that ``orders`` is a table. Table names are the user's own schema
    identifiers, which the model already sees in full on the query path. No row
    data, no ids, no counts of records appear here.
    """

    has_data_model: bool = False
    data_model_name: str | None = None
    table_names: tuple[str, ...] = ()
    has_schema: bool = False
    has_database: bool = False
    database_loaded: bool = False
    has_last_query: bool = False
    last_question: str | None = None
    corrective_count: int = 0

    def as_lines(self) -> str:
        lines = [
            f"- a current data model exists: {'yes' if self.has_data_model else 'no'}"
            + (f" (named {self.data_model_name!r})" if self.data_model_name else ""),
            f"- its tables: {', '.join(self.table_names) if self.table_names else '(none)'}",
            f"- a sample database exists: {'yes' if self.has_database else 'no'}",
            f"- that database has data loaded: {'yes' if self.database_loaded else 'no'}",
            f"- a previous query is available to re-run: {'yes' if self.has_last_query else 'no'}",
            f"- the last question asked was: {self.last_question or '(none)'}",
            f"- correctives recorded on this model: {self.corrective_count}",
        ]
        return "\n".join(lines)


def route(
    utterance: str,
    *,
    client: InferenceClient,
    context: RouterContext | None = None,
) -> IntentDecision:
    """Classify one utterance. Never raises for an expected condition."""
    ctx = context or RouterContext()
    messages = [
        Message("system", REGISTRY.render("router.system")),
        Message(
            "user",
            REGISTRY.render(
                "router.user",
                state_context=REGISTRY.render("router.state", state_lines=ctx.as_lines()),
                utterance=utterance,
            ),
        ),
    ]
    try:
        response = client.complete(
            messages,
            response_schema=ROUTER_SCHEMA,
            schema_name=ROUTER_SCHEMA_NAME,
            max_tokens=ROUTER_MAX_TOKENS,
            # Measured: classifying a compound utterance cost ~1970 reasoning
            # tokens and truncated at both 600 and 1200, because reasoning
            # expands to fill the budget it is given. Disabling it answers the
            # same classification in 111 tokens. This is a routing decision over
            # a fixed enum, not a problem that benefits from deliberation.
            reasoning_effort="none",
            temperature=0.0,
        )
    except T2SError as exc:
        logger.warning("router inference failed: %s", type(exc).__name__)
        return IntentDecision(
            intent="unknown",
            confidence="low",
            clarifying_question=(
                f"I could not reach the language model to work out what you meant ({exc}). "
                "Slash commands like /state, /models and /dbs still work -- they never "
                "call a model."
            ),
            rationale="inference unavailable",
        )
    return decision_from_payload(response.content)
