# Eval reports

Committed evidence for design §7 / D3 / D4 / D11. The evidence is a file in the repo, not a
screenshot in an email.

## Start here

**[`run-20260918-164917.md`](run-20260918-164917.md)** — the current live run, 53 corpus items × 4
arms (`kimi-k2p7-code` and `muse-glimmer-30b`, each with the repair loop off and on), 266 live
model turns against the Fireworks API on 2026-09-18, **after the W12 corpus revision**.

Read the headline table, then "The corpus was revised between these two runs", then the two
paragraphs under "Repair loop: off vs on". The short version:

- The repair loop **never fired** on the primary model across 53 items, so the accuracy difference
  between its two arms is sampling variance, not a loop effect, and the report says so in those
  words. That was predicted (W1 finding #6) and it is reported as a result, not hidden. In this run
  that difference is *negative*, which is the same statement about noise seen from the other side.
- On the weak model the loop is the difference between **0 usable queries out of 53** and a working
  component. That is D11's claim — scaffolding value scales inversely with model strength —
  measured rather than asserted.
- **The instrument was edited between the two runs, and the report says so before it says anything
  else.** 38 of 45 gold questions were rewritten to state what to return; no gold SQL changed. Every
  edit is logged with its before/after and a justification in
  [`../corpus/REVISIONS.md`](../corpus/REVISIONS.md). `column_count_mismatch` went from 71 failures
  across the four arms to 1.
- The dominant failure is now `order_mismatch` (36% of scored failures) — the same class of defect
  in the *sort* rather than the projection, named in `REVISIONS.md` as deliberately not fixed in
  this change so that the before/after above has exactly one cause.

## The run this one is compared against

[`run-20260918-161313.md`](run-20260918-161313.md) is the first live 4-arm run, on the corpus as
originally authored. **It is kept verbatim, not overwritten**, because it is the evidence that the
defect was real: 44% of all its scored failures are `column_count_mismatch`, the model answering
the question with a different column list than an unstated gold projection. Its diagnosis section
is what W12 acted on.

The before/after table in the new report re-scores *that run's recorded candidates* with today's
scorer, which is why its strict accuracy column reproduces the older report's numbers exactly —
scoring never reads the question, only the gold SQL and the candidate, so identical numbers are a
check that no gold query was touched by the revision.

### Before and after

| arm | before (`161313`) | after (`164917`) | Δ | `column_count_mismatch` before → after |
|---|---|---|---|---|
| `primary/loop_off` | 15.6% (7/45) | 51.1% (23/45) | +35.6 pp | 26 → 0 |
| `primary/loop_on` | 28.9% (13/45) | 46.7% (21/45) | +17.8 pp | 22 → 0 |
| `weak/loop_off` | 0.0% (0/45) | 0.0% (0/45) | 0 | 0 → 0 |
| `weak/loop_on` | 15.6% (7/45) | 37.8% (17/45) | +22.2 pp | 23 → 1 |

Read those deltas against the ~9 pp noise floor measured in the next section: three of the four
clear it comfortably, and `weak/loop_off` did not move at all because its failure is upstream of
the corpus (it cannot emit a valid response envelope, so no question wording could help it). The
deltas are the *combined* effect of the corpus revision and run-to-run variance; they are not a
controlled measurement of the edit alone, and the report says so in the same place it shows them.

## A second run of the identical configuration, and what it costs the headline (pre-revision)

[`run-20260918-161201.md`](run-20260918-161201.md) is a complete second execution of the same 53
items across the same 4 arms, started one minute before the run above. Both are scored by the
current scorer (`python -m evals.harness render <run>.json` re-derives the scores offline, so the
two are directly comparable). Keeping it is more useful than tidying it away:

| arm | run `161201` | run `161313` | spread |
|---|---|---|---|
| `primary/loop_off` | 11.1% (5/45) | 15.6% (7/45) | 4.4 pp — 2 items |
| `primary/loop_on` | 20.0% (9/45) | 28.9% (13/45) | 8.9 pp — 4 items |
| `weak/loop_off` | 0.0% (0/45) | 0.0% (0/45) | 0 |
| `weak/loop_on` | 17.8% (8/45) | 15.6% (7/45) | 2.2 pp — 1 item |

**Temperature is 0.0 on every call and the numbers still move by up to 4 items.** These are large
MoE models served at scale; identical inputs do not guarantee identical outputs. The practical rule
this sets for reading either report: on a 45-item gold set, a single-run difference smaller than
roughly 9 pp is not a finding.

That rule bites the headline. `primary/loop_on` beat `primary/loop_off` in **both** runs (+8.9 and
+13.3 pp) — but the repair loop fired **zero** times in both, so it cannot be the cause. Two
same-direction results is what a fair coin does one time in four, and the arms always run in the
same order, so ordering effects are not excluded either. The right conclusion is the boring one: on
this model there is no measured loop effect, and anyone quoting that gap as one is reading noise.

The `weak/loop_on` delta is a different kind of number, and it survives the same test: it rests on
53 recorded validator rejections with the rejected candidate and the reason stored per item, not on
a difference between two totals.

Run `161201` also contains the only transport failure of either run — a `429` on `retail-m01` that
outlived its retries. It is recorded as a failed item carrying the exception text, counted in the
denominator, and it did not abort the run. That is the resilience requirement, demonstrated rather
than claimed.

## Files

| file | what it is |
|---|---|
| `run-<timestamp>.md` | the human-readable report for one run |
| `run-<timestamp>.json` | every item of that run: each attempt's candidate SQL, validator verdict, tokens, latency, score |
| `weak-model-smoke.json` | the 5-item probe that picked the weak arm by measurement (D11), not by name |

No report file is ever overwritten or deleted. A corpus revision produces a *new* run beside the
old one, and the old one keeps the numbers that justified the revision.

## Reproducing

```sh
make check UV=$HOME/.local/bin/uv        # offline: harness unit tests, no API key needed
make eval  UV=$HOME/.local/bin/uv        # live: a full run like the ones above

python -m evals.harness run --arms primary/loop_on --limit 10   # a cheap partial run
python -m evals.harness smoke                                   # re-probe the weak-arm candidates
python -m evals.harness render evals/reports/<run-id>.json      # re-score + re-render, zero API calls

# the run above, with its before/after section against the pre-revision run:
python -m evals.harness run --baseline evals/reports/run-20260918-161313.json
```

`render` is the one to know about: it re-derives every score from the recorded candidate SQL, so a
scorer fix or a wording change is applied to the *same* measurements. Getting a nicer number is
never the cheap path.
