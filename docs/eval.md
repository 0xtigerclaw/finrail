# Evaluation: re-running the accuracy claim

`docs/model-comparison.md` says Nemotron 3.5 passed 70 of 70 analyze picks, Nemotron 3 67 of
70 and Opus 35 of 35. The scripts behind those numbers were kept outside the repo, so a reader
could check the claim but not run it. `finrail/eval` puts the loop in the repo:

| command | what it does |
|---|---|
| `python -m finrail.eval validate <cases.json>` | checks a case file and lists its cases |
| `python -m finrail.eval run <cases.json> --engine <name> --runs N --out <dir>` | dispatches every case N times through `dispatch_core.dispatch`, reads each model call from the ledger, writes `results.jsonl` and `summary.json` |
| `python -m finrail.eval compare <new summary> <baseline>` | prints the deltas; exits 1 when the pass rate drops below the baseline's 95% interval or a wrong figure shipped |
| `python -m finrail.eval report <summary>...` | renders the Results and By task tables of `docs/model-comparison.md`, one column per summary |

Nothing in the package changes a check, a prompt or a capability. It asks the same question
many times and writes down what the system did.

## Quick start, no key, no network

```bash
python -m finrail.eval run finrail/eval/cases/model_comparison_7.json --engine stub --runs 2 --out eval-out/stub --prices nvidia/nemotron-3-super-120b-a12b
```

```bash
python -m finrail.eval compare eval-out/stub/summary.json finrail/eval/baselines/nemotron35_32k.json
```

```bash
python -m finrail.eval report finrail/eval/baselines/nemotron3.json finrail/eval/baselines/nemotron35_32k.json finrail/eval/baselines/opus55.json eval-out/stub/summary.json
```

The stub engine answers from `finrail/eval/cases/stub_answers.json` over a local HTTP server
that speaks the same chat-completions shape Token Factory does, so every call is recorded in
the ledger as a real `model_call` event with usage. The filings are synthetic statements with
the documented figures at the table positions the profiles declare; the catalog's SHA-256 is
rewritten to the synthetic bytes for the run. A stub pass proves the harness's checks and
bookkeeping. It does not prove what the live filings say. That needs a live run.

## A live run

```bash
FINRAIL_RAG_CORPUS=... NEBIUS_API_KEY=... python -m finrail.eval run finrail/eval/cases/model_comparison_7.json --engine nebius --runs 10 --out eval-out/nemotron35
```

`--engine` takes any `FINRAIL_ENGINE` name (`nebius`, `meta`, `claude-cli`); `--model` sets
`FINRAIL_MODEL` and `--max-tokens` sets `FINRAIL_ENGINE_MAX_TOKENS`. The run needs what a
live dispatch needs: the Vertex corpus and the archive, and a key or a signed-in CLI. State is
isolated under `<out>/state`, so no lesson is injected into a prompt and nothing is written to a
real ledger. Exit status is 0 when every call met its expected outcome, 1 otherwise.

A live `run` has not been made with this package yet. The four baselines are transcriptions.

## Case files

A case names a filing, a capability, its parameters and what must come out:

```json
{"id": "anth-s1-net-loss", "title": "Anthropic S-1 net loss",
 "report_id": "ANTH-S1-2026", "capability": "finance.reports.analyze",
 "params": {"definition": "net_loss"},
 "expect": {"outcome": "figure", "amount": "80748"},
 "tags": ["analyze", "anthropic", "passing"], "draft": false,
 "source": "docs/model-comparison.md, By task table: 80,748"}
```

`expect.outcome` is `passed` (an answer came out), `figure` (and its amount is this, in USD
millions as a string), or `refused` with a `reason` substring or an error `code`
(`EMPTY_RESPONSE`, `SCHEMA_INVALID`, `CONTRACT_VIOLATION`, ...). `inject` asks for a tamper on
the model's answer at the seam, before the code checks: `other_column`, `invented_row`,
`drop_input` or `wrong_report`. That is how an adversarial case runs on a live engine, whose
answers cannot be chosen; the same checks that refuse a model's miss must refuse the tamper.
`draft: true` marks an expectation not grounded in a documented figure. Drafts run and are
reported, and back no headline claim. The validator refuses a malformed case by id and field.

