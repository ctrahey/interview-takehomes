# Status

## Model selection (D10) — resolved 2026-09-18
Listed live via `GET https://api.fireworks.ai/inference/v1/models`. Smoke-tested four candidates
with a real `response_format: json_schema` call on a 2-table join/aggregate question.

| model | latency | JSON | SQL | note |
|---|---|---|---|---|
| `kimi-k2p7-code` | 3.3s | ok | correct | cleanest envelope discipline — **PRIMARY** |
| `deepseek-v4p1-flash` | 3.1s | ok | correct | clean — **comparison arm** |
| `glm-5p3-flash` | 4.0s | ok | correct | leaked chain-of-thought into `prose` — rejected |
| `qwen3p8-max` | 5.0s | ok | correct | returned `clarification_needed` for a valid query — rejected |

- `T2S_MODEL=accounts/fireworks/models/kimi-k2p7-code`
- `T2S_MODEL_ALT=accounts/fireworks/models/deepseek-v4p1-flash`
- All four honored the JSON schema, so D7 (enforced structured output) is confirmed viable on this
  platform. The two rejections were *semantic*, not format, failures — which is itself worth one
  line in the eval report: schema enforcement guarantees shape, never meaning.

Key lives at `~/.fireworks-key` (not in the repo, not in env files). Config reads
`FIREWORKS_API_KEY`; the Makefile reads the file as opaque data via `cat`. Never log it.

## Incident — key exposure during W0 (2026-09-18)
The W0 agent first wrote the Makefile to `source ~/.fireworks-key`. That file holds a bare token,
not shell syntax, so the shell attempted to execute the token and echoed part of it to stderr. The
agent detected this, rewrote the target to `export FIREWORKS_API_KEY="$(cat ~/.fireworks-key)"`,
and verified no key material appears in `eval`/`demo` output. Nothing reached git.
**Recommended to Chris: rotate the key**, since the token hit a terminal buffer and possibly shell
history. Build does not depend on which key the file holds.
Standing rule for all agents: credential files are opaque data. Read them, never source them.

## Local environment gotcha
`uv` on PATH is a company Azure-Artifacts wrapper requiring `az login`. Use the real binary:
`make check UV=$HOME/.local/bin/uv`. The Makefile is portable (`UV ?= uv`); this override is
local-sandbox-only and must not be baked into CI.

## Work unit status
| unit | state | agent model |
|---|---|---|
| W0 scaffold | **done** — `make check` green, import contract verified fail-then-pass | sonnet |
| W1 t2s_core | dispatched | opus |
| W2 corpus | **done** — 53 items, 29 tests pass; independently spot-verified | sonnet |
| W3 foundation | **done** — `make check` green (64 foundation tests incl. D6 round-trip + D9 security) | sonnet |
| W4 eval harness | blocked on W1+W2 | opus |
| W5 api | blocked on W1+W3 | sonnet |
| W6 bdd | blocked on W5 | sonnet |
| W7 cli | blocked on W1+W3 | sonnet |
| W8 evidence | last | opus |
| W10 orchestrator (layer 3) | **done** — `t2s_nl` + `t2s-chat`; 104 tests, 38 live-captured fixtures | opus |
| W13 activity log + directive plans (D14/D15) | **done** — `make check` green, 536 tests | opus |
| W14 containerization | **done** — Dockerfile + docker-compose.yml, `make check` green, 544 tests | sonnet |
| W16 offline router + keyless chat | **done** — `make check` green, 653 tests | opus |
| W15 conversational HTTP surface | **done** — chat + activity log + SSE, `make check` green, 572 tests | opus |

## W14 — containerization (2026-09-18)
`Dockerfile` (multi-stage, uv-based, python:3.12-slim-bookworm, non-root, 354MB final image),
`docker-compose.yml` (`api`, `chat`, `postgres` behind a `postgres` profile), `.dockerignore`,
`docker/README.md`. Verified live: builds for arm64 (host) and amd64 (buildx cross-build); container
starts and reports healthy with **no** `FIREWORKS_API_KEY` set (layer-1 CRUD confirmed over HTTP);
with the key read from `~/.fireworks-key` into a local, gitignored `.env`, one live layer-2 call
succeeded end-to-end through the container; `docker history`/env grep confirm no key material ever
enters the image. No files under `packages/foundation` or `packages/orchestrator` were touched.

