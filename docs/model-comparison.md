# Nemotron vs Opus as the model inside Finrail

Measured 30 October 2026, on main at 47e23a8. There are three models: Nemotron 3 Super (the
current engine), Nemotron 3.5 Super VL (its replacement on Token Factory from 9 November) and
Claude Opus 5.5. Nemotron 3.5 was run twice: once with Finrail's reply cap as it is today,
and once with that cap raised.

Report page with charts: https://claude.ai/artifact/L9hZA9YH3TYJpEDN9y412C

## The answer

On the `finance.reports.analyze` row picks, all three models are accurate enough. The 95%
intervals overlap:

- Nemotron 3 passed the code checks 67 times out of 70.
- Nemotron 3.5 passed 70 out of 70.
- Opus 5.5 passed 35 out of 35.

No wrong figure got out on any model. Nemotron 3's 3 misses each read the right row from
another period's column, and the source-cell check refused all three.

Nemotron 3.5 needs more room to think than Finrail allows today. Finrail caps every HTTP-engine
reply at 8,192 tokens, and the thinking counts toward that cap.

| Nemotron 3.5 reply cap | Passed | Cut off, no answer |
|---|---|---|
| 8,192 tokens (Finrail today) | 66/70 | 4 |
| 32,768 tokens | 70/70 | 0 |

At the raised cap, 7 of 70 replies needed more than 8,192 tokens, the longest 11,934. The cap
is a Finrail setting (`max_tokens` in `_http_call`), not a Nebius usage limit.

Cost per checked answer at list price:

| Model | Per checked answer | Compared with Nemotron 3 |
|---|---|---|
| Nemotron 3 | $0.0014 | 1× |
| Nemotron 3.5 (32,768 cap) | $0.0044 | 3.1× |
| Opus as Finrail calls it | $0.0245 | 17× |

Opus is the fastest: 3.9 s median, against 5.3 s for Nemotron 3 and 12.5 s for Nemotron 3.5.

So for this job a small open model holds up. In Finrail the model only picks a row ID and
copies a number, and code checks both. The test does not show that harnesses like this never
need a frontier model. It shows that this one task, in one domain and under these checks,
does not.

## Results

Analyze tasks only: 7 tasks, 10 runs per Nemotron arm and 5 for Opus. The screen call is
reported separately below.

