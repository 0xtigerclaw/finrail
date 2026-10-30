"""THE RUNNER: every case through `dispatch_core.dispatch`, N times, with the ledger as witness.

What is measured is the system as it runs. A case is dispatched exactly as the console
or the CLI would dispatch it: the capability plans, the model is called through
`llm.call`, the code checks judge the answer, the run record and the `model_call` event
land in the ledger. The runner then reads the ledger for what the call cost, how long
the model took and how many tokens it used, rather than timing or counting anything of
its own beyond the wall clock around the dispatch.

Two deliberate departures from an ordinary dispatch:

* `finance.reports.analyze` declares `background = True`, so `dispatch` would hand it to
  a detached worker and return at once. The eval has to wait for the answer and read the
  ledger in this process, so the flag is turned off on the registry's instance for the
  duration of the run and put back after. Everything else about the dispatch is unchanged.
* An adversarial case may ask for a tamper on the model's answer (`inject`). The tamper is
  applied by a wrapper around `llm.call`, after the engine has answered and before the
  capability sees the answer, which is exactly where a model's own miss would appear. It
  is the only way to drive a wrong pick on a live engine, whose answers cannot be chosen.

State is isolated: FINRAIL_STATE and FINRAIL_HOME point under the output directory, so no
lesson is injected into a prompt and nothing is written to a real ledger.
"""
import datetime
import json
import os
import pathlib
import re
import subprocess
import sys
import time

from . import cases as _cases
from . import stats, stub

SUMMARY_SCHEMA = "finrail.eval.summary/1"
#: The seam's canonical error codes (finrail.core.llm), looked for in an error's text.
CODES = ("CONTEXT_WINDOW_EXCEEDED", "EMPTY_RESPONSE", "SCHEMA_INVALID", "PROVIDER_UNAVAILABLE",
         "TIMEOUT", "ENGINE_UNSUPPORTED")
OUTCOMES = ("passed", "refused", "cut_off", "schema_invalid", "engine_error", "failed",
            "wrong_figure", "error")
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")

#: The tamper in flight, read by the `llm.call` wrapper. One process, one call at a time.
_INJECT = {"tamper": "", "report_id": ""}


# ── tampers: a model's miss, made on purpose ─────────────────────────────────
def tamper(kind: str, answer: dict, context: dict, report_id: str) -> dict:
    """Return the model's answer with one deliberate fault in it. The checks must refuse it."""
    out = json.loads(json.dumps(answer))
    inputs = out.get("inputs") if isinstance(out.get("inputs"), list) else []
    if kind == "wrong_report":
        from finrail.extensions.finance import public_reports as r
        others = [i["source_id"] for i in r.catalog() if i["source_id"] != report_id]
        out["report_id"] = others[0] if others else "NO-SUCH-REPORT"
        return out
    if not inputs or not isinstance(inputs[0], dict):
        return out
    if kind == "invented_row":
        inputs[0]["row_id"] = "E9:L99"
    elif kind == "drop_input":
        inputs.pop()
    elif kind == "other_column":
        try:
            rows = {row["row_id"]: row for row in json.loads(context.get("retrieved_rows") or "[]")}
        except (ValueError, TypeError, KeyError):
            rows = {}
        quote = (rows.get(inputs[0].get("row_id")) or {}).get("quote", "")
        numbers = [n.replace(",", "") for n in _NUMBER.findall(quote)]
        others = [n for n in numbers if n != str(inputs[0].get("value"))]
        inputs[0]["value"] = others[-1] if others else f"{inputs[0].get('value')}0"
    return out


def _wrap_llm():
    """Install the tamper wrapper on `llm.call`; return the function that removes it."""
    from finrail.core import llm
    original = llm.call

    def wrapped(*args, **kwargs):
        out = original(*args, **kwargs)
        if _INJECT["tamper"] and isinstance(out, dict):
            out = tamper(_INJECT["tamper"], out, kwargs.get("context") or {}, _INJECT["report_id"])
        return out

    llm.call = wrapped

    def restore():
        llm.call = original
    return restore


