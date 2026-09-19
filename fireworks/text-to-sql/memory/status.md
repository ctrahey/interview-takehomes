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
