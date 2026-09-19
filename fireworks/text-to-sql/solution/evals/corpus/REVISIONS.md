# Corpus revisions

Every change ever made to `manifest.json` after a live eval run is recorded here, with the item's
question before and after and a one-line justification. `revisions.json` is the machine-readable
twin of this file; `test_corpus.py` asserts the two agree with the manifest and that every gold
item is accounted for, so this log cannot quietly drift from what was actually shipped.

---

## W12 — 2026-09-18 — questions that did not state what to return

### What was wrong

The first live 4-arm run (`evals/reports/run-20260918-161313.md`) scored **44% of all failures as
`column_count_mismatch`**: the candidate produced the gold's rows, under the gold's `WHERE` clause,
with a different column list. `retail-e01` is the clean example — the question was "Which products
have been discontinued?", the gold returns three columns, and every arm that answered it returned
six. Nothing in the question said which three.

That is a defect in the measuring instrument, not in the system under test. A question that does
not state its output shape makes the gold query's projection unknowable, and the item ends up
measuring column-list telepathy rather than SQL skill.

### The rule applied

A question was rewritten **only** to state the output shape it already implied, in the language an
analyst would use ("list the customer names and cities", never "SELECT name, city"). Specifically:

- **Permitted:** naming the columns the existing gold query already returns.
- **Not permitted:** changing any gold SQL; reshaping a question to match what a model happened to
  return; removing a difficulty (a join, a NULL semantic, a threshold, a tie, a rounding rule, a
  sort). Where an item was hard because the SQL is hard, it is still exactly as hard.

**No `gold_sql` value was modified by this revision.** The manifest diff for W12 is 38 changed
lines, all of them `"question"`. That is checkable: `git diff` the manifest.

### Scope deliberately not taken

Three further ways this corpus is ill-posed were found during the audit and **left alone**, so that
the before/after comparison in the new report has exactly one cause:

1. **Row order.** Several golds carry an arbitrary `ORDER BY` (e.g. `ORDER BY id`) that the question
   never asks for; a candidate with the right rows in another order fails. 14 failures in the first
   run were `order_mismatch`. Fixing it would mean writing a sort into questions whose analyst never
   asked for one — a different and much weaker claim than "state what to return" — so it stays, and
   the report keeps quantifying it in the order-insensitive diagnostic.
2. **Rounding.** `library-m04`, `retail-h05` and others round in the gold (`ROUND(x, 2)`) where the
   question says nothing about precision, so an unrounded candidate is a `value_mismatch`.
3. **Row inclusion.** `library-m01` ("How many books do we have in each category?") does not say
   whether a category with zero books should appear; gold says no (inner join), a defensible reading
   says yes. Fixing this changes which *rows* are correct, i.e. the answer, not the output shape, so
   it was out of the rule above.

All three are named in the new report as known open defects rather than silently carried.

### Items revised (38 of 45)

#### `retail-e01`

- **before:** Which products have been discontinued?
- **after:** Which products have been discontinued? List each one's name, category and unit price.
- **why:** "Which products" names no projection; the gold's three columns were unstated. Names the columns the gold already returns; the filter (NULL-means-active) is untouched.
- **gold SQL changed:** no

#### `retail-e02`

- **before:** List the customers based in Texas.
- **after:** List the customers based in Texas, showing each customer's name and city.
- **why:** "List the customers" does not say which customer attributes. States the gold's two; the 'TX' vs 'Texas' encoding difficulty is deliberately left in.
- **gold SQL changed:** no

#### `retail-e03`

- **before:** What are our five most expensive products?
- **after:** What are our five most expensive products? Show the product name and its price.
- **why:** "Most expensive" implies price is wanted but never says so; sort, limit and tie-break are unchanged.
- **gold SQL changed:** no

#### `retail-e04`

- **before:** Which orders are still pending?
- **after:** Which orders are still pending? Give the order id and the date it was placed.
- **why:** "Which orders" identifies no columns; states the gold's identifier plus date.
- **gold SQL changed:** no

#### `retail-e05`

