"""The evaluation harness, offline: case validation, a stub run's numbers, compare and report."""
import copy
import json
import pathlib

import pytest

from finrail.eval import __main__ as cli
from finrail.eval import cases, compare, report, runner, stats, stub

CASES = pathlib.Path(__file__).resolve().parents[1] / "finrail" / "eval" / "cases"
BASELINES = CASES.parent / "baselines"


def _case(**changes):
    base = {"id": "nvda-q1-fy2027-fcf", "title": "NVIDIA Q1 FY2027 free cash flow", "report_id": "NVDA-Q1-FY2027",
            "capability": "finance.reports.analyze", "params": {"definition": "issuer_fcf"},
            "expect": {"outcome": "figure", "amount": "48554"}, "tags": ["analyze"], "draft": False}
    base.update(changes)
    return base


def _doc(*items):
    return {"schema": cases.SCHEMA, "name": "t", "cases": list(items)}


# ── the case format ──────────────────────────────────────────────────────────
def test_committed_case_files_validate_and_count():
    seven = cases.load(CASES / "model_comparison_7.json")
    assert [c["id"] for c in seven["cases"]] == [
        "nvda-q1-fy2027-fcf", "nvda-q2-fy2027-fcf", "anth-s1-revenue", "anth-s1-net-loss",
        "anth-s1-operating-cash-flow", "anth-s1-capex", "anth-s1-house-operating-income"]
    assert all(c["expect"]["outcome"] == "figure" and not c["draft"] for c in seven["cases"])
    thirty_six = cases.load(CASES / "benchmark_36.json")
    assert len(thirty_six["cases"]) == 36
    assert sorted(c["id"] for c in thirty_six["cases"] if c["draft"]) == [
        "anth-s1-simple-fcf", "nvda-q1-fy2027-simple-fcf", "nvda-q2-fy2027-simple-fcf"]
    kinds = [c.get("inject", {}).get("tamper") for c in thirty_six["cases"] if c.get("inject")]
    assert (kinds.count("other_column"), kinds.count("invented_row"), kinds.count("drop_input"),
            kinds.count("wrong_report")) == (10, 3, 6, 3)
    assert sum(1 for c in thirty_six["cases"] if "plan" in c["tags"]) == 4
    assert {c["id"] for c in seven["cases"]} <= {c["id"] for c in thirty_six["cases"]}
    assert len(cases.load(CASES / "search_2.json")["cases"]) == 2


@pytest.mark.parametrize("change,fragment", [
    ({"id": "Bad Id"}, "kebab-case"),
    ({"capability": "finance.reports.record"}, "capability"),
    ({"params": {"definition": "issuer_fcf", "report_id": "X"}}, "report_id"),
    ({"params": {}}, "params.definition"),
    ({"expect": {"outcome": "maybe"}}, "expect.outcome"),
    ({"expect": {"outcome": "figure"}}, "expect.amount"),
    ({"expect": {"outcome": "figure", "amount": "48,554"}}, "expect.amount"),
    ({"expect": {"outcome": "passed", "amount": "1"}}, "figure"),
    ({"expect": {"outcome": "refused"}}, "reason"),
    ({"report_id": "NVDA-Q9-FY2027"}, "not in the catalog"),
    ({"inject": {"tamper": "swap_sign"}, "expect": {"outcome": "refused", "reason": "x"}}, "inject.tamper"),
    ({"inject": {"tamper": "drop_input"}}, "must expect a refusal"),
    ({"tags": "analyze"}, "tags"),
    ({"draft": "yes"}, "draft"),
])
def test_a_malformed_case_is_refused_by_name(change, fragment):
    with pytest.raises(cases.CaseError, match=fragment) as e:
        cases.validate(_doc(_case(**change)))
    assert "nvda-q1-fy2027-fcf" in str(e.value) or "#1" in str(e.value)