# ── what happened on one call ────────────────────────────────────────────────
def classify(exc, result=None) -> tuple:
    """(outcome, error_code, message) for a dispatch that returned `result` or raised `exc`."""
    from finrail.core.errors import ContractViolation, EngineFailed, NotConfigured, OpFailed
    if exc is None:
        answer = (result or {}).get("result") if isinstance(result, dict) else None
        if isinstance(answer, dict) and isinstance(answer.get("answer"), dict) \
                and answer["answer"].get("refused"):
            return "refused", "CONTRACT_VIOLATION", str(answer["answer"]["refused"])
        return "passed", "", ""
    msg = str(exc)
    code = next((c for c in CODES if c in msg), "")
    if isinstance(exc, ContractViolation):
        return "refused", code or "CONTRACT_VIOLATION", msg
    if isinstance(exc, EngineFailed):
        if code == "EMPTY_RESPONSE":
            return "cut_off", code, msg
        return "engine_error", code or "ENGINE_FAILED", msg
    if isinstance(exc, OpFailed):
        if code == "SCHEMA_INVALID":
            return "schema_invalid", code, msg
        return "failed", code or "OP_FAILED", msg
    if isinstance(exc, NotConfigured):
        return "failed", "NOT_CONFIGURED", msg
    return "error", f"{type(exc).__name__}", msg


def meets(expect: dict, outcome: str, amount, code: str, message: str) -> bool:
    want = expect["outcome"]
    if want == "passed":
        return outcome == "passed"
    if want == "figure":
        return outcome == "passed" and str(amount) == str(expect["amount"])
    if outcome not in ("refused", "cut_off", "schema_invalid", "engine_error", "failed"):
        return False
    if expect.get("code") and expect["code"] != code:
        return False
    if expect.get("reason") and expect["reason"] not in (message or ""):
        return False
    return True


def _amount(result) -> str:
    answer = (result or {}).get("result") if isinstance(result, dict) else None
    return str(answer.get("amount")) if isinstance(answer, dict) and "amount" in answer else ""


def _model_calls(offset: int) -> tuple:
    """The `model_call` events written since `offset`, and the new offset."""
    from finrail.core import events
    found, offset = events.read_from(events.LEDGER, offset, kinds=("model_call",))
    return [ev.get("data") or {} for ev in found], offset


def _usage(calls: list, price_model: str) -> dict:
    inp = out = 0
    ledger_cost = 0.0
    priced_any = False
    model = ""
    for data in calls:
        usage = data.get("usage") or {}
        inp += sum(int(usage.get(k) or 0) for k in
                   ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        out += int(usage.get("output_tokens") or 0)
        model = data.get("engine_model") or data.get("model") or model
        if data.get("cost_usd") is not None:
            ledger_cost += float(data["cost_usd"])
            priced_any = True
    list_cost = stats.list_cost(price_model or model, inp, out) if calls else 0.0
    return {"model_calls": len(calls), "model": model, "input_tokens": inp, "output_tokens": out,
            "model_ms": sum(int(d.get("duration_ms") or 0) for d in calls),
            "cost_usd_ledger": round(ledger_cost, 6) if priced_any else None,
            "cost_usd_list": list_cost}


# ── the run ──────────────────────────────────────────────────────────────────
def _git() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              timeout=5, cwd=pathlib.Path(__file__).resolve().parents[2]).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _environment(engine: str, model: str, max_tokens: str, out: pathlib.Path) -> dict:
    """Set the run's environment; return what to put back."""
    wanted = {"FINRAIL_STATE": str(out / "state"), "FINRAIL_HOME": str(out / "home")}
    if engine != stub.ENGINE:
        wanted["FINRAIL_ENGINE"] = engine
        if model:
            wanted["FINRAIL_MODEL"] = model
    if max_tokens:
        wanted["FINRAIL_ENGINE_MAX_TOKENS"] = str(max_tokens)
    saved = {k: os.environ.get(k) for k in wanted}
    os.environ.update(wanted)
    return saved


