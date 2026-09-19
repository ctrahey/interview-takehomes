# `t2s_nl` — the natural-language orchestrator (layer 3)

MAIN.md's "Fully Natural-Language API": the layer that classifies a speech-act
and then drives the deterministic layers underneath it. The only package allowed
to depend on **both** `foundation` and `t2s_core`.

## The rule

> **The LLM classifies and generates. It NEVER reports system state.**

"Show me my databases" is one enum-constrained routing call that returns the word
`databases`, followed by a read from `foundation` rendered by our own code. A
model is never in a position to invent a database name, a row count, or a column
type — because it is never asked, and its output is never consulted for a fact.

That is enforced, not merely intended:

- `t2s_nl.inspection` — every state answer — imports no inference client at all,
  and `test_state_is_deterministic.py` parses its imports to prove it.
- The same suite routes a turn with a router response whose free text names a
  database that does not exist, and asserts the invented name never reaches the
  user.
- It also swaps in a client that raises on contact and shows the answer is
  unchanged.

## Intents

| intent | example | what runs |
|---|---|---|
| `help` | "what can you do?" | static text from our code |
| `inspect` | "show me my databases" | `foundation` read, rendered by us |
| `create_schema` | "model a bookstore" | `t2s_core.generate_schema` → entity graph → stored version |
| `query` | "which author sold the most?" | `t2s_core.generate_query`, gated + repaired |
| `load_data` | "load it with 20 rows" | rows generated, validated, loaded via `foundation` |
| `execute` | "run that" | `foundation.sample_db.query`, D9-gated |
| `corrective` | "actually, revenue is in cents" | persisted to the data model (D13) |
| `unknown` | anything unclear | a clarifying question — never a guess |

## Running it

```sh
uv run t2s-chat                 # live; key from FIREWORKS_API_KEY or ~/.fireworks-key
T2S_OFFLINE=1 uv run t2s-chat   # replays recorded fixtures, no network
uv run t2s-chat --ask "model a bookstore" --ask "/state"   # scripted, for transcripts
```

It **starts without a key**. The live client is built on first use, so the REPL
opens, every deterministic read works, and the credential is explained at the
moment something actually needs to generate — matching what `t2s_api` has always
done.

State lives in `~/.t2s/foundation.sqlite3` (`T2S_DB_URL` to override) and sample
databases in `~/.t2s/sample_dbs` (`T2S_SAMPLE_DB_DIR`). Close the chat, reopen
it, and the session resumes with its model, schema, database and correctives.

Slash commands — `/state /models /dbs /schemas /queries /schema /rows /sql /run
/fix /correctives /new /help /quit` — are a shortcut, not the interface: each has
a plain-English equivalent, and none of them makes an inference call, so the
workbench stays usable when the model does not.

## Fixtures

`src/t2s_nl/fixtures/inference/` holds exchanges captured from the live model by
`scripts/capture_fixtures.py`, keyed by a hash of the whole request. The offline
suite replays them, so the intent table and the end-to-end path are tested
against what `kimi-k2p7-code` actually said rather than what we hoped it would.

Change a prompt template or a scripted utterance and the key changes, the test
fails with `FixtureNotFound`, and the fix is to re-run the capture with a key.
That friction is deliberate: a prompt change is a behaviour change.

## Offline without a fixture: the keyword router

Fixture coverage of open-ended English is unbounded, so `T2S_OFFLINE=1` alone
only ever answered the utterances somebody captured. When the recorded client
has nothing — or there is no key at all — `src/t2s_nl/offline_router.py`
classifies the utterance with keyword and pattern matching over the *same enum*
the model fills in. That is tractable only because the output space is small and
fixed: eight intents, eleven inspect targets.

Three rules, and the third is the design:

1. **Fixtures win.** The recorded client is tried first; the keyword router is
   reached only on `FixtureNotFound` or a missing key — two conditions a live
   client with a key and a network cannot produce, which is what makes the
   fallback structurally unreachable online.
2. **It says so.** Every turn it routes carries
   `· offline: routed by keyword, not by the model` (or `no API key: …`), and
   the activity log records `routed_by` and tags the summary `(keyword)`.
3. **Deterministic routing, never deterministic generation.** `inspect`, `help`,
   `execute` and `load_data` end in a deterministic read or action, so routing
   them is the whole job. `query`, `create_schema` and `corrective` end in text
   a model must write — those are **refused**, with what would unblock them.
   No SQL or DDL is ever synthesised without a model.