def test_malformed_files_and_duplicates_are_refused(tmp_path):
    with pytest.raises(cases.CaseError, match="schema"):
        cases.validate({"cases": [_case()]})
    with pytest.raises(cases.CaseError, match="duplicate"):
        cases.validate(_doc(_case(), _case()))
    with pytest.raises(cases.CaseError, match="non-empty"):
        cases.validate(_doc())
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(cases.CaseError, match="not JSON"):
        cases.load(bad)
    assert cli.main(["validate", str(bad)]) == 2
    assert cases.select([_case(), _case(id="other", tags=["x"])], tags=["x"]) == [_case(id="other", tags=["x"])]


# ── the stub run and its summary ─────────────────────────────────────────────
def test_stub_run_of_the_seven_tasks_produces_the_documented_figures(tmp_path):
    doc = cases.load(CASES / "model_comparison_7.json")
    summary = runner.run(doc, engine="stub", runs=2, out=tmp_path / "out",
                         price_model="nvidia/nemotron-3-super-120b-a12b", label="stub")
    assert (summary["calls"], summary["met"], summary["pass_rate"]) == (14, 14, 1.0)
    assert summary["wilson_95"] == [round(stats.wilson(14, 14)[0], 4), 1.0]
    assert summary["outcomes"]["passed"] == 14 and summary["outcomes"]["wrong_figure"] == 0
    assert summary["checks"] == {"answer_calls": 14, "answer_passed": 14, "answer_refused": 0,
                                 "adversarial_calls": 0, "adversarial_refused": 0}
    assert {cid: c["amounts"] for cid, c in summary["by_case"].items()} == {
        "nvda-q1-fy2027-fcf": ["48554"], "nvda-q2-fy2027-fcf": ["21341"], "anth-s1-revenue": ["16286"],
        "anth-s1-net-loss": ["80748"], "anth-s1-operating-cash-flow": ["3929"], "anth-s1-capex": ["482"],
        "anth-s1-house-operating-income": ["-567"]}
    # Tokens and cost come from the ledger's model_call events, priced at list.
    assert summary["tokens"] == {"input_mean": 1469.0, "output_mean": 1050.0}
    assert summary["cost_usd"]["per_call_list"] == pytest.approx(0.0013857, abs=1e-6)
    assert summary["cost_usd"]["per_checked_answer_list"] == summary["cost_usd"]["per_call_list"]
    assert summary["no_live_run"] is True and summary["provenance"]["kind"] == "stub"
    assert summary["drafts"] == [] and summary["engine"] == "stub"
    rows = [json.loads(l) for l in (tmp_path / "out" / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 14 and all(r["model_calls"] == 1 and r["run_id"] for r in rows)
    assert all(r["input_tokens"] == 1469 and r["output_tokens"] == 1050 and r["model_ms"] >= 0 for r in rows)
    # The ledger under the run's own state holds one model_call per dispatch, and nothing leaked out.
    ledger = (tmp_path / "out" / "state" / "events" / "_ledger.jsonl").read_text().splitlines()
    assert sum(1 for line in ledger if line.strip() and json.loads(line).get("kind") == "model_call") == 14
    assert json.loads((tmp_path / "out" / "summary.json").read_text())["met"] == 14


def test_a_second_execution_in_the_same_out_dir_counts_only_its_own_model_calls(tmp_path):
    doc = _doc(_case())
    first = runner.run(doc, engine="stub", runs=1, out=tmp_path / "out")
    second = runner.run(doc, engine="stub", runs=1, out=tmp_path / "out")
    rows = [json.loads(ln) for ln in (tmp_path / "out" / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["model_calls"] == 1
    assert rows[0]["input_tokens"] == 1469 and rows[0]["output_tokens"] == 1050
    assert second["tokens"]["input_mean"] == first["tokens"]["input_mean"] == 1469


def test_stub_run_of_the_benchmark_refuses_every_adversarial_case(tmp_path):
    doc = cases.load(CASES / "benchmark_36.json")
    summary = runner.run(doc, engine="stub", runs=1, out=tmp_path / "out")
    assert (summary["calls"], summary["met"]) == (36, 36)
    assert summary["checks"]["answer_calls"] == 10 and summary["checks"]["answer_passed"] == 10
    assert summary["checks"]["adversarial_calls"] == 26 and summary["checks"]["adversarial_refused"] == 26
    assert summary["outcomes"]["refused"] == 26 and summary["outcomes"]["wrong_figure"] == 0
    assert summary["drafts"] == ["anth-s1-simple-fcf", "nvda-q1-fy2027-simple-fcf", "nvda-q2-fy2027-simple-fcf"]
    rows = {r["case"]: r for r in map(json.loads, (tmp_path / "out" / "results.jsonl").read_text().splitlines())}
    # A refusal by the plan (method rule, unsupported definition, unknown report) reaches no model.
    for cid in ("anth-s1-adjusted-operating-income-m1", "nvda-q1-fy2027-revenue-unsupported",
                "anth-s1-issuer-fcf-unsupported", "nvda-q3-fy2027-unknown-report"):
        assert rows[cid]["model_calls"] == 0 and rows[cid]["outcome"] == "refused"
    assert "M-1" in rows["anth-s1-adjusted-operating-income-m1"]["message"]
    # A tampered answer reaches the checks and is refused there, after one model call.
    assert rows["anth-s1-net-loss-other-column"]["model_calls"] == 1
    assert "Source-cell check refused net_loss" in rows["anth-s1-net-loss-other-column"]["message"]
    assert "not present in the retrieved evidence" in rows["anth-s1-revenue-invented-row"]["message"]
    assert "missing required inputs" in rows["nvda-q2-fy2027-fcf-drop-input"]["message"]
    assert "did not match the selected report" in rows["nvda-q1-fy2027-fcf-wrong-report"]["message"]
    assert rows["nvda-q2-fy2027-simple-fcf"]["amount"] == "21400" and rows["anth-s1-simple-fcf"]["amount"] == "3447"


def test_a_cut_off_reply_a_wrong_figure_and_a_search_pick_are_counted(tmp_path):
    answers = json.loads((CASES / "stub_answers.json").read_text())
    answers["answers"]["cut"] = {"empty": True}
    answers_path = tmp_path / "answers.json"
    answers_path.write_text(json.dumps(answers))
    doc = _doc(_case(id="cut"),
               _case(id="wrong", expect={"outcome": "figure", "amount": "1"}),
               _case(id="refuse-expected", expect={"outcome": "refused", "code": "EMPTY_RESPONSE"}),
               {"id": "search", "title": "search", "report_id": "ANTH-S1-2026", "capability": "finance.reports.search",
                "params": {"query": "What does the filing say about compute?"}, "expect": {"outcome": "passed"},
                "tags": ["search"], "draft": True})
    summary = runner.run(doc, engine="stub", out=tmp_path / "out", answers=answers_path)
    rows = {r["case"]: r for r in map(json.loads, (tmp_path / "out" / "results.jsonl").read_text().splitlines())}
    assert rows["cut"]["outcome"] == "cut_off" and rows["cut"]["error_code"] == "EMPTY_RESPONSE" and not rows["cut"]["met"]
    assert rows["wrong"]["outcome"] == "wrong_figure" and rows["wrong"]["amount"] == "48554" and not rows["wrong"]["met"]
    assert rows["refuse-expected"]["outcome"] == "passed" and not rows["refuse-expected"]["met"]
    assert rows["search"]["outcome"] == "passed" and rows["search"]["met"] and rows["search"]["input_tokens"] == 900
    assert summary["outcomes"]["cut_off"] == 1 and summary["outcomes"]["wrong_figure"] == 1
    assert (summary["met"], summary["calls"]) == (1, 4) and summary["drafts"] == ["search"]
    # The stub answered from the file over the same HTTP seam a real engine uses.
    assert rows["search"]["model"] == "stub" and rows["cut"]["model_calls"] == 1


def test_tampers_make_the_miss_they_name():
    answer = {"report_id": "NVDA-Q1-FY2027", "inputs": [
        {"metric": "operating_cash_flow", "value": "50344", "row_id": "E1:L1"},
        {"metric": "cash_purchases", "value": "1757", "row_id": "E1:L2"}]}
    ctx = {"retrieved_rows": json.dumps([
        {"row_id": "E1:L1", "quote": "Net cash provided by operating activities |  | 50,344 |  | 27,414"}])}
    assert runner.tamper("other_column", answer, ctx, "NVDA-Q1-FY2027")["inputs"][0]["value"] == "27414"
    assert runner.tamper("invented_row", answer, ctx, "NVDA-Q1-FY2027")["inputs"][0]["row_id"] == "E9:L99"
    assert len(runner.tamper("drop_input", answer, ctx, "NVDA-Q1-FY2027")["inputs"]) == 1
    assert runner.tamper("wrong_report", answer, ctx, "NVDA-Q1-FY2027")["report_id"] != "NVDA-Q1-FY2027"
    assert answer["inputs"][0]["value"] == "50344"          # the original is never changed


def test_wilson_reproduces_the_documents_intervals():
    assert tuple(round(x, 2) for x in stats.wilson(67, 70)) == (0.88, 0.99)
    assert tuple(round(x, 2) for x in stats.wilson(70, 70)) == (0.95, 1.0)
    assert tuple(round(x, 2) for x in stats.wilson(35, 35)) == (0.90, 1.0)
    assert tuple(round(x, 2) for x in stats.wilson(66, 70)) == (0.86, 0.98)
    assert stats.wilson(0, 0) == (0.0, 0.0) and stats.median([3, 1, 2]) == 2 and stats.percentile([1, 2, 3, 4], 0.9) == 3.7
    assert stats.list_cost("nvidia/nemotron-3-super-120b-a12b", 1469, 1050) == pytest.approx(0.0013857)
    assert stats.list_cost("unknown-model", 1, 1) is None


# ── baselines, compare, report ───────────────────────────────────────────────
def test_baselines_are_transcriptions_of_the_document():
    arms = {p.stem: compare.load(p) for p in BASELINES.glob("*.json")}
    assert set(arms) == {"nemotron3", "nemotron35_32k", "nemotron35_8k", "opus55"}
    assert {k: (v["met"], v["calls"]) for k, v in arms.items()} == {
        "nemotron3": (67, 70), "nemotron35_32k": (70, 70), "nemotron35_8k": (66, 70), "opus55": (35, 35)}
    for arm in arms.values():
        assert arm["provenance"]["kind"] == "transcribed" and "not re-run" in arm["provenance"]["note"].lower()
        assert arm["outcomes"]["wrong_figure"] == 0 and len(arm["by_case"]) == 7
    assert arms["nemotron3"]["wilson_95"] == [round(stats.wilson(67, 70)[0], 4), round(stats.wilson(67, 70)[1], 4)]
    assert arms["nemotron35_8k"]["outcomes"]["cut_off"] == 4 and arms["nemotron3"]["outcomes"]["refused"] == 3


def test_compare_flags_a_pass_rate_outside_the_interval_and_a_wrong_figure(tmp_path):
    base = compare.load(BASELINES / "nemotron35_32k.json")
    same = copy.deepcopy(base)
    assert compare.compare(same, base)["ok"]
    worse = copy.deepcopy(base)
    worse["met"], worse["pass_rate"] = 60, 60 / 70
    worse["outcomes"]["passed"], worse["outcomes"]["refused"] = 60, 10
    result = compare.compare(worse, base)
    assert not result["ok"] and "below the baseline's 95% interval" in result["regressions"][0]
    noise = copy.deepcopy(base)
    noise["met"], noise["pass_rate"] = 68, 68 / 70       # inside 95% to 100%: not a regression
    assert compare.compare(noise, base)["ok"]
    shipped = copy.deepcopy(base)
    shipped["outcomes"]["wrong_figure"] = 1
    assert "wrong figure" in compare.compare(shipped, base)["regressions"][0]
    (tmp_path / "worse.json").write_text(json.dumps(worse))
    (tmp_path / "same.json").write_text(json.dumps(same))
    assert cli.main(["compare", str(tmp_path / "worse.json"), str(BASELINES / "nemotron35_32k.json")]) == 1
    assert cli.main(["compare", str(tmp_path / "same.json"), str(BASELINES / "nemotron35_32k.json")]) == 0
    text = compare.render(worse, base, result)
    assert "REGRESSION" in text and "Expected outcome met | 85.7% | 100.0% | -14.3 pts" in text


def test_report_renders_the_documents_tables_and_names_drafts(tmp_path):
    arms = [compare.load(BASELINES / n) for n in ("nemotron3.json", "nemotron35_32k.json", "opus55.json")]
    text = report.render(arms, title="Nemotron vs Opus")
    assert text.startswith("# Nemotron vs Opus")
    assert "| Expected outcome met | **67 / 70** (96%) | **70 / 70** (100%) | **35 / 35** (100%) |" in text
    assert "| 95% interval (Wilson) | 88% to 99% | 95% to 100% | 90% to 100% |" in text
    assert "| Refused by the checks | 3 | 0 | 0 |" in text
    assert "| Latency, median / p90 | 5.3 s / 9.6 s | 12.5 s / 26.3 s | 3.9 s / 6.0 s |" in text
    assert "| **Cost per checked answer, list** | **$0.00145** | **$0.00443** | **$0.02454** |" in text
    assert "| Per 1,000 checked answers | $1.45 | $4.43 | $24.54 |" in text
    assert "| Anthropic S-1 net loss | **8/10 · 5.2 s** | 10/10 · 13.1 s | 5/5 · 3.0 s | 80,748 |" in text
    assert "| Anthropic S-1 method-rule operating result (M-1) | 10/10 · 5.0 s | 10/10 · 14.6 s | 5/5 · 5.6 s | -567 |" in text
    assert "No draft cases" in text and "No live engine run was made" in text
    # A stub summary with drafts and adversarial cases adds its rows and names every draft.
    records = [
        {"case": "a", "title": "A", "run": 1, "draft": True, "expect": {"outcome": "figure", "amount": "5"},
         "outcome": "passed", "met": True, "amount": "5", "latency_s": 1.0, "input_tokens": 10, "output_tokens": 5,
         "cost_usd_list": 0.001, "cost_usd_ledger": None},
        {"case": "b", "title": "B tampered", "run": 1, "draft": False, "expect": {"outcome": "refused", "reason": "x"},
         "outcome": "refused", "met": True, "amount": "", "latency_s": 2.0, "input_tokens": 10, "output_tokens": 5,
         "cost_usd_list": 0.001, "cost_usd_ledger": None}]
    summary = runner.summarise(records, {"engine": "stub", "label": "stub", "runs_per_case": 1, "cases": 2,
                                         "provenance": {"kind": "stub", "note": "stub"}})
    text = report.render([summary])
    assert "| Adversarial picks refused | 1 / 1 |" in text and "- `a`" in text and "| A (draft) |" in text
    assert "| B tampered | 1/1 · 2.0 s | refused by design |" in text
    out = tmp_path / "r.md"
    (tmp_path / "s.json").write_text(json.dumps(summary))
    assert cli.main(["report", str(tmp_path / "s.json"), "--out", str(out)]) == 0
    assert out.read_text() == report.render([summary])


def test_stub_answers_file_is_well_formed():
    doc = stub.load_answers()
    assert doc["schema"] == stub.ANSWERS_SCHEMA
    keys = set(doc["answers"])
    for sid, definitions in (("NVDA-Q1-FY2027", ("issuer_fcf", "simple_fcf")), ("NVDA-Q2-FY2027", ("issuer_fcf", "simple_fcf")),
                             ("ANTH-S1-2026", ("revenue", "net_loss", "operating_cash_flow", "capex", "simple_fcf",
                                               "house_operating_income"))):
        assert {f"{sid}:{d}" for d in definitions} <= keys and f"{sid}:search" in keys
    with pytest.raises(ValueError, match="recorded-answers"):
        stub.load_answers(CASES / "model_comparison_7.json")
