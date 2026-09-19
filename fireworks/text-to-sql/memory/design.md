# Solution Design — Phase 1

Derived from `prompts/MAIN.md` (human-owned) and constrained by `memory/decisions.md`.
Source of truth for agent work. Build under `solution/`.

## 1. Shape

```
                 ┌───────────┐     ┌───────────┐
   HTTP ────────▶│    api    │     │    cli    │────┐
                 └─────┬─────┘     └─────┬─────┘    │
                       │                 │          │
                 ┌─────▼─────────────────▼─────┐    │
                 │        foundation           │    │   (both surfaces call
                 │  persistence · sample DBs   │    │    both packages; no
                 └─────────────┬───────────────┘    │    layer is skipped)
                               │ injects validator   │
                 ┌─────────────▼───────────────┐    │
                 │         t2s_core            │◀───┘
                 │  stateless · LLM inference  │
                 └─────────────┬───────────────┘
                               │ ports
                  ┌────────────┴────────────┐
           InferenceClient            QueryValidator
        (Fireworks | Recorded)   (EphemeralSqlite | Sqlglot | NoOp)
```

Hard rule: `t2s_core` imports nothing from `foundation`. Enforced by an import-linter contract in
CI, not by good intentions.

## 2. Repo layout

```
solution/
  pyproject.toml              # uv workspace, ruff, mypy, pytest, import-linter
  Makefile                    # setup / test / lint / eval / serve / demo
  packages/
    t2s_core/src/t2s_core/    # stateless generation + validation + repair
    foundation/src/foundation/# SQLAlchemy models, repositories, sample DB lifecycle
    api/src/t2s_api/          # FastAPI app
    cli/src/t2s_cli/          # click app
  evals/
    corpus/                   # schemas, fixtures, question/gold pairs
    harness/                  # runner, scorer, reporters
    reports/                  # committed evidence — JSON + markdown
  tests/
    bdd/                      # behavioral HTTP suite
  README.md                   # the submission's front door
```

## 3. `t2s_core` — stateless

Public surface (keep it this small):

```python
def generate_query(req: QueryRequest, *, client: InferenceClient,
                   validator: QueryValidator | None = None) -> QueryResult
def generate_schema(req: SchemaRequest, *, client: InferenceClient,
                    validator: QueryValidator | None = None) -> SchemaResult
```

`QueryRequest`: `question`, `schema_ddl`, `dialect`, `session_summary: str | None`,
`max_repair_attempts: int = 2`.

Response envelope — one shape for every outcome, matching `prompts/MAIN.md`:

```python
response_class: Literal["valid", "clarification_needed", "error"]
query: str | None
prose: str
error: ErrorDetail | None
metadata: {model, dialect, attempts: [...], latency_ms, usage: {...}}
```

`attempts` carries each candidate SQL and the validator verdict that rejected it. This is what makes
the repair loop *visible* in the demo and in the eval report; do not collapse it.

Ports (`typing.Protocol`, defined in `t2s_core.ports`):
- `InferenceClient.complete(messages, response_schema, **opts) -> InferenceResponse`
  - `FireworksClient` — OpenAI-compatible, `response_format` JSON schema (D7), explicit timeout,
    bounded retry with jitter on 429/5xx.
  - `RecordedClient` — replays fixtures keyed by a hash of the request (D8).
- `QueryValidator.check(sql, schema_ddl, dialect) -> Verdict`
  - `EphemeralSqliteValidator` (default for SQLite): in-memory DB built from the request's DDL,
    safety gate from D9, then `EXPLAIN`/dry-run. Catches unknown columns, bad joins, type errors —
    the failure modes prose review misses.
  - `SqlglotValidator` (non-SQLite dialects): parse + transpile only.
  - `NoOpValidator`: used to produce the loop-disabled arm of the eval.

Prompts live in `t2s_core/prompts/` as versioned template files with a registry, never as inline
string literals. Seed from the two hero prompts in `prompts/MAIN.md`.

## 4. `foundation` — stateful

SQLAlchemy + SQLite. Entities: `Project`, `Session`, `DataModel`, `DataModelVersion`, `Schema`,
`Query`, `Dataset`, `Database`. UUID primary keys (D-clarification 3 in MAIN.md). A default project
and session are auto-created per user so both are optional everywhere (clarification 2).

`DataModelVersion.graph` holds the dialect-neutral JSON entity graph; `Schema` is the rendered DDL
for one engine (D6). Rendering is `graph → sqlglot → DDL`, and it is a pure function worth its own
unit tests.

Sample database lifecycle — `create(schema) → load(dataset) → query(sql) → destroy()`. Files under
a managed directory; client-supplied paths never reach the filesystem (D9). Every `query` runs
read-only, timed out, and row-capped.

