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

    Orchestrator(client=..., store=...).handle("...") -> Turn
    route(utterance, client=...) -> IntentDecision

``t2s-chat`` is the reference front end over exactly that surface.
"""

from t2s_nl.clients import ChainedRecordedClient, is_offline, make_client, offline_client
from t2s_nl.correctives import carry, compose_session_summary
from t2s_nl.intents import INTENTS, ROUTER_SCHEMA, Intent, IntentDecision
from t2s_nl.orchestrator import HELP_TEXT, Orchestrator
from t2s_nl.render import render_turn
from t2s_nl.router import RouterContext, route
from t2s_nl.store import Store
from t2s_nl.turns import DataTable, Turn

__all__ = [
    "HELP_TEXT",
    "INTENTS",
    "ROUTER_SCHEMA",
    "ChainedRecordedClient",
    "DataTable",
    "Intent",
    "IntentDecision",
    "Orchestrator",
    "RouterContext",
    "Store",
    "Turn",
    "carry",
    "compose_session_summary",
    "is_offline",
    "make_client",
    "offline_client",
    "render_turn",
    "route",
]
