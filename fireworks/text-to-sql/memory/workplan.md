# Agent Work Plan — Phase 1 (~1 day)

Reads: `memory/decisions.md` (binding) and `memory/design.md` (spec). Every agent gets both, plus
its own unit below. Status is tracked in `memory/status.md`.

Sequencing is by dependency, not by clock. W0 must land before anything else starts.

## W0 — Scaffold  (blocking, ~20 min, single agent)
uv workspace with the four packages; ruff + mypy + pytest + import-linter configured; the
`t2s_core ↛ foundation` contract encoded and failing-by-design verified; Makefile targets
(`setup test lint eval serve demo`); CI workflow running the offline suite; `.env.example` with
`FIREWORKS_API_KEY` and `T2S_MODEL`.
**Done when:** `make lint test` passes green on an empty-but-wired repo.

## W1 — t2s_core  (depends W0; the critical path — staff the strongest agent here)
Envelope models; `ports.py` Protocols; `FireworksClient` with JSON-schema response format, timeout,
bounded retry; `RecordedClient` + fixture capture helper; `EphemeralSqliteValidator` including the
D9 safety gate; `SqlglotValidator`; `NoOpValidator`; the repair loop with per-attempt tracing; the
prompt template registry seeded from `prompts/MAIN.md`'s two hero prompts.
First task in this unit: resolve the model ID per D10 and write it to `memory/status.md`.
**Done when:** `generate_query` returns a correct envelope for a hand-checked example under
`RecordedClient`, with attempts traced, and the safety-gate rejection tests pass.

## W2 — Eval corpus  (depends W0 only — runs fully parallel to W1)
The 3 schemas, seeded fixtures, ~45 tiered (question, gold SQL) pairs, ~8 adversarial items with
their expected `response_class`. Every gold query must execute against its seeded fixture and return
a non-empty, non-trivial result — assert that in a test, or the corpus silently rots.
**Done when:** `pytest evals/corpus` proves every gold query runs and the expected-result snapshots
are stable.

## W3 — foundation  (depends W0; parallel to W1/W2)
SQLAlchemy entities with UUID keys; default project/session bootstrap; repositories; the entity
graph ↔ DDL rendering as a pure, unit-tested function; sample database lifecycle with the read-only
/ timeout / row-cap execution path.
**Done when:** a model graph round-trips to SQLite DDL, a sample DB is created, loaded, queried
read-only, and destroyed, all under test.

## W4 — Eval harness  (depends W1 + W2)
Runner, scorer (multiset comparison, order-sensitive iff gold has `ORDER BY`), the loop-off/loop-on
comparison arms, JSON + markdown reporters, `make eval`.
**Done when:** a full run against `RecordedClient` produces a committed report with every metric in
design §7 populated.

## W5 — api  (depends W1 + W3)
Layer 1 CRUD; layer 2 stateless endpoints; RFC 9457 error handling; `clarification_needed` as a 200.
**Done when:** OpenAPI spec generates cleanly and every endpoint has at least one passing test.

## W6 — Behavioral HTTP suite  (depends W5)
The use cases from `prompts/MAIN.md` — Hero Story 1 and 2 — stated against the HTTP contract, in
process, offline.
**Done when:** both hero stories pass end-to-end with no network.

## W7 — cli  (depends W1 + W3; parallel to W5/W6)
The commands in design §6, with readable output that surfaces the repair attempts.
**Done when:** `t2s query` answers a corpus question from a schema file on disk.

## W8 — Evidence & polish  (last, and do not let it get squeezed)
Top-level README: what it is, how to run it in one command, the architecture diagram, the eval
results table with the repair-loop delta called out, the security posture, and the deferred-scope
list with a sentence each. Plus a `/security-review` pass over the diff.
**Done when:** a reader who has never seen the repo can run the demo and read the evidence in under
five minutes.

## Parallelism map
```
W0 ──┬── W1 ──┬── W4 ──┐
     ├── W2 ──┘        ├── W8
     └── W3 ──┬── W5 ── W6 ─┤
              └── W7 ───────┘
```
Peak concurrency is 3 (W1 ∥ W2 ∥ W3), then 3 again (W4 ∥ W5 ∥ W7).

## If we run short on time, cut in this order
1. W7 CLI (the API + BDD suite already demonstrates everything)
2. W5/W6 layer-1 CRUD breadth — keep layer 2 whole, thin the CRUD surface
3. Corpus size — 45 pairs → 25, keeping all three schemas and every adversarial item

Never cut: W1, W2's adversarial items, W4, or W8. Those four *are* the submission.

## Live-API budget
Only W4's real run and W1's fixture capture hit Fireworks. Capture fixtures once, replay everywhere.
