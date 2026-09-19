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

---

## D11 — The eval runs multiple model arms, because the repair loop's value is model-dependent
W1 finding #6: eight engineered binder traps failed to make `kimi-k2p7-code` emit bind-failing SQL.
It either wrote correct dialect-aware SQL or abstained. So the loop-off vs loop-on delta on the
primary model may be near zero.

That is a **result**, not a setback, and we report it as one. But a single-arm eval would leave the
repair loop looking like unjustified machinery, so W4 runs the corpus across:
- `kimi-k2p7-code` (primary) — loop off, loop on
- a deliberately weaker model — loop off, loop on

Candidates for the weak arm, in preference order: `muse-glimmer-30b`,
`nemotron-lightning-3p5-30b-a3b`, `glm-5p3-flash` (known to leak chain-of-thought into `prose`, so
it also exercises the envelope-invariant repair path). Pick by a 5-item smoke test, not by name.

The claim this supports is stronger than the one we originally planned:
**scaffolding value scales inversely with model strength.** On a frontier code model the loop is
cheap insurance; on a cheaper model it is what makes the cheaper model usable. That is a real
engineering finding about where to spend inference budget, and it is only visible because the eval
has more than one arm.

~53 items × 2 arms × 2 models ≈ 210 calls. Cost is negligible; wall-clock is the only constraint.

## D12 — The sample-database path denies catalog access by default
W1 finding #8: "what columns does table X have?" is answered with `SELECT sql FROM sqlite_master`,
which is a legitimate single read-only SELECT and passes the D9 gate on its merits.

- In `t2s_core` this is **correct and stays allowed** — the request's own DDL is the entire world
  there, and the ephemeral DB is a throwaway built from that same DDL.
- In `foundation`'s sample-database execution path it is **denied by default**: add
  `sqlite_master` / `sqlite_schema` to a table denylist, with an explicit `allow_catalog: bool =
  False` parameter to opt in. Catalog enumeration against a persisted, potentially shared database
  is reconnaissance, and "the user owns this database" is an assumption that stops holding the
  moment anything is multi-tenant.

Document the opt-in rather than hiding the denial — a user asking "what tables exist?" should get a
clear refusal pointing at the CRUD endpoints that answer it deterministically, not a confusing
empty result.

## D13 — Correctives: stateless input now, lifecycle later
Proposed by Chris 2026-09-18. Accepted, scoped.

**Why it is not feature creep.** The repair loop (D4) catches only mechanical failure — unparseable
JSON, envelope invariant violations, safety-gate rejections, binder errors. W1 finding #6 showed
`kimi-k2p7-code` essentially never fails that way. The residual failure mode is *semantic*: SQL that
parses, binds, executes, returns rows, and answers the wrong question. No amount of model capability
fixes this, because the missing information is not in the DDL — that `revenue_cents` is cents, that
`status='C'` means cancelled and is usually excluded, that "revenue" is net of refunds. Correctives
are the repair mechanism for the error class the system is currently blind to.

**Two kinds. Do not conflate them.**

| | Domain corrective | Model corrective |
|---|---|---|
| example | "revenue_cents is cents" | "this model emits `DISTINCT ON` in SQLite" |
| scope | a `DataModel` | an `(inference_model, dialect)` pair |
| lifetime | outlives model choice | dies on model swap |
| owner | the user | us |
| storage | foundation, user data | the prompt registry, as a versioned template patch |

One bucket for both means you cannot switch LLMs without losing user domain knowledge, and cannot
patch a model quirk without editing user data. Phase 1 implements **domain correctives only**;
model correctives are named here so the split exists before anything is persisted.

**Phase 1 scope (stateless half only):**
- `QueryRequest.correctives: list[str] | None`. The stateless layer stays stateless — correctives
  arrive in the payload; assembling them is the caller's job. This is the first feature that would
  have tempted us to make the core stateful, and it does not have to.
- Injected as a clearly delimited section of the system prompt, new template version.
- Echoed in `metadata` so the eval can attribute outcomes to correctives.
- API and CLI pass-through.

**Security — correctives are a privileged injection point.** They land in the *system* prompt, which
is where "ignore previous instructions" is most effective. Required: the same DATA-not-instructions
framing proven in inference-findings §"Prompt injection"; a hard cap on count and per-item length;
a delimited section the model is told is user-supplied reference material, not instruction. A
corrective-borne injection attempt goes in the adversarial corpus.