- **before:** Which customers signed up before June 2022?
- **after:** Which customers signed up before June 2022? Show their name and the date they signed up.
- **why:** Adds the projection only. Phrased as 'the date they signed up' so the column name is still not handed over (the item's stated point).
- **gold SQL changed:** no

#### `retail-m01`

- **before:** How many orders has each customer placed, including customers who haven't ordered anything?
- **after:** How many orders has each customer placed, including customers who haven't ordered anything? Show the customer id, name and order count.
- **why:** The count was specified, the customer identification was not (id, name, or both). LEFT-JOIN difficulty untouched.
- **gold SQL changed:** no

#### `retail-m03`

- **before:** Which product category brings in the most revenue from orders that were actually delivered?
- **after:** Which product category brings in the most revenue from orders that were actually delivered? Show the category and its revenue.
- **why:** Asked for a category but the gold also returns the revenue that justifies it; states that second column.
- **gold SQL changed:** no

#### `retail-m04`

- **before:** For each customer who has bought something, how many distinct products have they purchased?
- **after:** For each customer who has bought something, how many distinct products have they purchased? Show the customer id, name and the number of distinct products.
- **why:** The measure was specified, the customer columns were not.
- **gold SQL changed:** no

#### `retail-m05`

- **before:** Which products have sold at least 10 units in total?
- **after:** Which products have sold at least 10 units in total? Show the product id, name and total units sold.
- **why:** "Which products" names no columns and does not say the unit total is wanted back; the HAVING threshold is unchanged.
- **gold SQL changed:** no

#### `retail-h01`

- **before:** Which customers have never placed an order?
- **after:** Which customers have never placed an order? Give the customer id and name.
- **why:** Anti-join difficulty untouched; only the two identifying columns are now stated.
- **gold SQL changed:** no

#### `retail-h02`

- **before:** For every order, show how it ranks by recency (1 = most recent) among that same customer's orders.
- **after:** For every order, show the customer id, the order id, the order date, and how the order ranks by recency (1 = most recent) among that same customer's orders.
- **why:** The rank was specified, the three columns it is reported against were not; RANK-vs-ROW_NUMBER and the partitioning are untouched.
- **gold SQL changed:** no

#### `retail-h03`

- **before:** Which calendar month generated the most revenue from delivered orders?
- **after:** Which calendar month generated the most revenue from delivered orders? Show the month and its revenue.
- **why:** Asked for a month but the gold also returns the revenue behind it; states that column.
- **gold SQL changed:** no

#### `retail-h04`

- **before:** Among customers who have placed at least one order, which ones spent more than the average total spend for that group?
- **after:** Among customers who have placed at least one order, which ones spent more than the average total spend for that group? Show the customer id, name and total spend.
- **why:** "Which ones" names no columns and does not say the total is wanted back; the above-average CTE pattern is untouched.
- **gold SQL changed:** no

#### `retail-h05`

- **before:** For each product category, what share of total revenue does it represent?
- **after:** For each product category, show its revenue and what share of total revenue that represents.
- **why:** "What share" implies one column; the gold also returns the revenue the share is computed from. States both.
- **gold SQL changed:** no

#### `library-e01`

- **before:** Which members have a lifetime membership that never expires?
- **after:** Which members have a lifetime membership that never expires? List their names.
- **why:** "Which members" could be answered with any member attribute; the gold returns names only. The NULL-means-lifetime inference is untouched.
- **gold SQL changed:** no

#### `library-e02`

- **before:** Which books were published before 1990?
- **after:** Which books were published before 1990? Show the title and year of publication.
- **why:** "Which books" names no columns; states the gold's two.
- **gold SQL changed:** no

#### `library-e04`

- **before:** For which authors do we not know a birth year?
- **after:** For which authors do we not know a birth year? List their names.
- **why:** States that names alone are wanted; the NULL filter is untouched.
- **gold SQL changed:** no

#### `library-e05`

- **before:** Which loans are still out — not yet returned?
- **after:** Which loans are still out — not yet returned? Show the loan id, the book and member ids, and the date it is due.
- **why:** "Which loans" names none of the gold's four columns.
- **gold SQL changed:** no

#### `library-m02`

- **before:** Which authors have written more than one book in our collection?
- **after:** Which authors have written more than one book in our collection? Show the author's name and how many books.
- **why:** The threshold was specified, the returned count and the author column were not; the many-to-many join is untouched.
- **gold SQL changed:** no

#### `library-m03`

- **before:** For members who joined in 2020, is their membership still active as of the start of 2024?
- **after:** For members who joined in 2020, show their name, the date they joined, and their membership status as of the start of 2024 — 'lifetime', 'active' or 'expired'.
- **why:** Names the three output columns and the status vocabulary, without saying which case maps to which — the NULL-is-a-third-case inference, the item's stated point, still has to be made by the model.
- **gold SQL changed:** no

#### `library-m05`

- **before:** Which books have been checked out more than five times?
- **after:** Which books have been checked out more than five times? Show the title and the number of loans.
- **why:** The threshold was specified, the returned count and the book column were not.
- **gold SQL changed:** no

#### `library-h01`

- **before:** Which members have never checked out a single book?
- **after:** Which members have never checked out a single book? Give the member id and name.
- **why:** Anti-join difficulty untouched; only the two identifying columns are now stated.
- **gold SQL changed:** no

#### `library-h02`

- **before:** For each member, what's their most recent checkout date, alongside their all-time loan count?
- **after:** For each member, show their member id, their most recent checkout date, and their all-time loan count.
- **why:** Both measures were already specified; only the member identifier (id, not name) was not. Double-window difficulty untouched.
- **gold SQL changed:** no

#### `library-h03`

- **before:** List every subcategory that falls under Fiction, no matter how many levels deep.
- **after:** List every subcategory that falls under Fiction, no matter how many levels deep, showing each one's id and name.
- **why:** The recursive walk was specified, the two output columns were not.
- **gold SQL changed:** no

#### `library-h04`

- **before:** Which members currently have more than one book overdue (due before June 2024 and still not returned)?
- **after:** Which members currently have more than one book overdue (due before June 2024 and still not returned)? Show the member id and how many overdue books they have.
- **why:** The overdue definition and threshold are untouched; the member column and the returned count are now stated.
- **gold SQL changed:** no

#### `library-h05`

- **before:** For each category that has books, what fraction of its books were published in the last ten years covered by our catalog (2014-2023)?
- **after:** For each category that has books, show the category name, how many of its books were published in the last ten years covered by our catalog (2014-2023), how many books it has in total, and the fraction that is recent.
- **why:** "What fraction" implies one column; the gold returns four. States all four, leaving the correlated-subquery structure untouched.
- **gold SQL changed:** no

#### `events-e01`

- **before:** Which users are on the enterprise plan?
- **after:** Which users are on the enterprise plan? Show their username and country.
- **why:** "Which users" names no columns; states the gold's two.
- **gold SQL changed:** no

#### `events-e02`

- **before:** List all churn events, most recent first.
- **after:** List all churn events, most recent first, showing the event id, the user id and when it happened.
- **why:** The sort was specified, the three columns were not.
- **gold SQL changed:** no

#### `events-e03`

- **before:** Which users signed up in November 2023?
- **after:** Which users signed up in November 2023? Show the username and the date they signed up.
- **why:** "Which users" names no columns; states the gold's two.
- **gold SQL changed:** no

#### `events-e04`

- **before:** What are the ten most recent purchase events?
- **after:** What are the ten most recent purchase events? Show the event id, the user id, when it happened and the revenue.
- **why:** Filter, sort and limit were specified; the four columns were not.
- **gold SQL changed:** no

#### `events-m01`

- **before:** What's the total purchase revenue generated by each user?
- **after:** What's the total purchase revenue generated by each user? Show the user id and their total revenue.
- **why:** "Each user" left the identifier open (id or username). The cents-vs-dollars question is deliberately left open — under D13 that is a domain corrective's job, not the question's.
- **gold SQL changed:** no

#### `events-m03`

- **before:** Which users have made at least three purchases?
- **after:** Which users have made at least three purchases? Show the user id and their purchase count.
- **why:** The boundary-inclusive threshold is untouched; the identifier and the returned count are now stated.
- **gold SQL changed:** no

#### `events-h01`

- **before:** Which users have never triggered a single event?
- **after:** Which users have never triggered a single event? Give the user id and username.
- **why:** Anti-join difficulty untouched; only the two identifying columns are now stated.
- **gold SQL changed:** no

#### `events-h02`

- **before:** For each user, what was their single largest purchase?
- **after:** For each user, what was their single largest purchase? Show the user id, the event id, when it happened and the amount.
- **why:** States the four columns only. The tie at the maximum — RANK keeps both rows, ROW_NUMBER keeps one — is the item's documented difficulty and is deliberately NOT resolved.
- **gold SQL changed:** no

#### `events-h03`

- **before:** Show the running cumulative total of purchase revenue over time, across all users.
- **after:** Show the running cumulative total of purchase revenue over time, across all users — for each purchase, when it happened, its revenue and the running total.
- **why:** The running total was specified, the two columns it is reported against were not.
- **gold SQL changed:** no

#### `events-h04`

- **before:** Which signup month had the most new users, and how did it compare to the month before it?
- **after:** Which signup month had the most new users, and how many signed up in the month before it? Show the month, its signups and the previous month's signups.
- **why:** "How did it compare" left the shape open (a delta? a ratio?); states the gold's three columns. The LAG and top-1 pick are untouched.
- **gold SQL changed:** no

#### `events-h05`

- **before:** Which users made a purchase within 24 hours of their signup event?
- **after:** Which users made a purchase within 24 hours of their signup event? List their user ids.
- **why:** "Which users" could be answered with a username; states that the id alone is wanted. The 24-hour window arithmetic is untouched.
- **gold SQL changed:** no

#### `events-h06`

- **before:** Which plans have more than five distinct users who made at least one purchase?
- **after:** Which plans have more than five distinct users who made at least one purchase? Show the plan and how many such users it has.
- **why:** The threshold and de-duplication are untouched; the returned count is now stated.
- **gold SQL changed:** no

### Items audited and deliberately left unchanged (7 of 45)

A grouped aggregate names both its grouping key and its measure, so these questions already
determine their output shape. They were re-read under the same rule and not touched.

- **`events-m02`** — "How many events of each type" already determines (type, count).
- **`events-m04`** — "Total purchase revenue by country" already determines (country, revenue).
- **`events-m05`** — "Average purchase size for each plan tier" already determines (plan, average).
- **`library-e03`** — "What are the names of our top-level categories" already says names, and nothing else.
- **`library-m01`** — "How many books in each category" already determines (category, count). Left alone deliberately although it is ill-posed in a different way — whether a category with zero books should appear — because fixing that would change which rows are correct, not the output shape; recorded as a known open defect.
- **`library-m04`** — "On average how many copies per category" already determines (category, average). Its remaining failure mode is rounding, not projection; recorded as a known open defect.
- **`retail-m02`** — "Total revenue by product category" already determines (category, revenue): a grouped aggregate names both its grouping key and its measure.

### Honest uncertainty

Two edits are judgment calls rather than clear-cut, and are flagged so a reader can disagree:

- **`library-m03`** — the gold emits the literal status strings `'lifetime'`, `'active'`,
  `'expired'`, which no model can guess; the item was unscoreable as written. The rewrite names the
  three labels but deliberately does **not** say which condition maps to which, so the inference the
  item exists to test (a NULL expiry is a third case, not "expired") is still the model's to make.
  It does leak that there are three buckets, which is a small reduction in difficulty. Recorded
  rather than hidden.
- **`events-h02`** — the projection is now stated, but the item's documented ambiguity (a tie at the
  maximum: `RANK()` returns both rows, `ROW_NUMBER()` one) is untouched and the item is still
  expected to fail for that reason. Stating the columns makes the *remaining* failure attributable
  to the ambiguity design.md predicted, which is the point.
