# Finrail

A finance-first agent harness where the model can suggest evidence, but only code can award it trust, and only a human can release a decision.

Inspired by DeepSeek Harness, which made everything a plugin. In Finrail every plugin also declares its trust rules in code: whether it reads or acts, whether a person must approve, how its result is verified, how a person asks for it (`ask`), and the steps it runs, each with who acts, the rule that holds it and where that rule is enforced (`steps`). The router, the chat trace, the proof page and the Rules page are all drawn from those declarations, so nothing on screen claims more than the code declares.

## Who does what

| Part | Its one job | What it can never do |
| --- | --- | --- |
| **Tavily** | Finds what the web says about a company | Count as evidence. A web page is a claim to check. |
| **Jev (TypeSafe)** | Reads: picks which workflow answers a question, and which cash-flow line and quarter a claim is about, with a confidence | Write text or a number. It can only pick from the choices offered; below the bar nothing is decided. |
| **NVIDIA Nemotron on Nebius** | Extracts: picks the filing rows and values for a calculation | Write a quote or do arithmetic. Code attaches the exact row text. |
| **Google Vertex RAG** | Retrieves excerpts from six archived filings | Return a source outside the selected filing's files. |
| **Kernel (code)** | Decides: checks every figure against the hash-checked archived cell, does the arithmetic in Decimal, compares claims | Answer without reconciling. There is no fallback. |
| **A person** | Releases a record | Be bypassed by the browser. Approval runs only from the operator's terminal. |

Every Nemotron call is written to the ledger as a `model_call` event, and every Jev call as a `decision_call` event: what the model was shown and what it returned. The rule is DeepSeek Harness's: if the model saw it, it is a record.

## The finance packs

| Pack | What it does | The rule it enforces |
| --- | --- | --- |
| `finance.reports` | Six real quarterly filings from NVIDIA, Nebius and ASML in a managed Vertex RAG corpus; NVIDIA free cash flow reconstructed from the filing; stated figures and web claims checked against it | Inputs must match the exact archived source cell; the total must equal NVIDIA's reported FCF or no answer is issued. A recorded figure must equal the checked line, and a person releases it. |
| `finance.screen` | Public-risk screening across leadership, regulatory, cyber and distress categories | A deterministic evidence gate is the sole authority for VERIFIED. A quote that is not verbatim in its source is discarded, never repaired. |
| `finance.dataroom`, `finance.compute`, `finance.memo` | Claim check, deterministic metrics and hash-bound memo release over a synthetic data room (CLI and tests) | Figures must be carried by cited sources; arithmetic is Python; release is bound to the memo's hash. |

A further pack, `tasks`, ships as the generic proof: the harness is not finance-only, and an unmodified non-finance pack runs under the same rules.

## Try the wrong figure

NVIDIA's Q2 FY2027 filing reports free cash flow of $21,341m: $24,077m operating cash flow, less $2,677m purchases and $59m principal payments. Ask the harness to record operating cash flow as if it were free cash flow:

```
finrail do finance.reports.record report_id=NVDA-Q2-FY2027 statement="Record NVIDIA's Q2 free cash flow as $24,077m"
```

Code reads the figure as typed, Jev reads which line the sentence is about, and the claim check refuses it: $24,077m is NVIDIA's operating cash flow, not its free cash flow. State it as operating cash flow and it passes, then waits for a person. (It needs a completed `finance.reports.analyze` run for that quarter first.)

## Quickstart

```
git clone https://github.com/0xtigerclaw/finrail
cd finrail
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[console,dev]"
export FINRAIL_STATE=$PWD/.state FINRAIL_HOME=$PWD/.home
finrail capabilities          # what exists, and the rules each declares
finrail rules                 # the same declarations, rendered
finrail get finance.compute.metric metric=margin
pytest                         # offline tests: checks, router, claim reader, gate, seam
```

## Engines: one seam, sponsor configuration

Every model call goes through one entry point (`finrail/core/llm.py`). The engine is environment configuration, never a capability branch:

| Deployment | Configuration |
| --- | --- |
| Nebius (this submission) | `FINRAIL_ENGINE=nebius`, `NEBIUS_API_KEY` in the environment. Judged capabilities run on NVIDIA Nemotron (`nvidia/nemotron-3-super-120b-a12b`) via the Nebius Token Factory API. |
| Meta | `FINRAIL_ENGINE=meta`, `FINRAIL_ENGINE_BASE_URL`, `FINRAIL_ENGINE_API_KEY`, `FINRAIL_MODEL` naming the Muse model. Same capabilities, same gate, same verdicts. |
| Local development | The default CLI engine. Never used in a judged deployment. |