**Why it earns its place in the eval.** Unlike the repair-loop delta (predicted ≈0 on a strong
model), the corrective delta should be large and should *stay* large as models improve, because it
supplies knowledge no model can infer from a schema. Measure accuracy with and without correctives
on items requiring domain knowledge.

**Product loop this unlocks (demoable, ~15s in the CLI):** an ambiguous question returns
`clarification_needed` → the user answers → the answer is recorded as a corrective → the same
question now returns a correct query. The corpus already contains the abstention items this needs.

**Deferred to phase 2:** persistence per DataModel, provenance, effectiveness tracking,
auto-promotion of clarification answers into correctives, conflict/precedence resolution beyond a
simple most-recent-wins ordering.

## D14 — A session is an append-only log of activities, not a mutable blob
Proposed by Chris 2026-09-19 after using the chat: "things are really quite slow and it's hard to
know what is going on."

The complaint is observability, not latency. A 7-second wait is tolerable when you can see what it
is doing; the same wait is intolerable when the terminal is silent. We currently persist session
*pointers* (current model, current database) — the state, with no record of how it was reached.

**Model.** `Activity` rows appended to a session, never updated. Each is one transition:
`seq`, `session_id`, `kind` (`router.classify`, `schema.generate`, `data.generate`, `data.load`,
`query.generate`, `query.repair`, `query.execute`, `corrective.record`), `phase` (`begin` | `end`),
`status`, `at`, `duration_ms`, `summary`, `detail` (JSON), plus `model`, `tokens`, `request_id`
where an inference call was involved. Begin and end are separate immutable rows rather than one row
mutated on completion — that is what makes it a transition log rather than a status table, and it
means a crashed step leaves evidence instead of a row stuck at "running".

**Ownership.** `foundation` persists it; the orchestrator emits it; surfaces subscribe. The TUI
shows a live line while a step runs and `/log` for history. The API can expose the same log per
session later, and the eval harness gets per-phase timings for free — which is the honest fix for
"p95 includes provider queueing", since we will finally have the breakdown.

**Why it is worth building now:** it is the instrument for every performance claim we make. We have
been reporting latency without being able to attribute it.

## D15 — One utterance yields an ordered list of directives, not one intent
Chris, same session: *"there isn't yet any notion of the input query having multiple directives in
it... we need a model where it is arbitrary how many directives are derived from a prompt."*

This is `prompts/MAIN.md`'s own "Interpretations of Natural Language speech-acts" section — the one
that asks for "a structured list of atomic interpretations" and trails off at a blank item 4. The
single-intent router was a phase-1 simplification; this restores the original design.

**Model.** The router returns a `Plan`: an ordered list of `Directive`s, each with an intent, its
parameters, and a referent slot. The orchestrator executes them in turn, appending activities per
directive (D14), rendering each result as it completes, and halting on failure with the completed
prefix still shown. A single-directive plan is the common case and must stay exactly as fast.

**What this fixes, in Chris's words:**
- *"Show me the query for X and sample results"* → `[query, execute]`. Today the second half is
  silently dropped.
- *"populate sample data and then show me a query for..."* → `[load_data, query]`, the utterance
  that truncated the router.
- *"Awesome — what's the SQL for that?"* → a directive whose referent is the previous activity's
  output. Referent resolution is a directive-level concern, which is why Chris's "scratch that" was
  the right instinct: continuity is not a separate feature, it is what a directive needs to name
  what it operates on.

**Constraints.** Cap the plan length and require each directive to be individually refusable — a
plan is a bigger blast radius than an intent, and a malicious or confused utterance must not become
a long chain of actions. Every directive passes the same D9 gate it would have as a lone intent.

## D16 — Reasoning is off for transcription, on for problem-solving
Measured twice, both times decisive: the router (971→0 reasoning tokens, 19x cheaper) and sample
data generation (**23.1s → 7.0s**, a 3.3x speedup on a 4-table schema).

Both are tasks where the model transcribes a structure we already validate, rather than solving a
problem. Reasoning expands to fill `max_tokens`, so a generous budget actively costs wall-clock.

Query and schema *generation* keep reasoning: writing correct SQL against an unfamiliar schema is
exactly the deliberation we are paying for. The rule is not "reasoning is waste" — it is
"reasoning is waste when the output is determined by the input".

Follow-on worth doing later: data generation should ask the model for a small *vocabulary* of
realistic values per column and expand deterministically in code, rather than generating every row
through the model. LLM for semantics, code for volume — and it makes seeds genuinely reproducible
rather than nominally so.
