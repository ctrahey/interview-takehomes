# Eval corpus

Hand-authored evidence for D3 (decisions.md): correctness is measured as execution accuracy
against a corpus we own end to end — three SQLite schemas, deterministic seed fixtures, and a
manifest of (question, gold SQL) pairs plus adversarial items. W4's harness consumes this corpus;
this package never calls the Fireworks API and has no dependency on `t2s_core` or `foundation`.

## Layout

```
evals/corpus/
  README.md            this file
  manifest.json         the machine-readable item list (see format below)
  test_corpus.py         pytest verification suite — run: uv run pytest evals/corpus/test_corpus.py
  schemas/
    retail/ddl.sql       multi-hop joins, aggregates, date filtering
    retail/seed.sql
    library/ddl.sql       many-to-many, self-referencing hierarchy, meaningful NULLs
    library/seed.sql
    events/ddl.sql        window functions, CTEs, GROUP BY ... HAVING
    events/seed.sql
```

Each `seed.sql` is a **static, hand-committed artifact**. It was produced once by a small
Python generator using a fixed `random.Random` seed (`20260918`); the generator itself is not
part of the repo, only its output. Nothing regenerates the seed files at test time or at import
time, so the corpus is byte-reproducible by construction — re-running the test suite (or `git
diff`ing the seed files) can never show drift.

## Schemas

### `retail` — customers / products / orders / order_items
Multi-hop joins (customer → order → order_item → product), revenue aggregation, date-range and
month/year-boundary filtering.

Baked-in edge cases: customer id 32 has placed zero orders (LEFT JOIN / anti-join target); three
customers have a `NULL` email; an order dated `2024-02-29` (leap day); an order pair straddling
the `2023-12-31` / `2024-01-01` year boundary; unshipped orders (`shipped_date IS NULL`) for
`pending`/`cancelled` status; three discontinued products.

### `library` — categories / authors / books / book_authors / members / loans
Many-to-many via `book_authors`; a self-referencing `categories.parent_category_id` hierarchy
three levels deep (e.g. Fiction → Science Fiction → Space Opera); `NULL` with real business
meaning in two places (`members.membership_expiry IS NULL` = lifetime membership,
`loans.returned_date IS NULL` = still checked out).

Baked-in edge cases: member id 38 has zero loans; two authors have an unknown `birth_year`;
a natural tie at exactly 8 loans (members 15 and 25) and at 7 loans (members 21 and 35), usable
for `HAVING`/ranking boundary questions; an explicit unambiguously-overdue open loan (member 1).

### `events` — users / events
Window functions (running totals, per-user ranking), CTEs, `GROUP BY ... HAVING` thresholds.
`events.revenue_cents` is `NULL` for every non-`purchase` event and always set for `purchase`
events — a meaningful, type-conditional NULL.

Baked-in edge cases: user id 35 has zero events; a purchase pair straddling the year boundary
(`2023-12-31 23:40` / `2024-01-01 00:15`); a real tie at exactly 3 purchases across several users
(for `HAVING >= 3`); two users with a purchase inside 24h of their own signup event (for the
"fast converter" hard-tier query, which would otherwise have no guaranteed match against random
data).

## Manifest format (`manifest.json`)

```json
{
  "format_version": 1,
  "items": [
    {
      "id": "retail-m01",
      "schema": "retail",
      "tier": "medium",
      "question": "How many orders has each customer placed, including customers who haven't ordered anything?",
      "gold_sql": "SELECT ... ;",
      "expected_response_class": "valid",
      "allowed_response_classes": null,
      "forbid_ddl_dml": false,
      "rationale": "LEFT JOIN + GROUP BY; surfaces the zero-order customer."
    }
  ]
}
```

Field reference:

| field | type | notes |
|---|---|---|
| `id` | string | stable, unique across the whole manifest. Prefix encodes schema + tier (e.g. `library-h03`, `adv-inj-01`). |
| `schema` | string | one of `retail`, `library`, `events`. |
| `tier` | string | one of `easy`, `medium`, `hard`, `adversarial`. |
| `question` | string | phrased like an analyst would ask it, not SQL read aloud. Never contains a literal snake_case column name. |
| `gold_sql` | string \| null | a single `SELECT`/`WITH` statement, always terminated with `;`. **`null` iff `tier == "adversarial"`.** |
| `expected_response_class` | string | one of `valid`, `clarification_needed`, `error` — the primary expected outcome from the system under test. |
| `allowed_response_classes` | list[string] \| null | present only where more than one class is a legitimate outcome (used for the two injection items, where a refusal is as acceptable as a safely-narrowed answer). When set, always includes `expected_response_class`. |
| `forbid_ddl_dml` | bool | when `true` (only the two injection items), the harness must additionally assert the candidate query contains no DDL/DML regardless of which `response_class` came back. This is on top of, not instead of, the engine-level safety gate (D9) — the corpus asserts the *outcome* is safe; the gate is what *makes* it safe. |
| `rationale` | string | one sentence: what capability/edge-case this item is probing. |

For gold (non-adversarial) items, `expected_response_class` is always `"valid"` and
`allowed_response_classes` is `null`.

## Adversarial items (8)

- `adv-inj-01`, `adv-inj-02` — the two real prompt-injection attacks captured during live
  Fireworks probing (`memory/inference-findings.md`), reproduced **verbatim**, adapted only in
  that they target the `retail` schema (which already has a `customers` table and an `orders`
  table, so no wording needed to change). Both allow `valid`, `clarification_needed`, or `error`
  as the response class — the pass/fail bar is `forbid_ddl_dml`, not the class.
- `adv-amb-01..03` — one per schema, ambiguous-but-answerable questions ("best customers",
  "popular books", "top users") that have no single correct interpretation against this schema.
  Expected: `clarification_needed`.
- `adv-err-01..03` — one per schema, questions that reference a concept absent from the schema
  (satisfaction scores, late-return fines, marketing campaigns). Expected: `error`. Two of the
  three (`library`, `retail`) are *partially* plausible — e.g. lateness is derivable but a fine
  amount is not — specifically to tempt a model into hallucinating a column.

## Verification (`test_corpus.py`)

`uv run pytest evals/corpus/test_corpus.py -v` — 29 tests, all offline, no network:

1. **Fixtures load cleanly** — every schema's DDL + seed loads into a fresh in-memory SQLite DB;
   every table lands in the intended row-count band (20-260, with a documented exception for the
   13-row `categories` reference table); the specific edge-case rows described above are asserted
   present by id/predicate, not just "some NULL somewhere."
2. **Manifest format validation** — required keys present on every item, ids unique, `schema`/
   `tier`/`*_response_class` values are from the closed vocabulary, `gold_sql` is present iff the
   item isn't adversarial, questions don't read like SQL and don't leak snake_case column names.
3. **Adversarial-specific checks** — the two injection attacks are present character-for-character
   and flagged `forbid_ddl_dml`; ambiguous items expect `clarification_needed`; unanswerable items
   expect `error`.
4. **Every gold query executes and returns a non-empty result** — a 0-row gold query fails the
   suite outright (a corpus bug, per the task brief). Also checks single-statement-only and no
   DDL/DML keywords in our own gold SQL (belt-and-suspenders on the corpus itself).
5. **Snapshot stability** — every gold query is run twice against independently rebuilt databases;
   results must match exactly (order-sensitive when the gold SQL has `ORDER BY`, multiset
   comparison otherwise). Also checks the seed files are literally byte-identical on repeat reads.
6. **Tier-appropriate structure** — every `hard` item must contain a CTE, a window function
   (`OVER (`), or a subquery (a second `SELECT`); every `medium` item must contain a `JOIN` or a
   `GROUP BY`; every `easy` item must be single-table (no `JOIN`). No gold query is a bare
   `SELECT * FROM table` unless it's also doing one of the above.

## Manual semantic re-read

Execution success (tests 4-6 above) proves a query *runs* and *returns something*; it does not
prove the query answers the question that was asked. Every one of the 45 gold pairs was re-read
by hand against its English question after the automated suite went green — see the W2 report for
the two ambiguities that surfaced and how they were resolved.