| file | cases | what |
|---|---|---|
| `cases/model_comparison_7.json` | 7 | the seven analyze tasks of `docs/model-comparison.md`, every amount the one that document prints |
| `cases/benchmark_36.json` | 36 | the benchmark the README promises (below) |
| `cases/search_2.json` | 2 | `finance.reports.search` smoke cases, both drafts: no expected quotes are documented |

### The 36-case benchmark

Ten passing cases, one per supported definition on NVIDIA Q1 FY2027, NVIDIA Q2 FY2027 and
Anthropic's S-1, and 26 adversarial ones:

| group | count | expected refusal |
|---|---|---|
| passing figure | 10 | none: NVIDIA issuer and simple FCF for Q1 and Q2; S-1 revenue, net loss, operating cash flow, capex, simple FCF, method-rule operating result |
| another column of the same row | 10 | `Source-cell check refused` (the miss Nemotron 3 made 3 times in 70) |
| invented row ID | 3 | `not present in the retrieved evidence` |
| dropped input | 6 | `missing required inputs` |
| wrong report in the answer | 3 | `did not match the selected report` |
| `adjusted_operating_income` under M-1 | 1 | `M-1`, refused by the plan before any model call |
| unsupported definition, unknown report | 3 | refused by the plan before any model call |

Three figures are drafts: `nvda-q1-fy2027-simple-fcf` (48,587), `nvda-q2-fy2027-simple-fcf`
(21,400) and `anth-s1-simple-fcf` (3,447). Each is arithmetic on documented lines, and no
document prints it. The README's "36-case benchmark has not been run" still holds for a live
engine: the set has been run on the stub only.

## What a run records

`results.jsonl` has one line per call: case, run number, outcome (`passed`, `refused`,
`cut_off`, `schema_invalid`, `engine_error`, `failed`, `wrong_figure`, `error`), whether the
expected outcome was met, the error code and message, the amount, the run id, wall-clock
latency around the dispatch, and from the ledger's `model_call` events the number of model
calls, the model, input and output tokens (fresh plus cached), the model's own duration, the
cost the engine reported (the CLI reports one; HTTP engines do not) and the list-price cost
from `finrail/eval/stats.py` (`--prices <model>` prices a stub run at a real model's rates).

`summary.json` has the pass rate (expected outcome met over calls) with its 95% Wilson
interval, the outcome counts, how many figure cases passed and how many adversarial cases were
refused, median and p90 latency, mean tokens, cost per call and per checked answer (total list
cost over passing answers), a per-case table, the draft ids, and `provenance`: `run`, `stub` or
`transcribed`, with a note.

## Baselines

`finrail/eval/baselines/` holds one summary per engine arm: `nemotron3`, `nemotron35_32k`,
`nemotron35_8k` and `opus55`. All four are **transcribed from `docs/model-comparison.md`**,
not re-run; their `provenance.kind` is `transcribed` and a report built from them says so. The
Wilson intervals recompute to the document's (88% to 99%, 95% to 100%, 90% to 100%, 86% to
98%). `compare` treats a new pass rate below the baseline's lower bound as a regression, and a
wrong figure shipped as a regression at any rate. Replace a transcription with a measured
baseline by copying a live run's `summary.json` over it.

## Regenerating the comparison tables

```bash
python -m finrail.eval report finrail/eval/baselines/nemotron3.json finrail/eval/baselines/nemotron35_32k.json finrail/eval/baselines/opus55.json finrail/eval/baselines/nemotron35_8k.json --out eval-out/model-comparison-tables.md
```

The Results and By task tables come out in the shape `docs/model-comparison.md` uses, with two
rows that document did not need: "Expected outcome met" (the rate the interval is on, which for
the seven passing tasks is the pass count) and "Adversarial picks refused" (shown when a summary
has adversarial cases). Every report ends with the draft cases and a provenance note that says
when no live engine run was made.

## Limits

- The stub's latency means nothing; its tokens are the recorded counts, not measured ones.
- Retrieval is held fixed by the stub. On a live run it is live, so a miss may be retrieval's.
- The tamper is applied to the model's answer. It tests the checks, not the model.
- Only `finance.reports.analyze` and `finance.reports.search` are covered. `finance.screen`
  is not: the document found a prompt fault there that has to be fixed before measuring it.
