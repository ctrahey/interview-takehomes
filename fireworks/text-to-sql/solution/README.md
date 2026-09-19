# text-to-SQL

A text-to-SQL system built on Fireworks.ai. You describe a domain in English and get a schema; you
ask a question in English and get SQL. The part worth your attention is what happens in between:
**every generated query is executed against a real database built from your own schema before you
ever see it, and when the engine rejects it, the engine's own error text goes back to the model and
it tries again.**

That loop is the argument. Everything else here is in service of it.

---

## Run it

```bash
uv sync
uv run t2s-chat                  # conversational workbench
```

Talk to it in plain English — describe a domain, iterate on it, create a sample database, load data,
ask questions, run them, correct it. `/help` lists the slash shortcuts, all of which are also
sayable as sentences.

```bash
uv run uvicorn t2s_api.app:app   # HTTP API + OpenAPI docs at /docs
docker compose up                # same thing, containerised
docker compose --profile postgres up
```

**No API key?** It still starts. Everything about your own saved objects works, questions are routed
by keyword instead of by the model, and anything that would require *writing* SQL explains what it
needs rather than guessing. `T2S_OFFLINE=1` replays recorded fixtures with no network at all.

---

## The loop

### Tier 1 — every query, always, no data required

A generated query is bind-checked against a real SQLite database built in memory from the DDL in the
request. This costs nothing, needs no sample data, and runs on every query:

| probe | verdict | engine said |
| --- | --- | --- |
| a column that does not exist | **rejected** | `no such column: check_ins.definitely_not_a_column` |
| a table that does not exist | **rejected** | `no such table: members_typo` |
| a real query | accepted | — |

That is a live excerpt from [`docs/evidence/end-to-end.md`](docs/evidence/end-to-end.md), against a
schema the system had just designed seconds earlier, in a database with zero rows in it.

This is the tier that makes a plausible-looking wrong answer hard to hand back. Unknown columns,
unknown tables, bad `GROUP BY`, and syntax errors cannot survive it.

### Verify it yourself, with tools this project does not control

A system reporting that its own answers are correct is not evidence. So take the database away
from it:

```bash
uv run t2s db export 536c7b02 --out ~/Desktop/league.sqlite3
```

That writes an ordinary SQLite file. Open it in **DB Browser for SQLite**, or the stock `sqlite3`
shell, or anything else — nothing downstream of that file knows this project exists. Paste in the
SQL the chat showed you and compare the rows yourself.

From the containerised stack, mount a directory you own and export into it:

```bash
docker compose run --rm -v "$PWD:/export" --entrypoint t2s chat \
  db export 536c7b02 --out /export/league.sqlite3
```

The export uses SQLite's `VACUUM INTO` rather than a file copy, so it is a consistent snapshot
taken through SQLite itself — a database mid-write cannot produce a torn file. The source is opened
read-only and is never modified.

If you would rather not use our command at all, the file is just sitting there:
`~/.t2s/sample_dbs/<id>.sqlite3` locally, or `docker cp` out of the `sample_dbs` volume. Copy it
with `cp`. That is the strongest form of the check, and it needs nothing from us.

### Tier 2 — when a sample database has data

The system will create a sample database from your schema, generate and load data into it, and
**execute the query**, so you see rows rather than a promise. That is the innovation this project
leans on: the same primitives that design a schema also let the system check its own work against
one.

### On rejection, it adapts

A rejection is not an error handed to the user. The failing SQL and the engine's verbatim message
are appended to the conversation and the model tries again, up to a bounded number of attempts.
Every attempt is retained, which is why the CLI and the chat can show you the trail instead of
quietly presenting attempt three as if it had been attempt one.

Four different failure classes funnel through that one path: unparseable JSON, a response violating
its own envelope invariants, a safety-gate rejection, and a binder error. They are all "the model
produced something we can prove is wrong", so they all get the same treatment.

**See it happen:** [`docs/evidence/self-correction.md`](docs/evidence/self-correction.md), captured
live against `muse-glimmer-30b` — a rejection and a successful recovery, with nothing staged.

Be precise about what that artifact shows, because it is less flattering than it could be. The
rejection is an **envelope** violation: the model omitted a required field of the response contract.
The SQL was fine both times. That is the failure mode cheap models actually exhibit, and it is why
the loop funnels four different failure classes through one path.

The **binder** branch — where SQLite rejects the SQL itself and the model rewrites it — is covered by
unit tests using a first attempt that is explicitly flagged synthetic in the fixture data. It is
**not** reproduced live here, because no model I tried would produce a bind-failing query on demand.
Six were tried, including a schema whose obvious column names (`email`, `signup_date`, `total`) were
all deliberately wrong. Every one of them read the DDL out of the prompt and got it right. Staging a
fake first attempt would have made a better screenshot and a worse repository.

---

## Evidence, and its limits

Two artifacts under [`docs/evidence/`](docs/evidence/), both regenerable with a key:

```bash
uv run python scripts/capture_evidence.py   # describe -> DDL -> database -> validate -> query
uv run python scripts/capture_repair.py     # a real rejection and recovery
```

Neither is staged. `capture_repair.py` refuses to write anything if the model does not actually
fail, rather than inventing a first attempt.