Two conversational tables were added for layer 3. `SessionState` holds the durable "where am I"
pointers. `Activity` (D14) is the **append-only transition log**: one row per `(step, phase)`,
`phase ∈ {begin, end}`, ordered by a per-session `seq`, carrying `status`, `duration_ms`, a
`summary`, a JSON `detail`, and `model`/`tokens`/`request_id` when inference was involved.
`ActivityRepository` exposes `append` and readers and **no mutation path at all** — that is the
invariant, and it is what makes an interrupted step legible as a `begin` with no `end` rather than
a status row stuck at "running". It is also, since D15, how a directive's referent resolves: "the
SQL for that" is the latest successful `query.generate` row, not a model call.

## 5. `api` — FastAPI

**Layer 1, deterministic CRUD (zero natural language):** projects, sessions, models, model versions,
schemas, datasets, databases, plus `POST /databases/{id}/load` and `POST /databases/{id}/query`.

**Layer 2, semi-deterministic and stateless:**
- `POST /text-to-sql/query` — `{question, schema_ddl, dialect, session_summary?}` → the envelope.
  Satisfiable entirely from the payload; no server state consulted. This endpoint alone answers the
  take-home's literal ask, which is why it must be the cleanest thing in the repo.
- `POST /text-to-sql/schema` — `{description, dialect}` → DDL + entity graph.

A later convenience veneer may accept `model_id` instead of inline `schema_ddl`; it resolves the
reference and calls the same stateless path. Build the stateless path first.

Errors are RFC 9457 `application/problem+json`. A model that returns `clarification_needed` is a
**200 with that response_class**, not an HTTP error — the request succeeded, the answer is a
question.

## 6. `cli` — click

`t2s query --schema f.sql --question "..."`, `t2s schema --describe "..."`,
`t2s db create|load|query`, `t2s eval run`. This is the live-demo surface; make its output readable
(show the repair attempts).

## 7. Eval harness — the deliverable that gets graded

Corpus: 3 schemas with deliberately different query character —
1. **retail/orders** — multi-hop joins, aggregates, date filtering
2. **library/lending** — many-to-many, self-referencing, nullable semantics
3. **events/analytics** — window functions, CTEs, group-by-having

~45 (question, gold SQL) pairs tiered easy/medium/hard, plus ~8 adversarial:
ambiguous questions that *should* return `clarification_needed`, and unanswerable-from-this-schema
questions that *should* return `error`. Silent hallucination on those is a failure.

Metrics reported:
- **Execution accuracy** (D3) — the headline, overall and by tier
- **Execution accuracy, repair loop off vs. on** — the design's payoff (D4)
- Valid-SQL rate (parses + binds)
- Abstention correctness on the adversarial set
- Latency p50/p95, token usage, cost per query

Outputs land in `evals/reports/` as JSON and markdown and are **committed**. The evidence is a file
in the repo, not a screenshot in an email.

## 8. Testing

- Unit: pure logic, `RecordedClient`, no network.
- Behavioral HTTP suite (`tests/bdd/`): use-cases stated against the HTTP contract, run against the
  app in-process with `RecordedClient`. Doubles as the API's executable specification.
- Property/adversarial: the safety gate rejects multi-statement, DDL-in-query-path, and
  `PRAGMA`/`ATTACH` attempts. Assert these explicitly — it's the security story's evidence.
- CI: lint, typecheck, import-linter contract, offline tests. Live eval is a separate target.

## 9. Deliberately deferred
Layer 3 NL orchestrator (intent classification, multi-step workflows), Slack bot, web modeler,
Postgres execution, auth/multi-tenancy. Named in the README with a sentence each, so the reader sees
the boundary was chosen rather than missed.

---

## Addendum — scorer contract (W4), fixed by the delivered corpus

The W2 manifest carries two fields beyond the original spec. W4 must honor both:

- **`allowed_response_classes`** (list) — the scorer tests set membership, not equality, against
  `expected_response_class`. Injection items legitimately pass as `valid` (answered the safe half),
  `error`, or `clarification_needed`; scoring them by equality would fail correct behavior.
- **`forbid_ddl_dml`** (bool) — a hard gate independent of response class. Any DDL or DML in the
  `query` field is an automatic fail even when the class is otherwise acceptable. Enforce this on
  the sqlglot AST, reusing the D9 gate — never by string matching for "DROP".

### Result-comparison policy (decided 2026-09-18)
Lenient on column *names* and numeric *type* (`500` == `500.0`); strict on column *count* and on the
row multiset. Order-sensitive iff the gold query contains `ORDER BY`. Rationale: this measures "did
it answer the question", not "did it match my formatting". Report a stricter variant (names must
match too) as a secondary number so the report shows the choice was made rather than assumed.

Known wrinkle to handle, not to fix: `events-h02` uses `RANK()`, so a user with two purchases tied
at their maximum returns both rows. That is correct behavior and the fixture contains such a tie —
a candidate query using `ROW_NUMBER()` will return one row and legitimately differ. Count it wrong
and note it in the report as a measured example of gold-query ambiguity; do not silently loosen the
comparison to hide it.
