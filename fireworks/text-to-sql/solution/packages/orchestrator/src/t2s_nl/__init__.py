"""t2s_nl — the natural-language orchestrator (MAIN.md §"The Fully Natural-Language API").

Layer 3. The only package permitted to depend on **both** ``foundation`` and
``t2s_core``, and the reason that permission is worth granting: this is where
context and memory management turn a stateless generator into something a person
can hold a conversation with.

The rule the whole layering exists to enforce, restated here because it is easy
to erode:

    **The LLM classifies and generates. It NEVER reports system state.**

"Show me my databases" is an LLM deciding the word "databases" and a
deterministic read of ``foundation`` producing the answer. A model is never in a
position to invent a database name, a row count, or a column type.

Public surface::

    Orchestrator(client=..., store=...).run("...")    -> list[Turn]   (a plan)
    Orchestrator(client=..., store=...).handle("...") -> Turn         (the last one)
    route(utterance, client=...) -> Plan

When there is no model to ask at all -- offline replay with no matching
fixture, or no API key -- ``route`` falls back to ``t2s_nl.offline_router``,
which classifies over the same enum with keywords and marks the plan
``routed_by="keyword"`` so the surface can say so. It routes; it never
generates, and a request that needs generated text is refused with an
explanation rather than answered with an invention.

``t2s-chat`` is the reference front end over exactly that surface. D14's
activity log rides along on every turn: ``Orchestrator.activity`` is the
emitter, and any object with ``on_begin``/``on_end`` can subscribe to it --
which is how the chat draws a live line while a slow step runs.
"""

from t2s_nl.activity import ActivityEmitter, ActivityListener, ActivityRecord
from t2s_nl.clients import (
    ChainedRecordedClient,
    DeferredFireworksClient,
    MissingApiKey,
    is_offline,
    make_client,
    offline_client,
)
from t2s_nl.correctives import carry, compose_session_summary
from t2s_nl.intents import (
    INTENTS,
    MAX_PLAN_DIRECTIVES,
    PLAN_SCHEMA,
    Directive,
    Intent,
    Plan,
)
from t2s_nl.live import LiveActivityDisplay
from t2s_nl.offline_router import OFFLINE_ROUTING_NOTE
from t2s_nl.offline_router import classify as classify_offline
from t2s_nl.orchestrator import HELP_TEXT, Orchestrator
from t2s_nl.render import render_turn
from t2s_nl.router import RouterContext, route
from t2s_nl.store import Store
from t2s_nl.turns import DataTable, Turn

__all__ = [
    "HELP_TEXT",
    "OFFLINE_ROUTING_NOTE",
    "INTENTS",
    "MAX_PLAN_DIRECTIVES",
    "PLAN_SCHEMA",
    "ActivityEmitter",
    "ActivityListener",
    "ActivityRecord",
    "ChainedRecordedClient",
    "DataTable",
    "DeferredFireworksClient",
    "Directive",
    "Intent",
    "LiveActivityDisplay",
    "MissingApiKey",
    "Orchestrator",
    "Plan",
    "RouterContext",
    "Store",
    "Turn",
    "carry",
    "classify_offline",
    "compose_session_summary",
    "is_offline",
    "make_client",
    "offline_client",
    "render_turn",
    "route",
]