**An honest limitation, stated up front.** On a frontier code model the repair loop almost never
fires. The schema is in the prompt, so the model can read the column names; eight engineered traps
failed to induce a single binder error on `kimi-k2p7-code`, and a four-arm evaluation recorded zero
repairs across 53 items. The loop's measurable value shows up on cheaper models — where the same
corpus goes from **0% to 37.8%** with the loop enabled, because a model that cannot reliably emit a
valid response envelope becomes usable once something checks and corrects it. That is the honest
case for building it: not that it rescues a good model, but that it decides which models you can
afford to use.

### Regression suite

A 53-item corpus (45 question/gold-SQL pairs across three schemas, plus 8 adversarial items) scored
by **execution accuracy** — run the gold query and the candidate against the same seeded database
and compare result sets. Reports in [`evals/reports/`](evals/reports/).

The first run scored 15.6% and the diagnosis is worth more than the number: 44% of failures were
`column_count_mismatch`. Questions like *"which customers are in Portland?"* never said whether they
wanted names, or names and emails, or the whole row — so the gold column list was unknowable and the
corpus was measuring telepathy. We rewrote 38 questions to state the output shape they already
implied, changed **no gold SQL**, and re-ran: **51.1%**, with `column_count_mismatch` falling from 71
occurrences to 1. Both reports are committed, and
[`evals/corpus/REVISIONS.md`](evals/corpus/REVISIONS.md) logs every edit with a justification.

Row-order mismatch is now the dominant failure at 36%. It is deliberately not fixed, so that the
corpus revision had exactly one cause.

The corpus is a regression test. It is not the argument.

---

## Architecture

```
          ┌───────────┐   ┌───────────┐
HTTP ────▶│  t2s_api  │   │ t2s-chat  │
          └─────┬─────┘   └─────┬─────┘
                │               │
          ┌─────▼───────────────▼─────┐
          │          t2s_nl           │  intent → ordered directives,
          │  conversational layer     │  executed in turn
          └─────┬───────────────┬─────┘
                │               │
      ┌─────────▼──────┐  ┌─────▼──────────┐
      │   foundation   │  │    t2s_core    │  stateless; depends on
      │  persistence   │  │  generation    │  nothing above it
      └────────────────┘  └───────┬────────┘
                                  │ ports
                    ┌─────────────┴────────────┐
             InferenceClient            QueryValidator
          (Fireworks | Recorded)  (EphemeralSqlite | Sqlglot | NoOp)
```

`t2s_core` is stateless and imports nothing from any layer above it. That is not a convention —
it is an **import-linter contract checked in CI**, verified by deliberately violating it and
confirming the build fails. It is what lets the same core run inside the API, the CLI, the chat, and
the eval harness without dragging a database along.

Three layers, deliberately:

1. **Deterministic CRUD** — projects, sessions, data models, schemas, sample databases. Zero natural
   language.
2. **Stateless generation** — question plus schema in, SQL out. Satisfiable entirely from the request
   payload, which is what the take-home literally asks for.
3. **Conversational** — one utterance becomes an *ordered list of directives*, executed in turn.
   "Load it with data and then show me unpaid balances" is two directives, not a dropped half.

A load-bearing rule in layer 3: **the model classifies and generates; it never reports state.** When
you ask "show me my databases", a model picks the intent and an ordinary database read produces the
answer. A test parses the inspection module's imports and fails if an inference client appears there.

A **data model** is a dialect-neutral entity graph; a **schema** is that graph rendered to one
dialect's DDL. The eight example schemas in [`examples/`](examples/) are ingested through exactly
that pipeline — Postgres DDL in, entity graph, SQLite out — which is how a circular foreign key and a
duplicated constraint in the upstream sources resolve without special-casing.

Depth lives in [`memory/decisions.md`](../memory/decisions.md) (16 ADRs) and
[`memory/design.md`](../memory/design.md).

---

## Security

Generated SQL is untrusted input to our own execution path.

- **AST-gated, never string-matched.** Single statement, `SELECT`/`WITH` only, enforced on the
  sqlglot AST. It blocks `INSERT`/`UPDATE` smuggled inside a CTE (both parse as a root `SELECT`,
  which defeats naive root-node checks), `load_extension`, `readfile`, stacked `ATTACH`, and
  `PRAGMA query_only=OFF` — while correctly allowing `SELECT 'DROP TABLE t' AS note`.
- **Defence in depth.** Read-only connections (`PRAGMA query_only=ON`), wall-clock timeouts, row
  caps. Sample database paths derive from UUIDs, so no client-supplied path reaches the filesystem.
- **Prompt injection is tested, not asserted.** Two real attacks live in the adversarial corpus.
  Schema text is attacker-controlled, so supplied DDL is parsed before it is ever interpolated into
  a prompt.
- **Catalog access denied by default** on persisted sample databases, behind an explicit opt-in.
- **Credentials** come from the environment, are never logged, and never appear in an error, a
  response, or a container image.

---

## Testing

**654 tests**, all offline — a recorded-fixture inference client means CI needs no API key and no
network. `make check` runs lint, types, the import contract, and the suite.

```bash
make check
```

---

## Deliberately not built

- **Authentication.** Anyone who can reach the API has full access. This is a demo, not a deployment.
- **Postgres as an execution engine.** It is a generation target, validated by parse and transpile;
  only SQLite is executed. The compose Postgres profile is a metadata store.
- **Migrations.** `create_all`, not Alembic.
- **Correctives as a first-class field.** They work and persist, but still travel inside a prompt
  field rather than a dedicated one.
- **A web UI.** The HTTP surface for one exists — chat, activity log, and an SSE stream — but no
  frontend consumes it yet.
