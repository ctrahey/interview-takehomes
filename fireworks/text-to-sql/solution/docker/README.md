# Running text-to-sql in Docker

Five minutes to a browser tab against a live API. Everything here is driven
by `Dockerfile` and `docker-compose.yml` at the repo root (`solution/`); this
file is the walkthrough.

## Quick start (no key, no Postgres)

```bash
cd solution
docker compose up --build
```

Open the interactive API docs at **http://localhost:8000/docs** (or
`http://localhost:8000/openapi.json` for the raw schema). Every layer-1 CRUD
endpoint — projects, sessions, data models, schemas, datasets, sample
databases — works right away. `t2s_api.app.create_app` defers building a
live Fireworks client until a layer-2 request (`/text-to-sql/query`,
`/text-to-sql/schema`) actually needs one, so the container starts and
reports **healthy with no `FIREWORKS_API_KEY` set at all**.

## Adding a Fireworks key

The key must never be baked into the image or passed as a build arg. Three
runtime routes, in order of least copying:

**1. Mount the file you already have (default, nothing to set up).**
`~/.fireworks-key` is bind-mounted read-only to `/home/t2s/.fireworks-key`,
which is exactly where the app looks. Nothing is copied, nothing lands in your
shell environment, and nothing lands in `.env`:

```bash
cd solution
docker compose up --build          # the key file comes along
docker compose run --rm chat
```

If the host file does not exist, Docker materialises an empty directory at that
path; `read_api_key()` checks `is_file()` and treats that as "no key", so a
keyless machine still starts cleanly rather than crashing.

**2. Forward it from your shell**, if you keep it somewhere else:

```bash
FIREWORKS_API_KEY="$(cat ~/.fireworks-key)" docker compose run --rm chat
```

**3. Put it in `.env`** (gitignored, and excluded from the build context):

```bash
printf 'FIREWORKS_API_KEY=%s\n' "$(cat ~/.fireworks-key)" >> .env
```

Note the host's `~/.fireworks-key` is *not* otherwise visible inside a
container, so the error message there names the routes above rather than
telling you to create a file the container cannot see.

Now `/text-to-sql/query` and `/text-to-sql/schema` make live calls. `T2S_MODEL`
/ `T2S_MODEL_ALT` in the same `.env` (see `.env.example`) select the model.

## A UI on your host talking to the API in the container

The API's CORS defaults (`t2s_api.app.DEFAULT_CORS_ORIGINS`) already allow
`http://localhost:5173`, `127.0.0.1:5173`, `:3000`, and `127.0.0.1:3000` —
i.e. a Vite or webpack dev server run directly on your host, outside Docker,
talking to `http://localhost:8000`. That is exactly the "containerized API +
host-run dev server" split most people hit, and it works with zero
configuration. If your UI runs on a different origin, set it in `.env`:

```bash
T2S_CORS_ORIGINS=http://localhost:4200,http://my-ui.local:8080
```

(overrides the defaults entirely — it is not additive — see
`t2s_api.app._cors_origins`).

## The conversational CLI (`t2s-chat`)

Same image, run as a one-off rather than a daemon:

```bash
docker compose run --rm chat
```

It shares both named volumes with `api` (below), so a sample database or
data model you create in the chat session is immediately visible to the API,
and vice versa — they are the same underlying state, not two copies of it.
`docker compose run` reaches `chat` directly even though it isn't part of
`docker compose up` (verified: profiled/unlisted services are excluded from
`up`, but `run <service>` names it explicitly and starts it regardless).

## Where data persists

Two named volumes, both declared in `docker-compose.yml`:

| volume | mounted at | what it holds | why a volume and not scratch space |
|---|---|---|---|
| `sample_dbs` | `/data/sample_dbs` | the actual SQLite files for sample databases you create (D9's managed directory) | these are real files a user spends real time creating and loading; losing them on `docker compose down` / a container restart would be a bad surprise |
| `foundation_meta` | `/data/foundation` | foundation's own metadata store — projects, sessions, data models, schemas, activities — as a SQLite file, when running the (default) SQLite metadata store | same reasoning: a session/conversation is meant to survive a restart (D14) |

`docker compose down` leaves both volumes intact. `docker compose down -v`
(or `docker volume rm`) is what actually deletes them.

## Postgres — there are two different questions here, and only one is in scope

Chris asked for a Postgres sidecar "in case I want to run with... a postgres
sidecar" without specifying which of two genuinely different things he meant.
Both are addressed, only one is implemented:

### (a) Postgres as the foundation *metadata store* — implemented

Projects, sessions, data models, schemas, datasets, database *records*,
queries, correctives, activities: all of it lives behind SQLAlchemy, which
already abstracts the DB URL (`foundation.db.create_foundation_engine`). This
is a config change, not a code change:

```bash
docker compose --profile postgres up -d postgres
T2S_API_DB_URL=postgresql+psycopg://t2s:t2s@postgres:5432/t2s \
T2S_DB_URL=postgresql+psycopg://t2s:t2s@postgres:5432/t2s \
  docker compose up --build api
# and, to point `chat` at the same Postgres-backed state:
T2S_DB_URL=postgresql+psycopg://t2s:t2s@postgres:5432/t2s \
  docker compose run --rm chat
```

(`T2S_API_DB_URL` is read by `t2s_api.db.default_engine`; `T2S_DB_URL` is
read by `t2s_nl.store.default_database_url` for the chat client. They are
two different env vars for the same *kind* of store because `api` and `chat`
each open their own engine — set both to the same URL to have them share
state through Postgres instead of the SQLite volume.)

What we found auditing `foundation` for SQLite-specifics: the ORM models
(`foundation/models.py`) use only portable SQLAlchemy types — `Uuid`,
`JSON`, `String`, `Text`, `DateTime`, `Boolean`, `Integer` — so they map
cleanly onto Postgres's native `uuid`/`json`/etc. types with zero schema
changes. `create_foundation_engine` already branches on
`url.startswith("sqlite")` for the two things that genuinely are
SQLite-only: `check_same_thread` and `PRAGMA foreign_keys=ON` (Postgres
enforces FKs by default, no pragma needed). The one missing piece was a
driver: nothing in the workspace depended on `psycopg`/`psycopg2`, so the
image installs `psycopg[binary]` at build time (`Dockerfile`, right after
the dependency-only `uv sync` layer) rather than adding it to
`packages/foundation/pyproject.toml` — keeping this work additive and
`packages/foundation` untouched, per scope.

Verified against a live Postgres container: `init_db` creates all tables,
the default project/session bootstrap runs (idempotent unique-slug path,
`foundation.bootstrap`), and a full CRUD round trip (create project → create
session → create data model → read it back) succeeds. See the W14 agent
report for the exact commands and output.

### (b) Postgres as an *executable engine for sample databases* — NOT implemented, and here is what it would take

This is a different question: could a user's *sample database* — the thing
`/databases/{id}/query` runs generated SQL against — be a real Postgres
instance instead of the SQLite files under `T2S_SAMPLE_DB_DIR`? **No, by
design** (memory/decisions.md D5): "SQLite is the only engine we execute
against. Postgres and MySQL are supported as generation targets, validated
by `sqlglot` parse + transpile only." `foundation/sample_db.py` is hard-wired
to Python's `sqlite3` module — the read-only `file:...?mode=ro` connection
string, the `PRAGMA query_only=ON`, the wall-clock timeout via SQLite's
progress handler, the row cap via `fetchmany` — none of it is
engine-abstracted, on purpose, because D9's security guarantees were
verified specifically against SQLite's semantics.

Having a Postgres sidecar in `docker-compose.yml` makes "just point sample
databases at Postgres too" newly *plausible* to build, which is exactly why
this paragraph exists instead of the door just quietly staying shut. What it
would actually take:

1. Generalize `foundation/sample_db.py`'s `create`/`load`/`query`/`destroy`
   lifecycle behind a small engine-specific backend interface (mirroring
   `t2s_core.ports.QueryValidator`'s pattern of an injected port), with the
   current SQLite implementation becoming one concrete backend.
2. Re-derive D9's three-layer enforcement for Postgres independently — the
   read-only *connection* trick is SQLite-file-specific
   (`mode=ro`); Postgres's equivalent is a role granted only `SELECT`, plus
   `SET TRANSACTION READ ONLY` per session, plus `statement_timeout` for the
   wall-clock bound instead of a progress-handler callback. None of these are
   drop-in replacements; each needs its own adversarial test pass (the kind
   `foundation/tests` already has for SQLite).
3. Per-sample-database isolation: today each sample DB is its own file under
   a managed directory, so one user's data literally cannot leak into
   another's. Postgres has no equivalent of "a fresh file" — it would need
   either one schema-per-database-record inside a shared instance, or
   provisioning/destroying whole databases via `CREATE DATABASE`/`DROP
   DATABASE`, both of which are a materially bigger operational surface than
   `Path.unlink()`.
4. Decide what `dialect="postgres"` even means once it's executable and not
   just a transpile target — does the eval corpus (D3, SQLite-seeded
   fixtures) need a parallel Postgres-seeded variant, or does execution
   accuracy stay defined against SQLite and Postgres becomes "generate +
   transpile + execute" only outside the graded eval path?

None of this is started. The `postgres` compose service today only ever
backs the metadata store.

## What this setup would still not be right for in production

- **The `postgres` service ships default credentials** (`t2s`/`t2s`) meant
  for local opt-in use, not a real deployment — no secrets manager, no TLS
  to Postgres, no connection-pooling story (PgBouncer, etc.).
- **No multi-replica story.** SQLite's default metadata store is a single
  file behind a single writer; even on the Postgres profile, the app has had
  zero load-bearing testing for concurrent instances, migrations-on-deploy,
  or graceful drain.
- **No schema migrations.** `init_db` is `Base.metadata.create_all` — fine
  for a fresh volume, but there is no Alembic (or equivalent) story for
  evolving a live Postgres schema across versions.
- **The image runs one process (uvicorn, no supervisor, no multi-worker
  story).** Fine for a demo; a real deployment wants a process manager or
  `uvicorn --workers N` behind a real load balancer, plus log shipping (this
  image just writes to stdout).
- **Auth is still out of scope** (D2 / design §9) — the container has no
  concept of a user, so anyone who can reach port 8000 has full CRUD and
  (with a key) full layer-2 access.

## Starting clean

```bash
docker compose down -v                      # drops foundation_meta + sample_dbs
docker compose --profile postgres down -v   # also drops postgres_data
```

`-v` is the part that matters: without it the named volumes survive, which is
usually what you want and occasionally exactly what you do not.

Locally, the equivalent is `uv run t2s reset` (it prints what it will delete and
asks first; `--yes` skips the prompt).

## After pulling changes

`docker compose up` and `docker compose run` **reuse the existing `t2s:local`
image** -- they do not rebuild on their own. After anything lands, rebuild:

```bash
docker compose up --build
# or
docker compose build
```

A container running last week's code while the repo has this week's is a
confusing way to lose an hour.