There is no silent fallback: if the configured engine is down or rejects a call, the run fails under a canonical error code (`PROVIDER_UNAVAILABLE`, `SCHEMA_INVALID`, `EMPTY_RESPONSE`, `TIMEOUT`, `CONTEXT_WINDOW_EXCEEDED`) instead of quietly answering from another provider. Tool-using and session calls are refused on the HTTP engines (`ENGINE_UNSUPPORTED`); the judged surface is what the seam serves.

`finance.screen.run` retrieves its evidence live when `TAVILY_API_KEY` is in the environment: Tavily searches the public web across the four risk categories, the model proposes findings from the fetched pages, and the deterministic gate awards every status (a failed retrieval fails the run). Without the key, the run classifies the bundled fixture screen and says so in its result record. Tavily does discovery only; it is never evidence.

Every judged call is written to the run ledger as a `model_call` event carrying the full prompt, the answer, the engine, the model and the token usage, so a judged finance call is reconstructable from the ledger alone.

## Web console

```
finrail-console   # http://127.0.0.1:8000
```

Ask (a chat on the left, the harness trace for each answer on the right), Filings (the six filings, every indexed file reconciled live against the corpus), Proof (the NVIDIA workpaper and a tamper replay), Ledger (runs, Nemotron calls and Jev calls), Rules, and under More: Approvals, Screening, Capabilities and a CLI page generated from the live registry. Each dispatches through the normal capability path. Every workflow page also shows the exact `finrail` command that does the same thing from a terminal: the harness is a CLI first, and the console is a shell over the same dispatcher. There is no privileged browser path: approvals from the console pass the same caller rule as the CLI, so a console started by an agent cannot approve the agent's runs, and the page says so.

## A new, standalone project

Finrail is a new open-source project, created in October 2026. This repository is complete in itself: it has no connection to any other repository, does not import or depend on any external codebase, and needs nothing outside this tree to install, run, test or evaluate. Everything described on this page (the harness core, the engine seam, the finance packs, the web console and the demonstration data) lives here. DeepSeek Harness is an inspiration for the design, not a dependency: no code is shared. The offline tests and synthetic fixtures mean a fresh clone runs with no private data and no credentials for its read and test paths; the live filings, Jev and Tavily paths need their keys.

What was built: a deterministic capability dispatcher with rules declared once in code; an engine seam that serves judged calls over HTTPS with canonical error codes and no silent fallback; the `finance.screen` evidence gate behind a human entity confirmation; the `finance.dataroom` claim check; the deterministic `finance.compute` pack; the hash-bound `finance.memo` release; and the web console that renders capabilities, rules, approvals and the run ledger from the live system.

## Evaluation

The repository's own test suite runs fully offline and makes no provider calls: 127 tests covering the source-cell checks and a tamper replay on the FCF calculation, the router and the claim reader against stubbed Jev answers (including a pick outside the offered choices being discarded), the record check (operating cash flow stated as free cash flow is refused and named), web claims against stubbed Tavily pages, every Jev call reaching the ledger, the evidence gate (including a fabricated quote being discarded rather than repaired, and a single-publisher finding held at REPORTED rather than VERIFIED), the claim check, hand-verified compute arithmetic, the confirmed-entity screen flow, the live screen path against a recorded retrieval stub (two independent publishers verify; a fabricated quote is rejected; a down provider fails loudly), the hash-bound memo release, and the engine seam against a recorded local stub (including refusal of tool calls and failure under canonical error codes). The planted-failure fixture is a regression test, not a demo trick: it must be refused on every configured engine.

## Honest limitations

- Manually started agents running inside a person's own login session share that person's session scope. This is disclosed rather than papered over; closing it is roadmap work.
- In-process capability code, like any in-process plugin system, is not OS-sandboxed. Per-pack filesystem roots are declared, and the wording here claims no more than that.
- The demo data room is one synthetic target. Live screening runs against the public web for any confirmed entity when a Tavily key is configured; source breadth on long-tail private companies is what it is, and the gate reports absence rather than inventing coverage.
- The demonstration deployment keeps state on one instance's disk. That is a demo property, stated here rather than discovered by a judge.

## Licence

Apache 2.0. See `LICENSE`.
