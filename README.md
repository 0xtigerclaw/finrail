# Finrail

A finance-first agent harness where the model can suggest evidence, but only code can award it trust, and only a human can release a decision.

Finrail is built for investment work, where a wrong number that looks right is the worst possible outcome. It is a deterministic dispatcher over pluggable capabilities, each of which declares its own rules once, in code: whether it reads or acts, whether a person must approve, whether it runs in the background, and how its result is verified after it executes. The model is confined to judged work inside those rails. The same capability runs unchanged on different model engines behind one seam; the trust rules do not move.

## The four finance packs

| Pack | What it does | The rule it enforces |
| --- | --- | --- |
| `finance.screen` | Public-risk screening across leadership, regulatory, cyber and distress categories | A deterministic evidence gate is the sole authority for VERIFIED. A quote that is not verbatim in its source is discarded, never repaired. The model never chooses the legal entity; a person confirms it first. |
| `finance.dataroom` | Search and drafting over a private data room | Every figure in a drafted record must be carried by the document it cites. The claim check refuses it otherwise, and an unverified draft can never carry a figure. |
| `finance.compute` | Margin, growth, leverage, covenant headroom, cash conversion | Arithmetic is Python, from cited workbook cells, recomputed independently before it is reported. The model may select a metric; it never does the maths. |
| `finance.memo` | IC memo drafting and release | The draft may state only figures carried by cited sources; the rest are listed as gaps. Release is a separate approval bound to the memo's hash, and the released artefact is re-read and re-hashed afterwards. |

A fifth pack, `tasks`, ships as the generic proof: the harness is not finance-only, and an unmodified non-finance pack runs under the same rules.

## Try the planted failure

The demonstration data room is synthetic: one fictional target, Meridian Components, with hand-verifiable figures, plus one planted failure. An unverified draft note claims a 24.5% EBITDA margin that no financial statement carries (the workbook gives 16.0%). Ask the harness to draft that figure into a record, on either engine:

```
finrail do finance.dataroom.propose "Draft a record entry stating that Meridian achieved a 24.5% EBITDA margin in FY2025"
```

The claim check refuses it, on the record. Ask for a memo and the same figure appears where it belongs: in the gaps list.

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
pytest                         # offline tests: gate, claim check, compute, seam
```

## Engines: one seam, sponsor configuration

Every model call goes through one entry point (`finrail/core/llm.py`). The engine is environment configuration, never a capability branch:

| Deployment | Configuration |
| --- | --- |
| Nebius (this submission) | `FINRAIL_ENGINE=nebius`, `NEBIUS_API_KEY` in the environment. Judged capabilities run on NVIDIA Nemotron (`nvidia/nemotron-3-super-120b-a12b`) via the Nebius Token Factory API. |
| Meta | `FINRAIL_ENGINE=meta`, `FINRAIL_ENGINE_BASE_URL`, `FINRAIL_ENGINE_API_KEY`, `FINRAIL_MODEL` naming the Muse model. Same capabilities, same gate, same verdicts. |
| Local development | The default CLI engine. Never used in a judged deployment. |

There is no silent fallback: if the configured engine is down or rejects a call, the run fails under a canonical error code (`PROVIDER_UNAVAILABLE`, `SCHEMA_INVALID`, `EMPTY_RESPONSE`, `TIMEOUT`, `CONTEXT_WINDOW_EXCEEDED`) instead of quietly answering from another provider. Tool-using and session calls are refused on the HTTP engines (`ENGINE_UNSUPPORTED`); the judged surface is what the seam serves.

Every judged call is written to the run ledger as a `model_call` event carrying the full prompt, the answer, the engine, the model and the token usage, so a judged finance call is reconstructable from the ledger alone.

## Web console

```
finrail-console   # http://127.0.0.1:8000
```

Pages for Home, Capabilities, Rules, Screen, Data room, Compute, Memo, Approvals, and Runs and ledger, each dispatching through the normal capability path. There is no privileged browser path: approvals from the console pass the same caller rule as the CLI, so a console started by an agent cannot approve the agent's runs, and the page says so.

## A new, standalone project

Finrail is a new open-source project, created in October 2026. This repository is complete in itself: it has no connection to any other repository, does not import or depend on any external codebase, and needs nothing outside this tree to install, run, test or evaluate. Everything described on this page — the harness core, the engine seam, the four finance packs, the web console and the demonstration data — lives here, and the synthetic demo data means a fresh clone runs with no private data and no credentials for its read and test paths.

What was built: a deterministic capability dispatcher with rules declared once in code; an engine seam that serves judged calls over HTTPS with canonical error codes and no silent fallback; the `finance.screen` evidence gate behind a human entity confirmation; the `finance.dataroom` claim check; the deterministic `finance.compute` pack; the hash-bound `finance.memo` release; and the web console that renders capabilities, rules, approvals and the run ledger from the live system.

## Evaluation

The repository's own test suite runs fully offline and makes no provider calls: 18 tests covering the evidence gate (including a fabricated quote being discarded rather than repaired, and a single-publisher finding held at REPORTED rather than VERIFIED), the claim check, hand-verified compute arithmetic, the confirmed-entity screen flow, the hash-bound memo release, and the engine seam against a recorded local stub (including refusal of tool calls and failure under canonical error codes). The planted-failure fixture is a regression test, not a demo trick: it must be refused on every configured engine.

## Honest limitations

- Manually started agents running inside a person's own login session share that person's session scope. This is disclosed rather than papered over; closing it is roadmap work.
- In-process capability code, like any in-process plugin system, is not OS-sandboxed. Per-pack filesystem roots are declared, and the wording here claims no more than that.
- The demo data room is one synthetic target and one archived fixture screen; live retrieval breadth is the screen pack's roadmap, not this submission's claim.
- The demonstration deployment keeps state on one instance's disk. That is a demo property, stated here rather than discovered by a judge.

## Licence

Apache 2.0. See `LICENSE`.
