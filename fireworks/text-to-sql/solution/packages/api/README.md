# `t2s_api` — the HTTP surface

Three layers, one app (design §5):

| layer | what it is | routes |
|---|---|---|
| 1 | deterministic CRUD, zero natural language | `/projects`, `/sessions`, `/data-models`, `/model-versions/*`, `/schemas/*`, `/datasets/*`, `/databases/*` |
| 2 | stateless text-to-SQL | `POST /text-to-sql/query`, `POST /text-to-sql/schema` |
| 3 | the conversation | `POST /sessions/{id}/chat`, `GET /sessions/{id}/activities`, `GET /sessions/{id}/activities/stream` |

Errors are RFC 9457 `application/problem+json`. A model that answers with a
question or a refusal is a **200** carrying that class — `response_class` at
layer 2, `turn.kind` at layer 3. Only transport and validation failures are
HTTP errors.

## Layer 3 in one paragraph

`POST /sessions/{id}/chat` takes `{"utterance": "..."}` and returns the
**executed plan**: the ordered directives the router derived (D15) and the turn
each one produced — SQL, tables, DDL, prose, repair attempts — plus the
`seq` of every activity row the run appended (D14).

```
status: "completed" | "halted" | "refused"
```

* `completed` — every directive ran. A turn may still be `clarification_needed`
  or `error`; that is the answer, not a failure.
* `halted` — a directive did not answer, so the rest were not run. `results`
  holds the completed prefix, `not_run` holds what did not happen, and
  `stopped_reason` says which directive stopped it. **A halt is never flattened
  into an HTTP error**: the client has to be able to render "this much worked".
* `refused` — the plan was over the cap (4 directives). *Nothing ran.*

## The live stream

```js
const es = new EventSource(`${API}/sessions/${id}/activities/stream`);
es.addEventListener("activity", e => render(JSON.parse(e.data)));
```

Each row of the append-only log arrives as it is appended, as an `activity`
event whose `id` is the row's `seq`. Lines beginning `:` are heartbeat comments
(every 15s, `T2S_SSE_HEARTBEAT_SECONDS`) that keep proxies from closing an idle
connection. An `event: lag` frame means this connection fell too far behind and
rows were dropped — re-read them from the REST endpoint.

**Reconnecting.** Every event carries `id: <seq>`, so a browser `EventSource`
resends `Last-Event-ID` automatically and is served everything it missed. A
non-browser client passes `?after_seq=` instead; the query parameter wins when
both are present. The backlog is read *after* the subscription is registered and
live rows at or below the last replayed `seq` are skipped, so the join is
gapless and duplicate-free. `GET /sessions/{id}/activities` returns
`next_after_seq`, which is the cursor to hand the stream.

**It cannot slow the conversation.** The orchestrator's emitter calls the broker
on the request's worker thread, and all the broker does there is a
non-blocking hand-off into each subscriber's asyncio queue. Nothing is joined,
nothing is awaited, and a subscriber that stops reading is dropped and told to
re-read rather than being buffered forever. Listener exceptions are swallowed
and logged by `ActivityEmitter` on top of that (D14). A disconnecting client
unsubscribes in the stream generator's `finally`, so nothing leaks.

The log is **read-only over HTTP**, deliberately: there is no write endpoint,
because a record of the past that a client can write is not a record.

## CORS

`T2S_CORS_ORIGINS` (comma-separated) overrides the default local dev origins
(Vite 5173, Next 3000). Not a wildcard — this API creates and queries
databases. The SSE stream is covered by the same middleware; a cross-origin
`EventSource` works out of the box and is asserted in `tests/test_chat_stream.py`,
because that is the usual place SSE breaks.

## Configuration

| variable | default | what it does |
|---|---|---|
| `T2S_API_DB_URL` | in-memory SQLite | the metadata store |
| `T2S_SAMPLE_DB_DIR` | `~/.t2s/sample_dbs` | where sample databases live |
| `T2S_CORS_ORIGINS` | local dev origins | browser origins allowed |
| `T2S_SSE_HEARTBEAT_SECONDS` | `15` | SSE keep-alive interval |
| `FIREWORKS_API_KEY` | — | required only when a layer-2/3 request needs inference |

The app starts with no key: `FireworksClient` is built lazily on the first
request that needs it, so layer 1 is fully usable without one.

## Running and testing

```sh
make serve UV=$HOME/.local/bin/uv        # uvicorn t2s_api.app:app --reload
uv run pytest packages/api/tests         # offline; no network, no key (D8)
```

The suite wires the app with a scripted or recorded `InferenceClient` via
`create_app(inference_client=...)` — DI, never a monkeypatch. The SSE tests run
against a real uvicorn on a loopback port, because `TestClient` buffers a
response to completion and an endless stream hangs it.
