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

## Integration constraints discovered during verification

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
