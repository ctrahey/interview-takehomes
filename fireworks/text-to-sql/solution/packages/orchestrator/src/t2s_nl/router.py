"""One LLM call that turns an utterance into a plan of directives (D15).

**One call, not a chain, and not one call per directive.** The router's only job
is to cut the utterance into atomic directives, pick each one's branch and fill
in its slots; everything it picks is then executed by deterministic code. A
single-directive utterance -- the overwhelmingly common case -- costs exactly
what it cost before this file learned about plans: one request, one response,
``reasoning_effort="none"`` (D16). The plan is a shape change in the response
schema, not an extra round trip.

It is given a *checklist* of what exists in the session (does a current model
exist? is a database loaded?) so pronouns resolve -- but never the contents of
any of it, so there is nothing for it to parrot back as fact.

Degradation, per inference-findings: the enum is enforced on the wire (D7) and
validated again on arrival; a truncated, unparseable or mislabelled answer
becomes a one-directive ``unknown`` plan plus a clarifying question. Transport
failure (the API is down, the key is wrong) becomes the same, with the
exception's message -- never a traceback into the chat loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from t2s_core.errors import T2SError
from t2s_core.ports import InferenceClient, InferenceResponse, Message
from t2s_nl.intents import (
    MAX_PLAN_DIRECTIVES,
    PLAN_SCHEMA,
    PLAN_SCHEMA_NAME,
    Directive,
    Plan,
    plan_from_payload,
)
from t2s_nl.prompts import REGISTRY

__all__ = ["ROUTER_MAX_TOKENS", "RouterContext", "route"]

logger = logging.getLogger("t2s_nl.router")

#: A plan is a handful of short fields per directive, capped at four directives.
#: Generous because the failure mode is a dead turn, not a cost: with reasoning
#: disabled the router answers a one-directive utterance in ~130 tokens and a
#: two-directive one in ~230, so this ceiling is never approached in practice
#: and exists only to bound a pathological reply.
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
    on_response: Callable[[InferenceResponse], None] | None = None,
) -> Plan:
    """Classify one utterance into a plan. Never raises for an expected condition.

    ``on_response`` receives the raw completion so the caller can attribute the
    model, token count and request id to its ``router.classify`` activity (D14)
    without this module importing the activity log.
    """
    ctx = context or RouterContext()
    messages = [
        Message(
            "system",
            REGISTRY.render("router.system", max_directives=str(MAX_PLAN_DIRECTIVES)),
        ),
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
            response_schema=PLAN_SCHEMA,
            schema_name=PLAN_SCHEMA_NAME,
            max_tokens=ROUTER_MAX_TOKENS,
            # Measured: classifying a compound utterance cost ~1970 reasoning
            # tokens and truncated at both 600 and 1200, because reasoning
            # expands to fill the budget it is given. Disabling it answers the
            # same classification in ~130 tokens. This is a transcription task
            # over a fixed enum, not a problem that benefits from deliberation
            # (D16) -- re-verified after the plan schema landed.
            reasoning_effort="none",
            temperature=0.0,
        )
    except T2SError as exc:
        logger.warning("router inference failed: %s", type(exc).__name__)
        return Plan(
            directives=[Directive(intent="unknown", rationale="inference unavailable")],
            confidence="low",
            clarifying_question=(
                f"I could not reach the language model to work out what you meant ({exc}). "
                "Slash commands like /state, /models and /dbs still work -- they never "
                "call a model."
            ),
        )
    if on_response is not None:
        on_response(response)
    return plan_from_payload(response.content)
