# Nemotron vs Opus as the model inside Finrail

Measured 30 October 2026, on main at 47e23a8.

## The answer

On the `finance.reports.analyze` row picks, Nemotron 3 Super passed the code checks 34 times
out of 35. Opus 5.5 passed 35 out of 35. Nemotron's one miss picked the right row but the
wrong column, and the source-cell check refused it, so nothing wrong shipped. At list prices
a checked answer costs $0.0014 on Nemotron and $0.0245 on Opus as Finrail calls it, about 18
times more. Opus was faster: 3.9 s median against 6.1 s.

So yes, for this job the small open model holds up. In Finrail the model only picks a row ID
and copies a number, and code checks both. With 35 calls per arm, the two pass rates are
within sampling error of each other.

This does not show that harnesses like this never need a frontier model. It shows that this
one task, in one domain and under these checks, does not. See the caveats.

## Results

Analyze tasks only (7 tasks × 5 runs per arm). The screen call is reported separately below.

| | Nemotron 3 Super (Nebius) | Opus 5.5 (claude CLI) |
|---|---|---|
| Passed the code checks | **34 / 35** (97%) | **35 / 35** (100%) |
| 95% interval (Wilson) | 85% to 99.5% | 90% to 100% |
| Refused by | source-cell check, once | none |
| Latency, median / p90 | 6.1 s / 10.1 s | 3.9 s / 6.0 s |
| Input tokens per call (mean) | 1,469 | 5,275 (about 3,540 of it is the CLI's own prompt) |
| Output tokens per call (mean) | 1,016 (mostly reasoning) | 172 |
| Cost per call, list, no caching | $0.00135 | $0.0245 |
| **Cost per checked answer, list** | **$0.0014** | **$0.0245** |
| Cost per call, Finrail's prompt alone via the API | n/a | about $0.0104 |
| Cost per call as the CLI billed this run | $0.00135 | $0.0059 |

Nemotron is about 18 times cheaper per checked answer at list price, about 7.5 times cheaper
than Opus called directly with only Finrail's prompt, and about 4 times cheaper than this run's
CLI bill. The CLI bill is low only because five identical prompts in a row hit its one-hour
prompt cache (see the caveats).

### By task

| Task | Nemotron pass | Opus pass | Nemotron median s | Opus median s | Checked amount (USD m) |
|---|---|---|---|---|---|
| NVIDIA Q1 FY2027 free cash flow | 5/5 | 5/5 | 6.8 | 4.2 | 48,554 |
| NVIDIA Q2 FY2027 free cash flow | 5/5 | 5/5 | 7.0 | 4.6 | 21,341 |
| Anthropic S-1 revenue | 5/5 | 5/5 | 8.0 | 3.5 | 16,286 |
| Anthropic S-1 net loss | 5/5 | 5/5 | 5.3 | 3.0 | 80,748 |
| Anthropic S-1 operating cash flow | 5/5 | 5/5 | 3.1 | 3.3 | 3,929 |
| Anthropic S-1 capex | **4/5** | 5/5 | 4.4 | 3.2 | 482 |
| Anthropic S-1 method-rule operating result (M-1) | 5/5 | 5/5 | 4.7 | 5.6 | -567 |

Every passing run on both arms produced the same checked amount for its task.

### The one refusal

On run 1 of the S-1 capex task, Nemotron chose row `E6:L28`, which is the right row:

```
Purchases of property and equipment | (2) | (15) | (131) | (18) | (482)
```

It proposed 15, which is a value from another period. The archived cell under "Six Months
Ended June 30, / 2026" is 482. `check_and_compute` refused it with "Source-cell check refused
cash_purchases: the proposed value is not the selected period's archived source cell." No
answer came out of that run. The other four runs on the same excerpts proposed 482. This is
the failure the harness exists to catch, and it caught it.

### The screen call (`finance.screen`, bundled Northwind fixture)

Both arms scored 0 out of 5, and the model is not the cause. `_SCREEN_SYSTEM` asks for "the
assertion indices it supports or contradicts" but never names the fields. The code reads
`supports_assertions` and `contradicts_assertions`. Opus answered with `supports` and
`contradicts`. Nemotron answered with `supports`, or left the indices out. Either way the gate
saw no support and marked every finding `rejected`. Both arms quoted the sources word for word
(no quote was discarded). This needs a one-line prompt fix, after which the screen comparison
can be re-run. It is not counted in the results above.

| | Nemotron | Opus |
|---|---|---|
| Passed | 0/5 | 0/5 |
| Quotes not verbatim | 0 | 0 |
| Median latency | 6.8 s | 8.0 s |
| Cost per call, list | $0.0012 | $0.0315 |

## Method

- **One seam.** Every call went through `finrail.core.llm.call()` with the exact arguments
  `ReportAnalyze.execute()` builds: the same system prompt, the same JSON prompt, the same
  `retrieved_rows` context and the same `wants` list. The results were then judged by the
  unchanged `public_reports.check_and_compute`. The screen arm called the unchanged
  `_live_candidates` and `_classify`. For every task, the SHA-256 of system prompt plus prompt
  was identical across the two arms.
- **Engines are configuration only.** For Nemotron: `FINRAIL_ENGINE=nebius`, model
  `nvidia/nemotron-3-super-120b-a12b` on Token Factory, temperature 0, `response_format`
  json_object. For Opus: the default CLI engine with `FINRAIL_MODEL=claude-opus-5-5`. This is
  `claude -p --model claude-opus-5-5 --tools "" --strict-mcp-config` with Finrail's system
  prompt appended. No code path or runtime default was changed.
- **Retrieval held fixed.** Retrieval was captured once and both arms got identical excerpts.
  - NVIDIA Q1 and Q2: Vertex RAG `retrieveContexts`, top_k 8, query from the profile, captured
    30 Oct 2026. The archived cells were read from the hash-checked GCS object.
  - Anthropic S-1: the evidence recorded by live runs 53f78f23 (revenue query, which the
    revenue, net loss, operating cash flow and capex definitions share) and 52af21fa (the
    method-rule query). The cells were rebuilt by `source_cells()` from the archived S-1 bytes
    (SHA-256 `2fa24ae7…905a`). A sibling session found Vertex retrieval to be deterministic for
    these queries.
- **Isolation.** `FINRAIL_STATE` was a fresh directory, so no lessons were injected into
  either arm and nothing was written to the real ledger.
- **Runs.** 5 rounds per arm. Each round went through all 7 analyze tasks plus the screen
  call. The arms ran one after the other, not interleaved.
- **Latency** is wall clock around `llm.call()`, measured from the calling machine. For Opus
  that includes starting the CLI process (about 1 s). For Nemotron it is one HTTPS request.
- **Tokens** come from each engine's own usage report. For Opus, input is the sum of
  uncached, cache-read and cache-write tokens. A trivial CLI call ("reply ok") cost 3,544
  input tokens. That is the CLI's own prompt, which Nemotron never sees.

## Prices (checked 30 October 2026)

| Model | Input | Cached input | Output | Source |
|---|---|---|---|---|
| Nemotron 3 Super 120B A12B, Token Factory | $0.30 / M | $0.06 / M | $0.90 / M | [Token Factory model catalog](https://tokenfactory.nebius.com/model-catalog.md) |
| Claude Opus 5.5 | $4 / M | $0.20 / M read; $5 / M 5-minute write, $8 / M 1-hour write | $20 / M | [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) |

The "list, no caching" column prices every input token at the base rate for both arms. The
CLI's own reported cost matched the 1-hour cache-write rate exactly.

**Nemotron 3 Super leaves Token Factory serverless on 9 November 2026.** Nebius suggests
`nvidia/Nemotron-3.5-Super-VL`, at the same price
([deprecation notice](https://docs.tokenfactory.nebius.com/model-deprecations-november-2026)).
These results are for the model being retired. Re-run them on the replacement before relying
on it.

## Caveats

- **Small sample.** 35 analyze calls per arm. One refusal in 35 cannot separate the two
  models: the intervals overlap almost entirely. A 3% refusal rate for Nemotron is a plausible
  reading, and so is 0%.
- **One task type in one domain.** The model's whole job here is choosing a row ID and copying
  a number from that row. Code does the quoting, the period and unit checks and all the
  arithmetic. Tasks that need judgment are not tested: writing a memo, weighing conflicting
  evidence, or picking among close candidates with no code check.
- **Retrieval held fixed.** Both models saw the same good excerpts, and every required row was
  in them (the sibling eval found 7/7 recall at top_k 8). Neither was tested on noisier or
  thinner retrieval.
- **Few filings.** Two NVIDIA 10-Qs and one S-1. The tables are clean SEC HTML.
- **Opus via the CLI, not the raw API.** This is how Finrail actually runs Opus. It adds about
  3,540 tokens of CLI prompt and about 1 s of process start to every call. The CLI exposes no
  temperature and this harness set no effort, so Opus ran at the CLI defaults. The "as billed"
  cost benefited from five identical prompts in a row hitting the cache. Real traffic varies
  more, so the list-price column is the fairer comparison.
- **Nemotron at temperature 0 still varied.** The same excerpts gave 15 once and 482 four
  times. Refusals should be read as a rate, not as all-or-nothing.
- **Screen arm not informative** until its prompt names its fields (see above).

## Recommendation

Keep Nemotron as the analyze engine. It passed 34 of 35 checks, against Opus's 35 of 35, at
about 1/18 of the list cost per checked answer. Its one wrong pick was refused by code,
and the person saw a refusal, never a wrong figure. The price is about 2 s more median latency
(about 4 s more at p90) and an occasional retry.

Say it as narrowly as that: "for picking source rows under code checks, a 12B-active open
model matched Opus within sampling error, at about 1/18 of the cost." Do not say
"harnesses like this don't need frontier models". This test covered only the part of the work
where the harness leaves the model the least to decide.

Before relying on it after 9 November: re-run these 35 calls on `Nemotron-3.5-Super-VL`, and
fix the screen prompt's field names and re-run that arm.

## Reproduce

The harness (`compare.py`, `summarize.py`, `operator-run.sh`) is kept outside the repo, in the
shared side-session notes folder under `model-comparison/`. `operator-run.sh` reads the Vertex
token and the Nebius key into the environment and never prints them, then captures the NVIDIA
retrieval and runs the Nemotron arm. `compare.py run opus 5` needs only an authenticated
`claude` CLI.

## Appendix: every call

| arm | task | run | result | seconds | input tokens | output tokens | amount |
|---|---|---|---|---|---|---|---|
| nemotron | ANTH-S1-2026/capex | 1 | refused | 9.6 | 1600 | 1801 |  |
| nemotron | ANTH-S1-2026/capex | 2 | pass | 4.4 | 1600 | 749 | 482 |
| nemotron | ANTH-S1-2026/capex | 3 | pass | 4.8 | 1600 | 863 | 482 |
| nemotron | ANTH-S1-2026/capex | 4 | pass | 4.1 | 1600 | 619 | 482 |
| nemotron | ANTH-S1-2026/capex | 5 | pass | 4.2 | 1600 | 896 | 482 |
| nemotron | ANTH-S1-2026/house_operating_income | 1 | pass | 3.5 | 2082 | 753 | -567 |
| nemotron | ANTH-S1-2026/house_operating_income | 2 | pass | 9.4 | 2082 | 1376 | -567 |
| nemotron | ANTH-S1-2026/house_operating_income | 3 | pass | 10.1 | 2082 | 1864 | -567 |
| nemotron | ANTH-S1-2026/house_operating_income | 4 | pass | 4.1 | 2082 | 624 | -567 |
| nemotron | ANTH-S1-2026/house_operating_income | 5 | pass | 4.7 | 2082 | 694 | -567 |
| nemotron | ANTH-S1-2026/net_loss | 1 | pass | 4.0 | 1592 | 843 | 80748 |
| nemotron | ANTH-S1-2026/net_loss | 2 | pass | 5.3 | 1592 | 872 | 80748 |
| nemotron | ANTH-S1-2026/net_loss | 3 | pass | 9.5 | 1592 | 1774 | 80748 |
| nemotron | ANTH-S1-2026/net_loss | 4 | pass | 6.4 | 1592 | 1071 | 80748 |
| nemotron | ANTH-S1-2026/net_loss | 5 | pass | 4.8 | 1592 | 709 | 80748 |
| nemotron | ANTH-S1-2026/operating_cash_flow | 1 | pass | 3.0 | 1606 | 488 | 3929 |
| nemotron | ANTH-S1-2026/operating_cash_flow | 2 | pass | 2.9 | 1606 | 483 | 3929 |
| nemotron | ANTH-S1-2026/operating_cash_flow | 3 | pass | 3.9 | 1606 | 603 | 3929 |
| nemotron | ANTH-S1-2026/operating_cash_flow | 4 | pass | 3.1 | 1606 | 479 | 3929 |
| nemotron | ANTH-S1-2026/operating_cash_flow | 5 | pass | 3.9 | 1606 | 512 | 3929 |
| nemotron | ANTH-S1-2026/revenue | 1 | pass | 7.1 | 1592 | 1200 | 16286 |
| nemotron | ANTH-S1-2026/revenue | 2 | pass | 10.9 | 1592 | 1723 | 16286 |
| nemotron | ANTH-S1-2026/revenue | 3 | pass | 11.3 | 1592 | 1979 | 16286 |
| nemotron | ANTH-S1-2026/revenue | 4 | pass | 8.0 | 1592 | 847 | 16286 |
| nemotron | ANTH-S1-2026/revenue | 5 | pass | 4.4 | 1592 | 691 | 16286 |
| nemotron | NVDA-Q1-FY2027/issuer_fcf | 1 | pass | 6.1 | 748 | 1030 | 48554 |
| nemotron | NVDA-Q1-FY2027/issuer_fcf | 2 | pass | 6.5 | 748 | 895 | 48554 |
| nemotron | NVDA-Q1-FY2027/issuer_fcf | 3 | pass | 6.9 | 748 | 1181 | 48554 |
| nemotron | NVDA-Q1-FY2027/issuer_fcf | 4 | pass | 10.5 | 748 | 1061 | 48554 |
| nemotron | NVDA-Q1-FY2027/issuer_fcf | 5 | pass | 6.8 | 748 | 1189 | 48554 |
| nemotron | NVDA-Q2-FY2027/issuer_fcf | 1 | pass | 8.3 | 1064 | 1281 | 21341 |
| nemotron | NVDA-Q2-FY2027/issuer_fcf | 2 | pass | 6.1 | 1064 | 1153 | 21341 |
| nemotron | NVDA-Q2-FY2027/issuer_fcf | 3 | pass | 7.6 | 1064 | 1101 | 21341 |
| nemotron | NVDA-Q2-FY2027/issuer_fcf | 4 | pass | 7.0 | 1064 | 1287 | 21341 |
| nemotron | NVDA-Q2-FY2027/issuer_fcf | 5 | pass | 5.8 | 1064 | 857 | 21341 |
| nemotron | screen/northwind | 1 | refused | 6.2 | 404 | 1080 |  |
| nemotron | screen/northwind | 2 | refused | 6.6 | 404 | 1340 |  |
| nemotron | screen/northwind | 3 | refused | 6.8 | 404 | 1076 |  |
| nemotron | screen/northwind | 4 | refused | 6.9 | 404 | 1136 |  |
| nemotron | screen/northwind | 5 | refused | 7.4 | 404 | 1175 |  |
| opus | ANTH-S1-2026/capex | 1 | pass | 3.1 | 5377 | 96 | 482 |
| opus | ANTH-S1-2026/capex | 2 | pass | 3.2 | 5377 | 96 | 482 |
| opus | ANTH-S1-2026/capex | 3 | pass | 3.3 | 5377 | 96 | 482 |
| opus | ANTH-S1-2026/capex | 4 | pass | 3.1 | 5377 | 96 | 482 |
| opus | ANTH-S1-2026/capex | 5 | pass | 3.4 | 5377 | 96 | 482 |
| opus | ANTH-S1-2026/house_operating_income | 1 | pass | 5.6 | 6052 | 295 | -567 |
| opus | ANTH-S1-2026/house_operating_income | 2 | pass | 5.3 | 6052 | 253 | -567 |
| opus | ANTH-S1-2026/house_operating_income | 3 | pass | 5.7 | 6052 | 258 | -567 |
| opus | ANTH-S1-2026/house_operating_income | 4 | pass | 5.0 | 6052 | 220 | -567 |
| opus | ANTH-S1-2026/house_operating_income | 5 | pass | 6.1 | 6052 | 224 | -567 |
| opus | ANTH-S1-2026/net_loss | 1 | pass | 6.2 | 5371 | 97 | 80748 |
| opus | ANTH-S1-2026/net_loss | 2 | pass | 3.4 | 5371 | 97 | 80748 |
| opus | ANTH-S1-2026/net_loss | 3 | pass | 2.9 | 5371 | 97 | 80748 |
| opus | ANTH-S1-2026/net_loss | 4 | pass | 2.9 | 5371 | 97 | 80748 |
| opus | ANTH-S1-2026/net_loss | 5 | pass | 3.0 | 5371 | 97 | 80748 |
| opus | ANTH-S1-2026/operating_cash_flow | 1 | pass | 3.1 | 5390 | 105 | 3929 |
| opus | ANTH-S1-2026/operating_cash_flow | 2 | pass | 4.7 | 5390 | 133 | 3929 |
| opus | ANTH-S1-2026/operating_cash_flow | 3 | pass | 3.2 | 5390 | 105 | 3929 |
| opus | ANTH-S1-2026/operating_cash_flow | 4 | pass | 3.3 | 5390 | 133 | 3929 |
| opus | ANTH-S1-2026/operating_cash_flow | 5 | pass | 3.9 | 5390 | 105 | 3929 |
| opus | ANTH-S1-2026/revenue | 1 | pass | 3.5 | 5369 | 95 | 16286 |
| opus | ANTH-S1-2026/revenue | 2 | pass | 3.8 | 5369 | 222 | 16286 |
| opus | ANTH-S1-2026/revenue | 3 | pass | 6.0 | 5369 | 95 | 16286 |
| opus | ANTH-S1-2026/revenue | 4 | pass | 3.2 | 5369 | 95 | 16286 |
| opus | ANTH-S1-2026/revenue | 5 | pass | 3.1 | 5369 | 139 | 16286 |
| opus | NVDA-Q1-FY2027/issuer_fcf | 1 | pass | 5.6 | 4513 | 282 | 48554 |
| opus | NVDA-Q1-FY2027/issuer_fcf | 2 | pass | 4.6 | 4513 | 245 | 48554 |
| opus | NVDA-Q1-FY2027/issuer_fcf | 3 | pass | 3.9 | 4513 | 254 | 48554 |
| opus | NVDA-Q1-FY2027/issuer_fcf | 4 | pass | 4.2 | 4513 | 280 | 48554 |
| opus | NVDA-Q1-FY2027/issuer_fcf | 5 | pass | 4.0 | 4513 | 277 | 48554 |
| opus | NVDA-Q2-FY2027/issuer_fcf | 1 | pass | 5.7 | 4854 | 266 | 21341 |
| opus | NVDA-Q2-FY2027/issuer_fcf | 2 | pass | 4.5 | 4854 | 281 | 21341 |
| opus | NVDA-Q2-FY2027/issuer_fcf | 3 | pass | 7.1 | 4854 | 173 | 21341 |
| opus | NVDA-Q2-FY2027/issuer_fcf | 4 | pass | 4.6 | 4854 | 263 | 21341 |
| opus | NVDA-Q2-FY2027/issuer_fcf | 5 | pass | 4.3 | 4854 | 257 | 21341 |
| opus | screen/northwind | 1 | refused | 10.9 | 4136 | 712 |  |
| opus | screen/northwind | 2 | refused | 7.2 | 4136 | 524 |  |
| opus | screen/northwind | 3 | refused | 8.0 | 4136 | 695 |  |
| opus | screen/northwind | 4 | refused | 6.9 | 4136 | 693 |  |
| opus | screen/northwind | 5 | refused | 7.5 | 4136 | 707 |  |
