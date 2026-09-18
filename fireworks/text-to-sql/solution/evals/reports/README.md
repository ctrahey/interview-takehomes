# Eval reports

Committed evidence for design §7 / D3 / D4 / D11. The evidence is a file in the repo, not a
screenshot in an email.

## Start here

**[`run-20260918-161313.md`](run-20260918-161313.md)** — the live run, 53 corpus items × 4 arms
(`kimi-k2p7-code` and `muse-glimmer-30b`, each with the repair loop off and on), 267 live model
turns against the Fireworks API on 2026-09-18.

Read the headline table, then the two paragraphs under "Repair loop: off vs on". The short version:

- The repair loop **never fired** on the primary model across 53 items, so the accuracy difference
  between its two arms is sampling variance, not a loop effect, and the report says so in those
  words. That was predicted (W1 finding #6) and it is reported as a result, not hidden.
- On the weak model the loop is the difference between **0 usable queries out of 53** and a working
  component. That is D11's claim — scaffolding value scales inversely with model strength —
  measured rather than asserted.
- 44% of all scored failures are `column_count_mismatch`: the model answered the question with a
  different set of columns than the gold query. That says as much about the corpus's
  under-specified questions as about the models, and the report argues the corpus is what should
  change first.

## A second run of the identical configuration, and what it costs the headline

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

## Reproducing

```sh
make check UV=$HOME/.local/bin/uv        # offline: harness unit tests, no API key needed
make eval  UV=$HOME/.local/bin/uv        # live: a full run like the ones above

python -m evals.harness run --arms primary/loop_on --limit 10   # a cheap partial run
python -m evals.harness smoke                                   # re-probe the weak-arm candidates
python -m evals.harness render evals/reports/<run-id>.json      # re-score + re-render, zero API calls
```

`render` is the one to know about: it re-derives every score from the recorded candidate SQL, so a
scorer fix or a wording change is applied to the *same* measurements. Getting a nicer number is
never the cheap path.