**Postgres as the foundation metadata store (D5's "(a)") — works, config-only.** `foundation`'s ORM
models use only portable SQLAlchemy types (`Uuid`, `JSON`, `String`, `Text`, `DateTime`); the only
SQLite-specific code is two `if url.startswith("sqlite")` branches already in `foundation/db.py`
(connect_args, `PRAGMA foreign_keys`). The only gap was a driver — nothing in the workspace depended
on `psycopg`. Added via `uv pip install psycopg[binary]` in the Dockerfile, *after* the final
`uv sync`, not via any package's `pyproject.toml`/`uv.lock` (a `uv sync --frozen` run after an
earlier attempt silently uninstalled it again — order matters). Verified live against a
`postgres:16-alpine` sidecar: all 12 tables created (`init_db` = `Base.metadata.create_all`), a
project→session FK round-trip via the running API, rows confirmed with `psql` in the postgres
container.

**Postgres as an executable sample-database engine (D5's "(b)")** stays out of scope — documented at
length in `docker/README.md` (what it would take: an engine-backend port on `foundation/sample_db.py`,
D9's three-layer enforcement re-derived for Postgres semantics, per-database isolation without "just
a file", and a decision on what the eval corpus means once Postgres is executable).

**Discovered in W14, fixed in W16:** `t2s_api` defers building a live `FireworksClient` until a
layer-2 request needs one, so the API starts with no key. `t2s-chat` (orchestrator) did not — it
failed fast at startup with no key. `t2s_nl.clients.DeferredFireworksClient` now gives the chat the
same behaviour.



**foundation's safety gate blocks `EXPLAIN`.** Correct for the user-facing query path, but
`t2s_core`'s `EphemeralSqliteValidator` uses `EXPLAIN` for its dry-run. Therefore the dry-run MUST
stay inside t2s_core's own in-memory validator and MUST NOT route through
`foundation.sample_db.query()`. W4/W5/W7: do not "simplify" by reusing foundation's executor for
validation — the two gates have deliberately different jobs. (The import-linter contract already
makes the wrong direction impossible; this note covers the right-direction misuse.)

**Independent red-team of `foundation.security.assert_safe_select` (2026-09-18, by orchestrator):**
11 attacks, including several absent from the unit suite — CTE hiding `DELETE ... RETURNING`, write
in a nested subquery, comment-smuggled stacked statement, `SELECT ... UNION ...; DELETE`,
`INSERT ... SELECT`, `CREATE VIEW`, `EXPLAIN`. All blocked. Correctly allowed: mixed-case SELECT,
trailing semicolon, and `SELECT 'DROP TABLE t' AS note` (proves AST-based, not keyword denylist).


## W10 — layer 3 landed (2026-09-18), amending D2
D2 scoped the natural-language orchestrator out of phase 1. It is now in, as
`packages/orchestrator` (module `t2s_nl`) plus a `t2s-chat` console REPL. D2's
"degrades gracefully" rule still holds: nothing below layer 3 depends on it, and
removing the package leaves the previous submission intact.

New foundation tables (layer 3 needs durable conversation state; foundation owns
100% of persistence, so they live there and not in the chat client):
- `session_states` — the current data model / version / schema / database /
  last query for one session. This is what makes "load it with data" and "run
  that" survive a restart.
- `correctives` — D13 domain correctives, scoped to a `DataModel`. D13's phase-2
  list said "persistence per DataModel" was deferred; it is now done, because a
  conversation that forgets a corrective on exit is not a conversation.

Import-linter now carries three contracts, not one: `t2s_core ↛ everything`,
`foundation ↛ {t2s_nl, t2s_api, t2s_cli}`, and `t2s_nl ↛ {t2s_api, t2s_cli}`.
`t2s_nl` is the only package allowed to import both lower layers, per MAIN.md.

**Still open for W9:** `QueryRequest.correctives`. Until it exists, correctives
ride in `session_summary`, isolated in `t2s_nl.correctives.compose_session_summary`
with a comment naming W9. The swap is one line at one call site.


## W13 — the activity log and multi-directive plans (2026-09-19)

**D14, the activity log.** New `foundation.models.Activity` + `ActivityRepository`, append-only:
`append` plus readers, and deliberately no update or delete method — asserted structurally *and*
by capturing the SQL the ORM emits and failing on any `UPDATE`/`DELETE` naming `activities`.
Each step is two rows (`begin`, then `end`), each written in its **own** transaction, so a process
that dies mid-step leaves a `begin` with no `end` rather than a row stuck at "running". There is a
test that forks, kills the child inside a step with `os._exit`, and reads the evidence back.

`t2s_nl.activity.ActivityEmitter` emits; surfaces subscribe via `on_begin`/`on_end`. Listener
exceptions are logged and swallowed — observability is not allowed to become a new failure mode.
`t2s_nl.live.LiveActivityDisplay` is the `t2s-chat` subscriber: a daemon thread redraws
`⋯ generating sample data  3.4s` in place and resolves it to `✓ generating sample data  7.0s`.
It animates only on a tty. `/log` shows history with per-step durations.

Measured overhead on the common path: **+2.7 ms median per single-directive turn** (four rows:
`router.classify` and `query.generate`, begin+end), against inference calls of 1.5–8 s.

**D15, directive plans.** The router now returns a `Plan` — an ordered list of `Directive`s — in
**one** call, not one call per directive. The plan schema replaces the flat intent schema, so a
one-directive utterance costs exactly one request as before. Cap is 4; a longer plan is refused
whole (never truncated) and the refusal is recorded as `status="refused"`. A directive that does
not answer halts the plan; the completed prefix is returned and already rendered, because turns
are published to the surface as each one completes.

Referents (`last_query`, `last_result`, `last_schema`, `last_data`) resolve by reading the **last
activity of the matching kind** out of the D14 log. "Awesome — what's the SQL for that?" is answered
with the inference client swapped for one that raises on contact, which is how we know nothing in
that answer came from a model. This is why D14 and D15 were one unit of work.

**D16 re-verified on the plan-shaped schema.** Live capture: **24/24 router cases classify
correctly with `reasoning_effort="none"`**, including all three of Chris's compound utterances.
Reasoning stays off.

**Measured router latency, single directive, live, paired (old flat schema vs new plan schema,
back to back on the same utterance, n=42 pairs):** median **+143 ms**, mean **+30 ms**, new faster
in 16/42. Median completion tokens **85 → 120**, which is the whole of the difference: the array
wrapper and the per-directive `referent` field. Still one call; no regression in call count.

**Known rough edge.** The `inspect` target `activity` is reachable in plain English only when the
user names the log ("show me the activity log"). "What have you been doing?" and "show me what
you've done this session" both classify as `sessions`; "why did that take so long?" classifies as
`unknown`. `/log` is the reliable route and is what the help text points at.

Fixtures: re-captured (router prompt `router.system` v1 → v2 and a new response schema). 145
fixtures orphaned by the change were traced (by instrumenting `RecordedClient._load` over the whole
offline suite) and deleted; 46 remain, every one of them replayed by a test.

Also fixed: `tests/nl_doubles.TripwireClient.complete` accepted `reasoning_effort` and did not
forward it. Since the fixture key includes it when set, that wrapper silently orphaned every
fixture it touched.


## W15 — layer 3 over HTTP (2026-09-19)

`POST /sessions/{id}/chat`, `GET /sessions/{id}/activities`, and an SSE stream of
the same log at `/activities/stream`. Everything new is in `packages/api`; no file
under `packages/orchestrator` or `packages/foundation` was touched. `t2s_nl` is now
a declared dependency of `t2s_api` — the one direction MAIN.md's layering allows,
and the three import contracts still hold.

**The chat response is the executed plan, not an answer.** Built on
`Orchestrator.plan()` + `execute_plan()` (what `run()` does internally), never on
`handle()`, which returns only the last turn. `status` is `completed` | `halted` |
`refused`: a halt carries the completed prefix in `results` and the unrun
directives in `not_run`, on a 200. Each directive is paired with the turn it
produced and the `seq` of the activity rows it appended — attributed by a listener
that marks a boundary at each completed turn, which is exact because a plan
executes sequentially.

**SSE.** An `ActivityBroker` on `app.state` is an `ActivityListener`; the chat
endpoint passes it to the orchestrator. On the worker thread it does one
`loop.call_soon_threadsafe(put_nowait)` per subscriber — no lock across I/O, no
join, no await, and a full queue drops and counts rather than applying
backpressure to an inference call. The stream generator's `finally` unsubscribes,
so a dropped client leaks neither a listener nor a thread. Heartbeat comments every
15s (`T2S_SSE_HEARTBEAT_SECONDS`). Reconnect: every event carries `id: <seq>`, so
`Last-Event-ID` works for browsers and `?after_seq=` for everyone else; the backlog
is read *after* subscribing and live rows ≤ the last replayed seq are skipped, so
the join is gapless and duplicate-free.

**Found by driving it live:** activity timestamps serialised with a `Z` when
published live and without one when replayed out of SQLite (which has no timezone
type), so the same row read as UTC on the stream and as local time on the page.
`ActivityOut` now normalises to UTC and a test pins it.

**Testing note.** starlette's `TestClient` buffers a response to completion, so an
endless SSE stream hangs it. The six stream tests run against a real uvicorn on a
loopback port — still offline, still no key, and it is what makes the disconnect
and cross-origin assertions real. 28 new tests; 572 total.

**Drive-live evidence:** a bookstore modelled, a sample database loaded, then
"show me the query for the best selling authors and sample results" classified as
`[query, execute]` and both halves returned in one response, while `curl -N` on the
stream showed all 16 rows — the 10-row backlog, then the 6 live ones as they
happened.


## W16 — offline mode made usable: a deterministic keyword router (2026-09-19)

`T2S_OFFLINE=1` replayed fixtures and nothing else, so "what models do I have?" died with
`FixtureNotFound` and always would: fixture coverage of open-ended English is unbounded. New
`t2s_nl/offline_router.py` classifies an utterance with keyword and pattern matching over the
**same enum** the router's LLM call fills in — eight intents, eleven inspect targets. This is
MAIN.md rule of thumb #3 (deterministic tooling over flowing everything through context) applied
where it is actually tractable, which is exactly where the output space is small and fixed.

**Coverage.** All ten `inspect` targets (databases, models, schemas, schema_detail, sample_rows,
queries, sessions, correctives, state, activity), plus `help`, `execute`, `load_data`, and the
`last_query` referent ("what's the SQL for that?" → a D14 log read). Extracts table (session table
names beat grammar), row count, and seed. Splits on "and then"/"then"/"and also", and **backs out
of the split** when any half is unnameable — that is what "unambiguous" means operationally.
Measured against `scenarios.ROUTER_CASES`: **24/24 agreement** with what live `kimi-k2p7-code`
actually decided, counting a refusal as agreement on the 8 generation cases.

**The boundary, which is the point.** Routing is a choice over an enum; generation is not.
`query`, `create_schema` and `corrective` produce text a model must write, so the keyword router
**refuses them whole** (as a `Plan.refusal`, D15's existing "nothing was executed" shape) and says
what would unblock it. A property test asserts that no utterance in any repo corpus can make it
emit a generation directive. `load_data` is routable but its rows come from the model, so a second
backstop in `Orchestrator.execute` turns a generation call with no model behind it into the same
explanation. Nothing in either path synthesises SQL, DDL or rows.

**Fixtures win, and it is inert online.** The recorded client is tried first; the fallback fires on
exactly two exceptions — `FixtureNotFound` and the new `MissingApiKey` — both local, permanent and
impossible for a client that has a key and a network. A 429, a 503 or a read timeout degrades to
"ask the user" exactly as before. Proved three ways: all 24 captured utterances replay with the
fallback booby-trapped; five live failure modes with the same tripwire; and a structural assertion
that neither exception name appears in `FireworksClient`.

**It says what it did.** `Plan.routed_by` / `Plan.routing_note` (ours, deliberately *not* on the
wire, so a model cannot claim a provenance it does not have) become a visible note on every turn —
`· offline: routed by keyword, not by the model`, or `· no API key: ...`. `/log` records
`routed_by` in the activity detail and tags the summary `understood: inspect (keyword)`, so a
transcript shows which turns the model classified and which it did not.

**Keyless `t2s-chat`.** `DeferredFireworksClient` builds the live client on first `complete()`, so
the REPL opens with no key, the banner says what will and will not work, every deterministic read
works, plain English is keyword-routed, and the missing credential is explained at the point of
need. The W14 asymmetry with `t2s_api` is closed.

**CLI:** unchanged in substance — every `t2s` command is a *generation* command, so there is
nothing to route; only the unfixtured-offline message now states the boundary instead of quoting a
fixture hash.

Verified live with `FIREWORKS_API_KEY` unset and `~/.fireworks-key` absent: all six target
utterances answer from `foundation` on a fresh session; generation requests refuse; `t2s-chat`
starts keyless and `/state` `/models` `/dbs` `/log` all work. `make check UV=$HOME/.local/bin/uv`
green, **653 tests** (80 new in `packages/orchestrator/tests/test_offline_router.py`), 3 import
contracts kept.

**What offline still cannot do:** write SQL, design DDL, generate sample data, or record a
corrective in your own words — all four need a model, by design. The keyword router is also a
worse classifier than the model on wording it has no cue for; it answers `unknown` with a list of
what it does understand rather than guessing.
