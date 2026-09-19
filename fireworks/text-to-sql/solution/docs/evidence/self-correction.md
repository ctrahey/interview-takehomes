# Self-correction, captured live

Captured 2026-09-19 05:38 UTC against `accounts/fireworks/models/muse-glimmer-30b`.
Regenerate with `uv run python scripts/capture_repair.py`.

**The model is chosen deliberately and it is not our primary one.** On `kimi-k2p7-code` this loop almost never fires: the schema is in the prompt, so the model can read the column names, and six models were tried before one produced a rejection worth showing. That is itself the finding -- the loop is insurance whose value scales inversely with model strength, and it is what makes a cheap model usable at all.

Question asked: _Which customers placed orders in every month of 2024?_

## Attempt 1 — rejected

```sql
SELECT c.id, c.name FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.order_date >= '2024-01-01' AND o.order_date < '2025-01-01' GROUP BY c.id, c.name HAVING COUNT(DISTINCT strftime('%m', o.order_date)) = 12
```

Rejected by the validator as `envelope_invariant`:

> response_class: Field required

That exact text is fed back to the model as the next turn -- our validator's words, not a paraphrase of them.

## Attempt 2 — accepted

```sql
SELECT c.id, c.name FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.order_date >= '2024-01-01' AND o.order_date < '2025-01-01' GROUP BY c.id, c.name HAVING COUNT(DISTINCT strftime('%m', o.order_date)) = 12
```

## Note: the SQL did not change

Both attempts carry identical SQL. The rejection here was an **envelope** failure -- the model omitted a required field of the response contract -- not a bad query. This is the failure mode cheap models actually exhibit, and it is exactly why the repair loop funnels envelope violations, safety-gate rejections and binder errors through one path: from the caller's side they are all 'the model produced something we can prove is wrong'.

The binder branch -- where the *SQL itself* is rejected by SQLite and rewritten -- is covered by unit tests using a seeded first attempt that is explicitly flagged synthetic in the fixture data. It is not reproduced here live because no model tried would produce a bind-failing query on demand; see the README.

## Outcome

`valid` after 2 attempts, validated by `ephemeral_sqlite`.

Every attempt is retained in `metadata.attempts`, which is what lets the CLI and the chat render the trail instead of silently presenting the final answer as if it had been the first.
