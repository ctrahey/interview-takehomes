# Locked Decisions (ADR log)

Status: agreed with Chris 2026-09-18. Agents: treat these as binding. Raise a flag rather than
silently deviating.

## D1 — Time budget: ~1 focused day
Phase 1 must be submittable at the end of it. Every unit of work has a "degrades gracefully"
story: if it is not done, the repo still demonstrates a working, evidenced text-to-SQL system.

## D2 — In scope for phase 1
- `t2s_core` (stateless text-to-SQL) + **eval harness**
- `foundation` (persistence, sample databases)
- `api` (FastAPI) + behavioral HTTP test suite
- `cli` (click)

**Out of scope:** the fully-natural-language orchestrator layer (layer 3), the Slack bot, the Vue
web modeler. Layer 3's intent-classification idea is documented in the design as future work and
referenced in the README so the architectural thesis still reads intact.

## D3 — Correctness is execution accuracy, measured on a hand-authored corpus
We author the eval corpus ourselves: 3 schemas, seeded SQLite fixtures, ~45 (question, gold SQL)
pairs across easy/medium/hard tiers, plus ~8 adversarial items. A candidate query is correct iff
executing it against the seeded database yields the same result set as the gold query — compared
as a multiset of tuples, order-insensitive unless the gold query has an `ORDER BY`, in which case
order is compared too.

Rationale: self-contained (no external download in the critical path), and it exercises the DDL +
fixture generation machinery as the eval substrate — which is the design's central claim.

## D4 — The repair loop lives in the core, behind an injected port
`t2s_core` defines `QueryValidator` as a Protocol and accepts an implementation. The default
implementation builds an ephemeral in-memory SQLite database from the DDL supplied *in the request*
and bind-checks/dry-runs the candidate there. `sqlite3` is stdlib, so this preserves
"zero dependency on Foundation" literally and completely.

Flow: generate → validate → on error, re-prompt with the failing SQL and the engine's error text →
retry up to `max_repair_attempts` (default 2). Every attempt is recorded in the result metadata.

The eval harness reports accuracy **with the loop disabled and enabled**. That delta is the headline
result of the submission.

## D5 — Dialects
SQLite is the only engine we execute against. Postgres and MySQL are supported as *generation
targets*, validated by `sqlglot` parse + transpile only. The API never claims execution support for
a dialect it cannot run.

## D6 — Data Model vs Schema is real, not notional
A `DataModel` version persists a dialect-neutral JSON entity graph (tables, columns, types, primary
and foreign keys, constraints). A `Schema` is that graph rendered to concrete DDL for one engine.
This is what makes "models are purist, schemas are pragmatic projections" an actual mechanism rather
than a claim.

## D7 — Structured output is enforced, not requested
Fireworks is OpenAI-compatible and supports `response_format` with a JSON schema. Every LLM call
uses it. We never rely on prose instructions like "respond with JSON and nothing else", and we never
regex a code fence out of free text. Prompt text may restate the contract for the model's benefit,
but the schema is the enforcement.

## D8 — Tests never require a live API
`InferenceClient` is a Protocol. `FireworksClient` is the production implementation;
`RecordedClient` replays JSON fixtures captured from real calls. Unit tests, the HTTP behavioral
suite, and CI all run offline against `RecordedClient`. Only the eval harness hits the live API, and
it is invoked explicitly.

## D9 — Security constraints on generated SQL (non-negotiable, day 1)
Generated SQL is untrusted input to our own execution path.
- Query path: single statement only; `SELECT`/`WITH` allowlist enforced on the `sqlglot` AST before
  execution, never by string matching.
- Read-only connection (`PRAGMA query_only=ON`), wall-clock timeout, row cap on results.
- Sample databases are per-database files under a managed directory; no path supplied by a client
  ever reaches the filesystem.
- Schema text is attacker-controlled (table names, column comments are a prompt-injection vector).
  Treat supplied DDL as data: it is parsed by `sqlglot` before it is ever interpolated into a
  prompt, and parse failure is a 422, not a model call.
- API key from environment only. Never logged, never echoed in an error or a trace.

## D10 — Model selection is verified, not remembered
Do **not** hardcode a Fireworks model ID from memory. At build time, list the available models via
the Fireworks API (or current docs) and select a code-capable instruct model that supports JSON
schema response format. Record the chosen ID and the date in `memory/status.md`. Model ID is config
(`T2S_MODEL`), not a constant in the source.

## Open item (needs Chris)
- Fireworks API key: is one provisioned, and is there a spend ceiling for eval runs?
  Everything except the live eval run proceeds without it.