def _restore(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def run(doc: dict, engine: str = stub.ENGINE, runs: int = 1, out="eval-out", answers=None,
        price_model: str = "", tags=(), ids=(), label: str = "", model: str = "",
        max_tokens: str = "", progress=None) -> dict:
    """Dispatch every selected case `runs` times; write results.jsonl and summary.json
    under `out`; return the summary."""
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    selected = _cases.select(doc["cases"], tags, ids)
    if not selected:
        raise _cases.CaseError("no case matches the requested tags or ids")
    if runs < 1:
        raise _cases.CaseError("--runs must be at least 1")
    say = progress or (lambda text: None)
    records = []
    saved_env = _environment(engine, model, max_tokens, out)
    restore_llm = None
    toggled = []
    try:
        with (stub.installed(answers) if engine == stub.ENGINE else _nothing()) as stubbed:
            from finrail.core import dispatch_core, registry
            registry.load(force=True)
            for name in _cases.CAPABILITIES:
                cap = registry.command(name)
                if cap.background:
                    cap.background = False
                    toggled.append(cap)
            if stubbed:
                # The module the registry's capabilities actually read from (see stub.installed).
                for mod in {sys.modules[type(registry.command(n)).__module__] for n in _cases.CAPABILITIES}:
                    stubbed["filings"].patch(mod)
            restore_llm = _wrap_llm()
            offset = 0
            results_path = out / "results.jsonl"
            with open(results_path, "w", encoding="utf-8") as fh:
                for n in range(1, runs + 1):
                    for case in selected:
                        stub.CURRENT["case"] = case["id"]
                        stub.CURRENT["key"] = (f"{case['report_id']}:"
                                               f"{case['params'].get('definition') or 'search'}")
                        _INJECT["tamper"] = (case.get("inject") or {}).get("tamper", "")
                        _INJECT["report_id"] = case["report_id"]
                        payload = {"report_id": case["report_id"], **case["params"]}
                        started = time.perf_counter()
                        exc = result = None
                        try:
                            result = dispatch_core.dispatch(case["capability"], payload, source="eval")
                        except Exception as e:                          # noqa: BLE001
                            exc = e
                        wall = time.perf_counter() - started
                        outcome, code, message = classify(exc, result)
                        amount = _amount(result) if outcome == "passed" else ""
                        if outcome == "passed" and case["expect"]["outcome"] == "figure" \
                                and amount != str(case["expect"]["amount"]):
                            outcome = "wrong_figure"
                        met = meets(case["expect"], outcome, amount, code, message)
                        calls, offset = _model_calls(offset)
                        rec = {"case": case["id"], "title": case["title"], "run": n,
                               "report_id": case["report_id"], "capability": case["capability"],
                               "params": case["params"], "inject": _INJECT["tamper"] or None,
                               "draft": bool(case.get("draft")), "expect": case["expect"],
                               "outcome": outcome, "met": met, "error_code": code,
                               "message": (message or "")[:400], "amount": amount or None,
                               "run_id": (result or {}).get("id") if isinstance(result, dict) else None,
                               "latency_s": round(wall, 3), **_usage(calls, price_model)}
                        records.append(rec)
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        say(f"{'ok ' if met else 'MISS'} {case['id']} run {n}: {outcome}"
                            + (f" {amount}" if amount else "") + f" in {wall:.1f}s")
    finally:
        _INJECT["tamper"] = ""
        if restore_llm:
            restore_llm()
        for cap in toggled:
            cap.background = True
        _restore(saved_env)
    summary = summarise(records, {
        "engine": engine, "model": model or (records[0]["model"] if records else ""),
        "label": label or engine, "runs_per_case": runs, "cases": len(selected),
        "cases_name": doc.get("name", ""), "price_model": price_model,
        "max_tokens": str(max_tokens or os.environ.get("FINRAIL_ENGINE_MAX_TOKENS") or ""),
        "at": datetime.datetime.now().isoformat(timespec="seconds"), "git": _git(),
        "provenance": {"kind": "stub" if engine == stub.ENGINE else "run",
                       "note": ("Stub engine answering from a recorded-answers file over synthetic "
                                "filings. No live engine run was made." if engine == stub.ENGINE
                                else f"Live run on engine {engine}.")}})
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    return summary


class _nothing:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# ── the summary ──────────────────────────────────────────────────────────────
def summarise(records: list, meta: dict) -> dict:
    calls = len(records)
    met = sum(1 for r in records if r["met"])
    counts = {k: sum(1 for r in records if r["outcome"] == k) for k in OUTCOMES}
    answer_cases = [r for r in records if r["expect"]["outcome"] in ("passed", "figure")]
    adversarial = [r for r in records if r["expect"]["outcome"] == "refused"]
    passed = sum(1 for r in answer_cases if r["outcome"] == "passed")
    low, high = stats.wilson(met, calls)
    priced = [r["cost_usd_list"] for r in records if r.get("cost_usd_list") is not None]
    ledger = [r["cost_usd_ledger"] for r in records if r.get("cost_usd_ledger") is not None]
    total_list = round(sum(priced), 6) if priced else None
    by_case = {}
    for r in records:
        c = by_case.setdefault(r["case"], {"title": r["title"], "runs": 0, "met": 0, "passed": 0,
                                           "draft": r["draft"], "expect": r["expect"],
                                           "latencies": [], "amounts": set(), "outcomes": {}})
        c["runs"] += 1
        c["met"] += int(r["met"])
        c["passed"] += int(r["outcome"] == "passed")
        c["latencies"].append(r["latency_s"])
        c["outcomes"][r["outcome"]] = c["outcomes"].get(r["outcome"], 0) + 1
        if r.get("amount"):
            c["amounts"].add(r["amount"])
    for c in by_case.values():
        c["median_s"] = round(stats.median(c["latencies"]), 1)
        c["amounts"] = sorted(c.pop("amounts"))
        c.pop("latencies")
    return {
        "schema": SUMMARY_SCHEMA, **{k: v for k, v in meta.items() if k != "provenance"},
        "provenance": meta.get("provenance") or {},
        "calls": calls, "met": met, "pass_rate": round(met / calls, 4) if calls else 0.0,
        "wilson_95": [round(low, 4), round(high, 4)],
        "checks": {"answer_calls": len(answer_cases), "answer_passed": passed,
                   "answer_refused": sum(1 for r in answer_cases if r["outcome"] == "refused"),
                   "adversarial_calls": len(adversarial),
                   "adversarial_refused": sum(1 for r in adversarial if r["met"])},
        "outcomes": counts,
        "latency_s": {"median": round(stats.median([r["latency_s"] for r in records]), 2),
                      "p90": round(stats.percentile([r["latency_s"] for r in records], 0.9), 2),
                      "mean": round(stats.mean([r["latency_s"] for r in records]), 2)},
        "tokens": {"input_mean": round(stats.mean([r["input_tokens"] for r in records]), 1),
                   "output_mean": round(stats.mean([r["output_tokens"] for r in records]), 1)},
        "cost_usd": {"total_list": total_list,
                     "per_call_list": round(total_list / calls, 6) if total_list is not None and calls else None,
                     "per_checked_answer_list": (round(total_list / passed, 6)
                                                 if total_list is not None and passed else None),
                     "total_ledger": round(sum(ledger), 6) if ledger else None},
        "by_case": by_case,
        "drafts": sorted({r["case"] for r in records if r["draft"]}),
        "no_live_run": meta.get("engine") == stub.ENGINE,
    }