| | Nemotron 3 Super | Nemotron 3.5 Super VL (32,768 cap) | Opus 5.5 | Nemotron 3.5 (8,192 cap) |
|---|---|---|---|---|
| Passed the code checks | **67 / 70** (96%) | **70 / 70** (100%) | **35 / 35** (100%) | 66 / 70 (94%) |
| 95% interval (Wilson) | 88% to 99% | 95% to 100% | 90% to 100% | 86% to 98% |
| Refused by the checks | 3 | 0 | 0 | 0 |
| Cut off by the reply cap | 0 | 0 | 0 | 4 |
| Wrong figure shipped | 0 | 0 | 0 | 0 |
| Latency, median / p90 | 5.3 s / 9.6 s | 12.5 s / 26.3 s | 3.9 s / 6.0 s | 8.8 s / 16.0 s |
| Input tokens per call (mean) | 1,469 | 1,469 | 5,275 (about 3,540 is the CLI's own prompt) | 1,469 |
| Output tokens per call (mean) | 1,050 | 4,433 | 172 | 3,829 |
| Cost per call, list, no caching | $0.00139 | $0.00443 | $0.02454 | $0.00389 |
| **Cost per checked answer, list** | **$0.00145** | **$0.00443** | **$0.02454** | $0.00412 |
| Per 1,000 checked answers | $1.45 | $4.43 | $24.54 | $4.12 |

Opus has two other cost views:

- **Direct API, Finrail's prompt only:** about $0.0104 a call. That is still 7× Nemotron 3.
- **As the CLI billed this run:** $0.0059 a call. That is low only because five identical
  prompts in a row hit its one-hour cache.

The whole test cost $1.54 at list price.

### Why the cost gap is this size

Per token, Opus input costs 13× Nemotron's and Opus output costs 22×. Opus sends more input,
mostly the CLI's own prompt, but writes far less: 172 output tokens a call. Both Nemotron
models reason before answering, and that reasoning is billed as output. Nemotron 3.5 reasons
about 4.2× longer than Nemotron 3, which is why it costs about 3× as much and is slower.

### By task

| Task | Nemotron 3 | Nemotron 3.5 (32,768 cap) | Opus 5.5 | Nemotron 3.5 (8,192 cap) | Checked amount (USD m) |
|---|---|---|---|---|---|
| NVIDIA Q1 FY2027 free cash flow | 10/10 · 6.3 s | 10/10 · 19.0 s | 5/5 · 4.2 s | **9/10 · 14.1 s** | 48,554 |
| NVIDIA Q2 FY2027 free cash flow | 10/10 · 6.3 s | 10/10 · 14.5 s | 5/5 · 4.6 s | 10/10 · 9.7 s | 21,341 |
| Anthropic S-1 revenue | 10/10 · 8.3 s | 10/10 · 12.1 s | 5/5 · 3.5 s | 10/10 · 8.9 s | 16,286 |
| Anthropic S-1 net loss | **8/10 · 5.2 s** | 10/10 · 13.1 s | 5/5 · 3.0 s | **8/10 · 14.0 s** | 80,748 |
| Anthropic S-1 operating cash flow | 10/10 · 3.4 s | 10/10 · 6.5 s | 5/5 · 3.3 s | 10/10 · 4.8 s | 3,929 |
| Anthropic S-1 capex | **9/10 · 4.2 s** | 10/10 · 7.1 s | 5/5 · 3.2 s | 10/10 · 6.2 s | 482 |
| Anthropic S-1 method-rule operating result (M-1) | 10/10 · 5.0 s | 10/10 · 14.6 s | 5/5 · 5.6 s | **9/10 · 9.6 s** | -567 |

Cells show passes out of runs, then the median seconds. Every passing run on every arm
produced the same checked amount for its task.

### Every miss

**Nemotron 3: right row, wrong column (3, all refused by the source-cell check).**

| Task | Row it picked | Proposed | Archived cell (six months ended 30 June 2026) |
|---|---|---|---|
| S-1 capex | `Purchases of property and equipment \| (2) \| (15) \| (131) \| (18) \| (482)` | 15 | 482 |
| S-1 net loss | `Net loss (2) \| $ (1,531) \| $ (8,309) \| $ (41,965) \| $ (12,969) \| $ (80,748)` | 8,309 | 80,748 |
| S-1 net loss | the same row | 12,969 | 80,748 |

The S-1 tables print five periods side by side, and the current period is the last column.
In 3 of 70 calls Nemotron 3 took another period's figure. Opus never did in 35, and
Nemotron 3.5 never did in 140.

**Nemotron 3.5 at Finrail's 8,192-token cap: cut off (4).** Each reply stopped at exactly
8,192 tokens while the model was still thinking, so it held no answer. Each came back empty
after 18 to 24 s. Finrail reported `EMPTY_RESPONSE` and retried nothing. It happened on S-1
net loss twice, NVIDIA Q1 FCF once and the S-1 method-rule task once. With the cap raised to
32,768, all 70 calls answered and passed. This was not a usage limit or quota.

### The screen call (`finance.screen`, bundled Northwind fixture)

Every arm scored 0, and the models are not the cause:

| | Nemotron 3 | Nemotron 3.5 (32,768) | Opus | Nemotron 3.5 (8,192) |
|---|---|---|---|---|
| Passed | 0/10 | 0/10 | 0/5 | 0/10 |
| Quotes not verbatim | 0 | 0 | 0 | 0 |

`_SCREEN_SYSTEM` asks for "the assertion indices it supports or contradicts" but never names
the fields. The code reads `supports_assertions` and `contradicts_assertions`. The models
answer with `supports` / `contradicts`, or leave the indices out, so the gate sees no support
and marks every finding `rejected`. Every arm quoted its sources word for word. This needs a
one-line prompt fix, after which the screen part can be re-run. It is not counted above.

## Method

- **One seam.** Every call went through `finrail.core.llm.call()` with the exact arguments
  `ReportAnalyze.execute()` builds: the same system prompt, JSON prompt, `retrieved_rows`
  context and `wants` list. Results were judged by the unchanged
  `public_reports.check_and_compute`. The screen arm called the unchanged `_live_candidates`
  and `_classify`. For every task, the SHA-256 of system prompt plus prompt was identical on
  every arm.
- **Engines are configuration only.**
  - Nemotron arms: `FINRAIL_ENGINE=nebius`, with model `nvidia/nemotron-3-super-120b-a12b` or
    (through `FINRAIL_MODEL`) `nvidia/Nemotron-3.5-Super-VL`. Both on Token Factory,
    temperature 0, JSON mode.
  - Reply cap: Nemotron 3 and the first Nemotron 3.5 run used Finrail's `max_tokens` of
    8192. For the second Nemotron 3.5 run, the test harness rewrote `max_tokens` to 32768 in
    the request body. Finrail's code and defaults were not changed.
  - Opus: the default CLI engine with `FINRAIL_MODEL=claude-opus-5-5`, that is
    `claude -p --model claude-opus-5-5 --tools "" --strict-mcp-config` with Finrail's system
    prompt appended.
- **Retrieval held fixed.** Retrieval was captured once and every arm got identical excerpts.
  - NVIDIA Q1 and Q2: Vertex RAG `retrieveContexts`, top_k 8, query from the profile, captured
    30 Oct 2026. The archived cells were read from the hash-checked GCS object.
  - Anthropic S-1: the evidence recorded by live runs 53f78f23 (the revenue query, which the
    revenue, net loss, operating cash flow and capex definitions share) and 52af21fa (the
    method-rule query). The cells were rebuilt by `source_cells()` from the archived S-1
    bytes (SHA-256 `2fa24ae7…905a`).
- **Isolation.** `FINRAIL_STATE` was a fresh directory, so no lessons were injected into any
  arm and nothing was written to the real ledger.
- **Runs.** Each round went through all 7 analyze tasks plus the screen call.
  - Nemotron 3: 10 rounds.
  - Nemotron 3.5: 10 rounds at each cap.
  - Opus: 5 rounds. More were not run, by choice.
- **Latency** is wall clock around `llm.call()`, measured from the calling machine. For Opus
  it includes starting the CLI process (about 1 s). The two Nemotron 3.5 runs were made at
  different times, so part of their latency gap is the endpoint, not the cap.
- **Tokens** come from each engine's own usage report. A trivial CLI call ("reply ok") cost
  3,544 input tokens. That is the CLI's own prompt, which the Nemotron arms never see.

## Prices (checked 30 October 2026)

| Model | Input | Cached input | Output | Source |
|---|---|---|---|---|
| Nemotron 3 Super 120B A12B, Token Factory | $0.30 / M | $0.06 / M | $0.90 / M | [Token Factory model catalog](https://tokenfactory.nebius.com/model-catalog.md) |
| Nemotron 3.5 Super VL, Token Factory | $0.30 / M | $0.06 / M | $0.90 / M | same |
| Claude Opus 5.5 | $4 / M | $0.20 / M read; $5 / M 5-minute write, $8 / M 1-hour write | $20 / M | [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) |

The "list, no caching" figures price every input token at the base rate on every arm. The
CLI's own reported cost matched the 1-hour cache-write rate exactly.

**Nemotron 3 Super leaves Token Factory serverless on 9 November 2026**
([deprecation notice](https://docs.tokenfactory.nebius.com/model-deprecations-november-2026)).
Nebius names `nvidia/Nemotron-3.5-Super-VL` as the replacement, at the same price.

## Caveats

- **Sample size.** 70 analyze calls per Nemotron arm and 35 for Opus. The intervals overlap,
  so the small accuracy differences between models are not proven.
- **One task type in one domain.** The model's whole job here is choosing a row ID and copying
  a number. Code does the quoting, the period and unit checks and all the arithmetic. Tasks
  that need judgment are not tested: writing a memo, weighing conflicting evidence, or work
  with no code check behind it.
- **Retrieval held fixed.** Every model saw the same good excerpts, and every required row was
  in them. Noisier or thinner retrieval was not tested.
- **Few filings.** Two NVIDIA 10-Qs and one S-1, all clean SEC HTML.
- **Opus via the CLI.** This is how Finrail runs Opus. It adds about 3,540 tokens of CLI
  prompt and about 1 s of process start per call. The CLI has no temperature control, and
  the harness set no effort.
- **Nemotron 3.5's reply length varies a lot.** The median reply was about 3,900 tokens, the
  longest 11,934. A cap of 32,768 was never reached in 70 calls, but that is no guarantee.
- **Temperature 0 still varies.** Nemotron gave different values on identical excerpts. Read
  refusals as a rate, not as all-or-nothing.

## Recommendation

1. **Keep a Nemotron model for the analyze picks.** On accuracy it matches Opus within the
   sample, at about 1/17 of Opus's list cost per checked answer (Nemotron 3) or about 1/5.5
   (Nemotron 3.5). The harness turns every wrong pick into a refusal, never a wrong number.
2. **Switch to Nemotron 3.5 before 9 November, and raise Finrail's reply cap with it.** At
   32,768 tokens it passed 70 of 70. At today's 8,192, about 1 call in 17 comes back empty.
   The cap is the `max_tokens` constant in `_http_call` (`finrail/core/llm.py`).
3. **Show retries on the run record.** At these prices one retry after a refusal still leaves
   Nemotron far cheaper than Opus. Finrail's rule is never to retry silently, so the retry
   should be visible.
4. **State the claim narrowly:** "for picking source rows under code checks, a small open
   model matched Opus within sampling error, at a fraction of the cost." Do not say
   "harnesses like this don't need frontier models".

## Reproduce

The harness (`compare.py`, `summarize.py`, `build_report.py`, `readme_chart.py` and the
`operator-run*.sh` scripts) is kept outside the repo, in the shared side-session notes folder
under `model-comparison/`.

- The operator scripts read the Vertex token and the Nebius key into the environment and
  never print them.
- `compare.py run opus 5` needs only an authenticated `claude` CLI.
- `compare.py run nemotron35_32k N` needs only the Nebius key.

## Appendix: every analyze call

| arm | task | run | result | seconds | input tokens | output tokens |
|---|---|---|---|---|---|---|
| Nemotron 3 Super | Anthropic S-1 capex | 1 | refused by checks | 9.6 | 1,600 | 1,801 |
| Nemotron 3 Super | Anthropic S-1 capex | 2 | pass | 4.4 | 1,600 | 749 |
| Nemotron 3 Super | Anthropic S-1 capex | 3 | pass | 4.8 | 1,600 | 863 |
| Nemotron 3 Super | Anthropic S-1 capex | 4 | pass | 4.1 | 1,600 | 619 |
| Nemotron 3 Super | Anthropic S-1 capex | 5 | pass | 4.2 | 1,600 | 896 |
| Nemotron 3 Super | Anthropic S-1 capex | 6 | pass | 3.6 | 1,600 | 668 |
| Nemotron 3 Super | Anthropic S-1 capex | 7 | pass | 2.3 | 1,600 | 504 |
| Nemotron 3 Super | Anthropic S-1 capex | 8 | pass | 3.1 | 1,600 | 540 |
| Nemotron 3 Super | Anthropic S-1 capex | 9 | pass | 4.9 | 1,600 | 891 |
| Nemotron 3 Super | Anthropic S-1 capex | 10 | pass | 2.8 | 1,600 | 504 |
| Nemotron 3 Super | Anthropic S-1 method-rule operating result (M-1) | 1 | pass | 3.5 | 2,082 | 753 |
| Nemotron 3 Super | Anthropic S-1 method-rule operating result (M-1) | 2 | pass | 9.4 | 2,082 | 1,376 |
| Nemotron 3 Super | Anthropic S-1 method-rule operating result (M-1) | 3 | pass | 10.1 | 2,082 | 1,864 |
| Nemotron 3 Super | Anthropic S-1 method-rule operating result (M-1) | 4 | pass | 4.1 | 2,082 | 624 |
| Nemotron 3 Super | Anthropic S-1 method-rule operating result (M-1) | 5 | pass | 4.7 | 2,082 | 694 |
| Nemotron 3 Super | Anthropic S-1 method-rule operating result (M-1) | 6 | pass | 5.2 | 2,082 | 873 |
| Nemotron 3 Super | Anthropic S-1 method-rule operating result (M-1) | 7 | pass | 6.4 | 2,082 | 1,337 |
| Nemotron 3 Super | Anthropic S-1 method-rule operating result (M-1) | 8 | pass | 12.3 | 2,082 | 2,626 |
| Nemotron 3 Super | Anthropic S-1 method-rule operating result (M-1) | 9 | pass | 4.8 | 2,082 | 698 |
| Nemotron 3 Super | Anthropic S-1 method-rule operating result (M-1) | 10 | pass | 3.8 | 2,082 | 723 |
| Nemotron 3 Super | Anthropic S-1 net loss | 1 | pass | 4.0 | 1,592 | 843 |
| Nemotron 3 Super | Anthropic S-1 net loss | 2 | pass | 5.3 | 1,592 | 872 |
| Nemotron 3 Super | Anthropic S-1 net loss | 3 | pass | 9.5 | 1,592 | 1,774 |
| Nemotron 3 Super | Anthropic S-1 net loss | 4 | pass | 6.4 | 1,592 | 1,071 |
| Nemotron 3 Super | Anthropic S-1 net loss | 5 | pass | 4.8 | 1,592 | 709 |
| Nemotron 3 Super | Anthropic S-1 net loss | 6 | refused by checks | 14.2 | 1,592 | 2,666 |
| Nemotron 3 Super | Anthropic S-1 net loss | 7 | refused by checks | 4.2 | 1,592 | 1,122 |
| Nemotron 3 Super | Anthropic S-1 net loss | 8 | pass | 4.2 | 1,592 | 937 |
| Nemotron 3 Super | Anthropic S-1 net loss | 9 | pass | 5.0 | 1,592 | 1,081 |
| Nemotron 3 Super | Anthropic S-1 net loss | 10 | pass | 6.3 | 1,592 | 636 |
| Nemotron 3 Super | Anthropic S-1 operating cash flow | 1 | pass | 3.0 | 1,606 | 488 |
| Nemotron 3 Super | Anthropic S-1 operating cash flow | 2 | pass | 2.9 | 1,606 | 483 |
| Nemotron 3 Super | Anthropic S-1 operating cash flow | 3 | pass | 3.9 | 1,606 | 603 |
| Nemotron 3 Super | Anthropic S-1 operating cash flow | 4 | pass | 3.1 | 1,606 | 479 |
| Nemotron 3 Super | Anthropic S-1 operating cash flow | 5 | pass | 3.9 | 1,606 | 512 |
| Nemotron 3 Super | Anthropic S-1 operating cash flow | 6 | pass | 3.8 | 1,606 | 523 |
| Nemotron 3 Super | Anthropic S-1 operating cash flow | 7 | pass | 6.8 | 1,606 | 1,543 |
| Nemotron 3 Super | Anthropic S-1 operating cash flow | 8 | pass | 2.2 | 1,606 | 423 |
| Nemotron 3 Super | Anthropic S-1 operating cash flow | 9 | pass | 4.8 | 1,606 | 1,051 |
| Nemotron 3 Super | Anthropic S-1 operating cash flow | 10 | pass | 3.0 | 1,606 | 681 |
| Nemotron 3 Super | Anthropic S-1 revenue | 1 | pass | 7.1 | 1,592 | 1,200 |
| Nemotron 3 Super | Anthropic S-1 revenue | 2 | pass | 10.9 | 1,592 | 1,723 |
| Nemotron 3 Super | Anthropic S-1 revenue | 3 | pass | 11.3 | 1,592 | 1,979 |
| Nemotron 3 Super | Anthropic S-1 revenue | 4 | pass | 8.0 | 1,592 | 847 |
| Nemotron 3 Super | Anthropic S-1 revenue | 5 | pass | 4.4 | 1,592 | 691 |
| Nemotron 3 Super | Anthropic S-1 revenue | 6 | pass | 10.1 | 1,592 | 1,635 |
| Nemotron 3 Super | Anthropic S-1 revenue | 7 | pass | 9.4 | 1,592 | 2,176 |
| Nemotron 3 Super | Anthropic S-1 revenue | 8 | pass | 8.6 | 1,592 | 1,675 |
| Nemotron 3 Super | Anthropic S-1 revenue | 9 | pass | 4.9 | 1,592 | 877 |
| Nemotron 3 Super | Anthropic S-1 revenue | 10 | pass | 6.7 | 1,592 | 1,448 |
| Nemotron 3 Super | NVIDIA Q1 FY2027 free cash flow | 1 | pass | 6.1 | 748 | 1,030 |
| Nemotron 3 Super | NVIDIA Q1 FY2027 free cash flow | 2 | pass | 6.5 | 748 | 895 |
| Nemotron 3 Super | NVIDIA Q1 FY2027 free cash flow | 3 | pass | 6.9 | 748 | 1,181 |
| Nemotron 3 Super | NVIDIA Q1 FY2027 free cash flow | 4 | pass | 10.5 | 748 | 1,061 |
| Nemotron 3 Super | NVIDIA Q1 FY2027 free cash flow | 5 | pass | 6.8 | 748 | 1,189 |
| Nemotron 3 Super | NVIDIA Q1 FY2027 free cash flow | 6 | pass | 6.5 | 748 | 1,291 |
| Nemotron 3 Super | NVIDIA Q1 FY2027 free cash flow | 7 | pass | 5.9 | 748 | 1,034 |
| Nemotron 3 Super | NVIDIA Q1 FY2027 free cash flow | 8 | pass | 5.5 | 748 | 1,006 |
| Nemotron 3 Super | NVIDIA Q1 FY2027 free cash flow | 9 | pass | 5.3 | 748 | 962 |
| Nemotron 3 Super | NVIDIA Q1 FY2027 free cash flow | 10 | pass | 3.5 | 748 | 686 |
| Nemotron 3 Super | NVIDIA Q2 FY2027 free cash flow | 1 | pass | 8.3 | 1,064 | 1,281 |
| Nemotron 3 Super | NVIDIA Q2 FY2027 free cash flow | 2 | pass | 6.1 | 1,064 | 1,153 |
| Nemotron 3 Super | NVIDIA Q2 FY2027 free cash flow | 3 | pass | 7.6 | 1,064 | 1,101 |
| Nemotron 3 Super | NVIDIA Q2 FY2027 free cash flow | 4 | pass | 7.0 | 1,064 | 1,287 |
| Nemotron 3 Super | NVIDIA Q2 FY2027 free cash flow | 5 | pass | 5.8 | 1,064 | 857 |
| Nemotron 3 Super | NVIDIA Q2 FY2027 free cash flow | 6 | pass | 4.5 | 1,064 | 836 |
| Nemotron 3 Super | NVIDIA Q2 FY2027 free cash flow | 7 | pass | 5.1 | 1,064 | 1,235 |
| Nemotron 3 Super | NVIDIA Q2 FY2027 free cash flow | 8 | pass | 5.0 | 1,064 | 865 |
| Nemotron 3 Super | NVIDIA Q2 FY2027 free cash flow | 9 | pass | 6.5 | 1,064 | 1,151 |
| Nemotron 3 Super | NVIDIA Q2 FY2027 free cash flow | 10 | pass | 6.7 | 1,064 | 1,071 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 capex | 1 | pass | 6.8 | 1,600 | 1,314 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 capex | 2 | pass | 6.9 | 1,600 | 2,496 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 capex | 3 | pass | 7.3 | 1,600 | 2,493 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 capex | 4 | pass | 12.6 | 1,600 | 3,579 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 capex | 5 | pass | 4.7 | 1,600 | 1,686 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 capex | 6 | pass | 9.9 | 1,600 | 3,051 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 capex | 7 | pass | 5.1 | 1,600 | 2,118 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 capex | 8 | pass | 2.9 | 1,600 | 1,162 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 capex | 9 | pass | 8.8 | 1,600 | 2,251 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 capex | 10 | pass | 11.1 | 1,600 | 3,868 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 method-rule operating result (M-1) | 1 | pass | 41.5 | 2,082 | 10,957 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 method-rule operating result (M-1) | 2 | pass | 7.2 | 2,082 | 2,250 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 method-rule operating result (M-1) | 3 | pass | 10.8 | 2,082 | 3,317 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 method-rule operating result (M-1) | 4 | pass | 10.2 | 2,082 | 3,396 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 method-rule operating result (M-1) | 5 | pass | 34.6 | 2,082 | 9,422 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 method-rule operating result (M-1) | 6 | pass | 12.4 | 2,082 | 3,845 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 method-rule operating result (M-1) | 7 | pass | 26.5 | 2,082 | 10,612 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 method-rule operating result (M-1) | 8 | pass | 25.9 | 2,082 | 9,420 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 method-rule operating result (M-1) | 9 | pass | 6.1 | 2,082 | 1,672 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 method-rule operating result (M-1) | 10 | pass | 16.8 | 2,082 | 4,634 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 net loss | 1 | pass | 9.7 | 1,592 | 2,251 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 net loss | 2 | pass | 9.2 | 1,592 | 3,311 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 net loss | 3 | pass | 32.8 | 1,592 | 11,934 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 net loss | 4 | pass | 17.6 | 1,592 | 4,807 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 net loss | 5 | pass | 12.2 | 1,592 | 3,630 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 net loss | 6 | pass | 13.0 | 1,592 | 3,866 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 net loss | 7 | pass | 10.7 | 1,592 | 4,013 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 net loss | 8 | pass | 13.5 | 1,592 | 5,886 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 net loss | 9 | pass | 13.3 | 1,592 | 3,943 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 net loss | 10 | pass | 13.4 | 1,592 | 4,764 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 operating cash flow | 1 | pass | 23.9 | 1,606 | 5,079 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 operating cash flow | 2 | pass | 6.4 | 1,606 | 1,738 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 operating cash flow | 3 | pass | 7.2 | 1,606 | 2,232 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 operating cash flow | 4 | pass | 8.5 | 1,606 | 2,072 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 operating cash flow | 5 | pass | 8.5 | 1,606 | 2,458 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 operating cash flow | 6 | pass | 6.6 | 1,606 | 1,833 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 operating cash flow | 7 | pass | 3.3 | 1,606 | 1,365 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 operating cash flow | 8 | pass | 4.6 | 1,606 | 1,980 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 operating cash flow | 9 | pass | 3.9 | 1,606 | 1,326 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 operating cash flow | 10 | pass | 3.9 | 1,606 | 1,336 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 revenue | 1 | pass | 19.0 | 1,592 | 4,499 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 revenue | 2 | pass | 14.2 | 1,592 | 3,671 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 revenue | 3 | pass | 25.9 | 1,592 | 9,071 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 revenue | 4 | pass | 13.2 | 1,592 | 3,682 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 revenue | 5 | pass | 28.6 | 1,592 | 7,469 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 revenue | 6 | pass | 11.0 | 1,592 | 3,258 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 revenue | 7 | pass | 4.7 | 1,592 | 1,868 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 revenue | 8 | pass | 10.8 | 1,592 | 4,786 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 revenue | 9 | pass | 9.9 | 1,592 | 2,751 |
| Nemotron 3.5 Super VL (32,768 cap) | Anthropic S-1 revenue | 10 | pass | 9.9 | 1,592 | 2,758 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q1 FY2027 free cash flow | 1 | pass | 46.1 | 748 | 10,394 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q1 FY2027 free cash flow | 2 | pass | 24.0 | 748 | 6,057 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q1 FY2027 free cash flow | 3 | pass | 22.7 | 748 | 6,463 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q1 FY2027 free cash flow | 4 | pass | 12.5 | 748 | 4,534 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q1 FY2027 free cash flow | 5 | pass | 17.1 | 748 | 5,671 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q1 FY2027 free cash flow | 6 | pass | 19.2 | 748 | 6,054 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q1 FY2027 free cash flow | 7 | pass | 18.7 | 748 | 5,994 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q1 FY2027 free cash flow | 8 | pass | 5.0 | 748 | 2,156 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q1 FY2027 free cash flow | 9 | pass | 18.2 | 748 | 4,493 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q1 FY2027 free cash flow | 10 | pass | 20.4 | 748 | 6,473 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q2 FY2027 free cash flow | 1 | pass | 27.0 | 1,064 | 7,021 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q2 FY2027 free cash flow | 2 | pass | 14.6 | 1,064 | 3,513 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q2 FY2027 free cash flow | 3 | pass | 21.8 | 1,064 | 7,978 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q2 FY2027 free cash flow | 4 | pass | 13.5 | 1,064 | 5,334 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q2 FY2027 free cash flow | 5 | pass | 26.3 | 1,064 | 7,226 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q2 FY2027 free cash flow | 6 | pass | 14.4 | 1,064 | 5,261 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q2 FY2027 free cash flow | 7 | pass | 12.3 | 1,064 | 4,863 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q2 FY2027 free cash flow | 8 | pass | 11.0 | 1,064 | 4,669 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q2 FY2027 free cash flow | 9 | pass | 14.2 | 1,064 | 3,760 |
| Nemotron 3.5 Super VL (32,768 cap) | NVIDIA Q2 FY2027 free cash flow | 10 | pass | 14.6 | 1,064 | 5,215 |
| Opus 5.5 | Anthropic S-1 capex | 1 | pass | 3.1 | 5,377 | 96 |
| Opus 5.5 | Anthropic S-1 capex | 2 | pass | 3.2 | 5,377 | 96 |
| Opus 5.5 | Anthropic S-1 capex | 3 | pass | 3.3 | 5,377 | 96 |
| Opus 5.5 | Anthropic S-1 capex | 4 | pass | 3.1 | 5,377 | 96 |
| Opus 5.5 | Anthropic S-1 capex | 5 | pass | 3.4 | 5,377 | 96 |
| Opus 5.5 | Anthropic S-1 method-rule operating result (M-1) | 1 | pass | 5.6 | 6,052 | 295 |
| Opus 5.5 | Anthropic S-1 method-rule operating result (M-1) | 2 | pass | 5.3 | 6,052 | 253 |
| Opus 5.5 | Anthropic S-1 method-rule operating result (M-1) | 3 | pass | 5.7 | 6,052 | 258 |
| Opus 5.5 | Anthropic S-1 method-rule operating result (M-1) | 4 | pass | 5.0 | 6,052 | 220 |
| Opus 5.5 | Anthropic S-1 method-rule operating result (M-1) | 5 | pass | 6.1 | 6,052 | 224 |
| Opus 5.5 | Anthropic S-1 net loss | 1 | pass | 6.2 | 5,371 | 97 |
| Opus 5.5 | Anthropic S-1 net loss | 2 | pass | 3.4 | 5,371 | 97 |
| Opus 5.5 | Anthropic S-1 net loss | 3 | pass | 2.9 | 5,371 | 97 |
| Opus 5.5 | Anthropic S-1 net loss | 4 | pass | 2.9 | 5,371 | 97 |
| Opus 5.5 | Anthropic S-1 net loss | 5 | pass | 3.0 | 5,371 | 97 |
| Opus 5.5 | Anthropic S-1 operating cash flow | 1 | pass | 3.1 | 5,390 | 105 |
| Opus 5.5 | Anthropic S-1 operating cash flow | 2 | pass | 4.7 | 5,390 | 133 |
| Opus 5.5 | Anthropic S-1 operating cash flow | 3 | pass | 3.2 | 5,390 | 105 |
| Opus 5.5 | Anthropic S-1 operating cash flow | 4 | pass | 3.3 | 5,390 | 133 |
| Opus 5.5 | Anthropic S-1 operating cash flow | 5 | pass | 3.9 | 5,390 | 105 |
| Opus 5.5 | Anthropic S-1 revenue | 1 | pass | 3.5 | 5,369 | 95 |
| Opus 5.5 | Anthropic S-1 revenue | 2 | pass | 3.8 | 5,369 | 222 |
| Opus 5.5 | Anthropic S-1 revenue | 3 | pass | 6.0 | 5,369 | 95 |
| Opus 5.5 | Anthropic S-1 revenue | 4 | pass | 3.2 | 5,369 | 95 |
| Opus 5.5 | Anthropic S-1 revenue | 5 | pass | 3.1 | 5,369 | 139 |
| Opus 5.5 | NVIDIA Q1 FY2027 free cash flow | 1 | pass | 5.6 | 4,513 | 282 |
| Opus 5.5 | NVIDIA Q1 FY2027 free cash flow | 2 | pass | 4.6 | 4,513 | 245 |
| Opus 5.5 | NVIDIA Q1 FY2027 free cash flow | 3 | pass | 3.9 | 4,513 | 254 |
| Opus 5.5 | NVIDIA Q1 FY2027 free cash flow | 4 | pass | 4.2 | 4,513 | 280 |
| Opus 5.5 | NVIDIA Q1 FY2027 free cash flow | 5 | pass | 4.0 | 4,513 | 277 |
| Opus 5.5 | NVIDIA Q2 FY2027 free cash flow | 1 | pass | 5.7 | 4,854 | 266 |
| Opus 5.5 | NVIDIA Q2 FY2027 free cash flow | 2 | pass | 4.5 | 4,854 | 281 |
| Opus 5.5 | NVIDIA Q2 FY2027 free cash flow | 3 | pass | 7.1 | 4,854 | 173 |
| Opus 5.5 | NVIDIA Q2 FY2027 free cash flow | 4 | pass | 4.6 | 4,854 | 263 |
| Opus 5.5 | NVIDIA Q2 FY2027 free cash flow | 5 | pass | 4.3 | 4,854 | 257 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 capex | 1 | pass | 6.2 | 1,600 | 2,789 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 capex | 2 | pass | 5.5 | 1,600 | 2,486 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 capex | 3 | pass | 6.2 | 1,600 | 2,726 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 capex | 4 | pass | 5.4 | 1,600 | 2,250 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 capex | 5 | pass | 12.4 | 1,600 | 2,458 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 capex | 6 | pass | 6.4 | 1,600 | 2,051 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 capex | 7 | pass | 7.6 | 1,600 | 2,331 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 capex | 8 | pass | 4.7 | 1,600 | 1,836 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 capex | 9 | pass | 5.9 | 1,600 | 2,087 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 capex | 10 | pass | 7.3 | 1,600 | 3,199 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 method-rule operating result (M-1) | 1 | pass | 8.0 | 2,082 | 3,449 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 method-rule operating result (M-1) | 2 | pass | 8.4 | 2,082 | 3,442 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 method-rule operating result (M-1) | 3 | pass | 12.5 | 2,082 | 5,466 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 method-rule operating result (M-1) | 4 | pass | 7.7 | 2,082 | 2,944 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 method-rule operating result (M-1) | 5 | pass | 16.0 | 2,082 | 3,431 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 method-rule operating result (M-1) | 6 | pass | 15.1 | 2,082 | 5,105 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 method-rule operating result (M-1) | 7 | cut off by reply cap | 23.6 | 2,082 | 8,192 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 method-rule operating result (M-1) | 8 | pass | 10.4 | 2,082 | 3,481 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 method-rule operating result (M-1) | 9 | pass | 8.8 | 2,082 | 3,981 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 method-rule operating result (M-1) | 10 | pass | 7.9 | 2,082 | 3,219 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 net loss | 1 | pass | 14.5 | 1,592 | 6,160 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 net loss | 2 | pass | 14.6 | 1,592 | 6,164 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 net loss | 3 | pass | 12.2 | 1,592 | 5,780 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 net loss | 4 | cut off by reply cap | 18.2 | 1,592 | 8,192 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 net loss | 5 | pass | 9.9 | 1,592 | 2,592 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 net loss | 6 | pass | 13.5 | 1,592 | 4,903 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 net loss | 7 | pass | 8.0 | 1,592 | 2,677 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 net loss | 8 | pass | 8.4 | 1,592 | 3,363 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 net loss | 9 | cut off by reply cap | 18.7 | 1,592 | 8,192 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 net loss | 10 | pass | 15.3 | 1,592 | 6,403 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 operating cash flow | 1 | pass | 3.6 | 1,606 | 1,480 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 operating cash flow | 2 | pass | 3.2 | 1,606 | 1,480 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 operating cash flow | 3 | pass | 5.4 | 1,606 | 2,136 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 operating cash flow | 4 | pass | 3.9 | 1,606 | 1,457 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 operating cash flow | 5 | pass | 7.1 | 1,606 | 1,415 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 operating cash flow | 6 | pass | 6.2 | 1,606 | 2,183 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 operating cash flow | 7 | pass | 4.1 | 1,606 | 1,418 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 operating cash flow | 8 | pass | 3.5 | 1,606 | 1,341 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 operating cash flow | 9 | pass | 5.9 | 1,606 | 2,507 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 operating cash flow | 10 | pass | 5.5 | 1,606 | 2,142 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 revenue | 1 | pass | 12.5 | 1,592 | 3,463 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 revenue | 2 | pass | 10.8 | 1,592 | 4,844 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 revenue | 3 | pass | 7.7 | 1,592 | 3,613 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 revenue | 4 | pass | 7.3 | 1,592 | 3,325 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 revenue | 5 | pass | 10.0 | 1,592 | 3,235 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 revenue | 6 | pass | 11.2 | 1,592 | 3,412 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 revenue | 7 | pass | 7.1 | 1,592 | 2,701 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 revenue | 8 | pass | 7.7 | 1,592 | 2,536 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 revenue | 9 | pass | 5.0 | 1,592 | 1,906 |
| Nemotron 3.5 Super VL (8,192 cap) | Anthropic S-1 revenue | 10 | pass | 10.2 | 1,592 | 4,386 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q1 FY2027 free cash flow | 1 | pass | 11.8 | 748 | 5,181 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q1 FY2027 free cash flow | 2 | pass | 14.8 | 748 | 6,664 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q1 FY2027 free cash flow | 3 | pass | 10.4 | 748 | 4,718 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q1 FY2027 free cash flow | 4 | pass | 13.6 | 748 | 5,774 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q1 FY2027 free cash flow | 5 | cut off by reply cap | 20.8 | 748 | 8,192 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q1 FY2027 free cash flow | 6 | pass | 25.4 | 748 | 6,013 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q1 FY2027 free cash flow | 7 | pass | 8.6 | 748 | 2,826 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q1 FY2027 free cash flow | 8 | pass | 14.2 | 748 | 5,664 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q1 FY2027 free cash flow | 9 | pass | 25.1 | 748 | 7,781 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q1 FY2027 free cash flow | 10 | pass | 14.1 | 748 | 6,009 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q2 FY2027 free cash flow | 1 | pass | 9.3 | 1,064 | 3,366 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q2 FY2027 free cash flow | 2 | pass | 7.5 | 1,064 | 3,411 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q2 FY2027 free cash flow | 3 | pass | 10.7 | 1,064 | 4,799 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q2 FY2027 free cash flow | 4 | pass | 8.7 | 1,064 | 3,411 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q2 FY2027 free cash flow | 5 | pass | 10.2 | 1,064 | 3,979 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q2 FY2027 free cash flow | 6 | pass | 16.7 | 1,064 | 4,637 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q2 FY2027 free cash flow | 7 | pass | 6.8 | 1,064 | 2,352 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q2 FY2027 free cash flow | 8 | pass | 12.6 | 1,064 | 4,065 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q2 FY2027 free cash flow | 9 | pass | 10.3 | 1,064 | 2,944 |
| Nemotron 3.5 Super VL (8,192 cap) | NVIDIA Q2 FY2027 free cash flow | 10 | pass | 8.9 | 1,064 | 3,926 |
