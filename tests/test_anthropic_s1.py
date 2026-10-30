"""Anthropic's S-1 as the seventh source: offline contracts, no credentials, no RAG.

The fixture reproduces the shape of the archived filing's statement tables (column-span
headers with fiscal years first, then the six-month comparative, then the current six
months; the unit line in the caption above the table) at the same table positions the
profile declares, so the column, sign, reconciliation and extraction rules are exercised
exactly as they run on the real bytes.
"""
import copy
import hashlib
from decimal import Decimal

import pytest

from finrail.core.errors import ContractViolation
from finrail.extensions.finance import ask
from finrail.extensions.finance import public_reports as r

REPORT = "ANTH-S1-2026"
PERIODS = ('<tr><td></td><td colspan="5">Year Ended December 31,</td><td></td><td colspan="3">Six Months Ended June 30,</td></tr>')
YEARS = ('<tr><td>{unit}</td><td>2023</td><td></td><td>2024</td><td></td><td>2025</td><td></td><td>2025</td><td></td><td>2026</td></tr>'
         '<tr><td></td><td colspan="5"></td><td></td><td colspan="3">(unaudited)</td></tr>')


def _row(label, *values):
    cells = "".join(f"<td>{v}</td><td></td>" for v in values).removesuffix("<td></td>")
    return f"<tr><td>{label}</td>{cells}</tr>"


def _line(text):
    return f'<div style="line-height:7pt;position:absolute">{text}</div>'


def _table(caption_lines, unit, *rows):
    return "".join(_line(t) for t in caption_lines) + "<table>" + PERIODS + YEARS.format(unit=unit) + "".join(rows) + "</table>"


def s1_html(*, year="2026", revenue="$ 16,286", total="$ 16,286", unit_caption=True, adjusted="$ 1,618", charitable="126") -> bytes:
    unit = "(In millions, except share amounts which are in thousands, and per share data)" if unit_caption else "(per share data)"
    operations = _table(["F-7", "Anthropic, PBC", "Consolidated Statements of Operations", unit], "",
                        _row("Revenue", "$ 41", "$ 386", "$ 4,585", "$ 1,163", revenue),
                        _row("Operating expenses:"), _row("Total operating expenses", "592", "3,366", "12,645", "3,298", "16,979"),
                        _row("Change in fair value of convertible notes, derivative liabilities, and SAFEs",
                             "(1,045)", "(5,434)", "(34,308)", "(10,942)", "(72,388)"),
                        _row("Net loss", "$ (1,531)", "$ (8,309)", "$ (41,965)", "$ (12,969)", "$ (80,748)"),
                        _row("Net loss per share attributable to common stockholders, basic and diluted", "$ (1.42)", "$ (5.43)", "$ (23.04)", "$ (7.25)", "$ (39.88)"))
    cash = _table(["F-12", "Anthropic, PBC", "Consolidated Statements of Cash Flows", "(In millions)" if unit_caption else ""], "",
                  _row("Cash flows from operating activities:"),
                  _row("Net loss", "$ (1,531)", "$ (8,309)", "$ (41,965)", "$ (12,969)", "$ (80,748)"),
                  _row("Net cash provided by (used in) operating activities", "(1,627)", "(5,692)", "(2,637)", "(1,513)", "3,929"),
                  _row("Cash flows from investing activities:"),
                  _row("Purchases of property and equipment", "(2)", "(15)", "(131)", "(18)", "(482)"))
    summary = _table(["Cash Flows", "The following table shows a summary of our cash flows for the periods presented:"], "(in millions)",
                     _row("Net cash provided by (used in)"),
                     _row("Operating activities", "$ (1,627)", "$ (5,692)", "$ (2,637)", "$ (1,513)", "$ 3,929"))
    note = _table(["The following table presents consumption-based and subscription-based revenue (in millions):"], "",
                  _row("Consumption-based", "$ 39", "$ 288", "$ 3,796", "$ 983", "$ 13,064"),
                  _row("Total", "$ 41", "$ 386", "$ 4,585", "$ 1,163", total))
    bridge = _table(["Adjusted Operating Income (Loss)", "The following table presents a reconciliation of operating loss:"],
                    "(in millions, except for percentages)",
                    _row("Operating loss", "$ (551)", "$ (2,980)", "$ (8,060)", "$ (2,135)", "$ (693)"),
                    _row("Stock-based compensation expense", "29", "192", "1,165", "286", "2,185"),
                    _row("Non-cash charitable contributions (1)", "—", "—", "541", "—", charitable),
                    _row("Bartz settlement (2)", "—", "—", "1,500", "—", "—"),
                    _row("Adjusted Operating Income (Loss)", "$ (522)", "$ (2,788)", "$ (4,854)", "$ (1,849)", adjusted))
    parts = []
    for number in range(1, 121):
        parts.append({61: bridge, 72: summary, 109: operations, 114: cash, 120: note}.get(number, "<table></table>"))
    page = '<div style="height:792pt;width:612pt">' + _line("Table of Contents") + _line("including to fund our substantial compute commitments, and our ability") + "</div>"
    html = "<html><body>" + page + '<div style="height:792pt;width:612pt">' + "".join(parts) + "</div></body></html>"
    return html.replace("2026", year).encode()


def fixture(definition="revenue", **changes):
    item = copy.deepcopy(r.report(REPORT))
    raw = s1_html(**changes)
    item["sha256"] = hashlib.sha256(raw).hexdigest()
    cells = r.source_cells(item, raw)
    names = r.labels(r.PROFILES["Anthropic"])
    lines = {key: number for number, key in enumerate(names, 1)}
    evidence = [{"id": "E1", "text": "\n".join(cells[key]["source_row"] for key in names), "source_uri": item["indexed_uris"][0]}]
    found = {"report_id": REPORT, "evidence": evidence, "backend": "test stub"}
    terms = r.PROFILES["Anthropic"]["definitions"][definition]["terms"]
    candidate = {"report_id": REPORT, "period_end": item["period_end"], "currency": "USD", "unit": "million", "definition": definition,
                 "inputs": [{"metric": key, "value": cells[key]["value"], "row_id": f"E1:L{lines[key]}"} for _sign, key in terms]}
    return item, raw, cells, found, candidate


def test_catalog_entry_is_the_edgar_primary_document():
    item = r.report(REPORT)
    assert item["company"] == "Anthropic" and item["format"] == "html" and item["period_end"] == "2026-06-30"
    assert item["sha256"] == "2fa24ae7704dbb535d096ee7459b765fe899ac9118762e738183a94473c9905a"
    assert item["url"].startswith("https://www.sec.gov/Archives/edgar/data/0002130357/000162828026067168/")
    assert item["edgar_accession"] == "0001628280-26-067168"
    assert item["rag_file_ids"] == ["5820050344834778841"] and item["indexed_uris"] == [f"gs://{item['bucket']}/{item['parsed_object']}"]
    assert item["object_generation"] and item["parsed_object_generation"]
    assert item["object"].startswith("2026-10-29-anthropic/") and len(r.catalog()) == 7


def test_six_month_2026_column_is_selected_not_the_comparative():
    _item, _raw, cells, _found, _p = fixture()
    assert {k: cells[k]["value"] for k in ("revenue", "net_loss", "operating_cash_flow", "cash_purchases")} == {
        "revenue": "16286", "net_loss": "80748", "operating_cash_flow": "3929", "cash_purchases": "482"}
    assert cells["revenue_total"]["value"] == "16286" and cells["cash_flow_net_loss"]["value"] == "80748"
    assert cells["summary_operating_cash_flow"]["value"] == "3929"
    for cell in cells.values():
        assert "Six Months Ended June 30," in cell["column"] and "2026" in cell["column"]
        assert cell["html_cell"] == 10
    assert "unaudited" in cells["revenue"]["column"] and cells["net_loss"]["sign"] == "-"
    assert cells["revenue"]["table"] == 109 and cells["cash_purchases"]["table"] == 114


@pytest.mark.parametrize("value", ["1163", "4585", "41", "16287"])
def test_comparative_fiscal_year_or_altered_values_are_refused(value):
    item, _raw, cells, found, p = fixture()
    p["inputs"][0]["value"] = value
    with pytest.raises(ContractViolation, match="Source-cell check refused"):
        r.check_and_compute(item, "revenue", p, found, cells)


@pytest.mark.parametrize("definition,amount,reconciled", [
    ("revenue", "16286", "Total"), ("net_loss", "80748", "Net loss"),
    ("operating_cash_flow", "3929", "Operating activities"), ("capex", "482", None), ("simple_fcf", "3447", None)])
def test_each_definition_is_decimal_arithmetic_on_checked_cells(definition, amount, reconciled):
    item, _raw, cells, found, p = fixture(definition)
    out = r.check_and_compute(item, definition, p, found, cells)
    assert out["amount"] == amount and out["currency"] == "USD" and out["unit"] == "million"
    assert (out["reconciled"] or {}).get("label") == reconciled
    assert "reported_fcf" not in out
    if definition == "simple_fcf":
        assert out["formula"] == "operating_cash_flow - cash_purchases"
        assert "does not define free cash flow" in out["metric"] and "no free cash flow" in out["reconciliation"]
    if definition == "capex":
        assert "nowhere else" in out["reconciliation"]


def test_a_figure_that_does_not_reconcile_to_the_filing_is_refused():
    item, _raw, cells, found, p = fixture()
    cells["revenue_total"]["value"] = "16287"
    with pytest.raises(ContractViolation, match="reconcile"):
        r.check_and_compute(item, "revenue", p, found, cells)
    item, raw, cells, found, p = fixture(total="$ 16,285")
    with pytest.raises(ContractViolation, match="reconcile"):
        r.check_and_compute(item, "revenue", p, found, cells)


def test_wrong_definition_report_or_period_is_refused():
    item, _raw, cells, found, p = fixture()
    with pytest.raises(ContractViolation, match="Unsupported definition"):
        r.check_and_compute(item, "issuer_fcf", p, found, cells)
    with pytest.raises(ContractViolation, match="did not match"):
        r.check_and_compute(item, "revenue", {**p, "period_end": "2025-06-30"}, found, cells)
    with pytest.raises(ContractViolation, match="did not match"):
        r.check_and_compute(item, "revenue", {**p, "report_id": "NVDA-Q2-FY2027"}, found, cells)


def test_header_without_the_period_year_or_unit_is_refused():
    item = copy.deepcopy(r.report(REPORT))
    for changes, message in (({"year": "2027"}, "year header is missing"), ({"unit_caption": False}, "unit header")):
        raw = s1_html(**changes)
        item["sha256"] = hashlib.sha256(raw).hexdigest()
        with pytest.raises(ContractViolation, match=message):
            r.source_cells(item, raw)
    raw = s1_html()
    with pytest.raises(ContractViolation, match="SHA-256"):
        r.source_cells(item, raw)
    raw = s1_html(revenue="$ (16,286)")
    item["sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ContractViolation, match="sign"):
        r.source_cells(item, raw)


def test_cell_under_requires_exactly_one_period_and_year_column():
    period, year = "Six Months Ended June 30,", "2026"
    rows = [["", "Year Ended December 31,", "", period], ["", "2025", "2026", "2025", "2026"], ["Revenue", "$ 1", "$ 2", "$ 3", "$ 4"]]
    spans = [[1, 2, 1, 2], [1, 1, 1, 1, 1], [1, 1, 1, 1, 1]]
    assert r._cell_under(rows, spans, "Revenue", period, year)["signed_value"] == 4
    twice = [rows[0], ["", "2025", "2025", "", "2026", "2026"], rows[2]]
    with pytest.raises(ContractViolation, match="exactly one 2026 column"):
        r._cell_under(twice, [spans[0], [1] * 6, spans[2]], "Revenue", period, year)
    both = [["", period, "", period], rows[1], rows[2]]
    with pytest.raises(ContractViolation, match="exactly one 'Six Months"):
        r._cell_under(both, spans, "Revenue", period, year)
    dash = [rows[0], rows[1], ["Revenue", "$ 1", "$ 2", "$ 3", "—"]]
    with pytest.raises(ContractViolation, match="not a number"):
        r._cell_under(dash, spans, "Revenue", period, year)
    with pytest.raises(ContractViolation, match="Expected one source row"):
        r._cell_under(rows + [rows[2]], spans + [spans[2]], "Revenue", period, year)


def test_tamper_replays_are_refused_by_the_checker():
    item, _raw, cells, found, p = fixture()
    value = r.check_and_compute(item, "revenue", p, found, cells)
    assert r.replay(value)["amount"] == "16286"
    for attack in r.ATTACKS:
        out = r.replay(value, attack)
        assert out["refused"] and out["refused_by"] == "check_and_compute", attack


def test_extraction_keeps_document_order_tables_and_pages_without_computed_figures():
    text = r.extract(s1_html(), REPORT, "Anthropic")
    assert text.startswith(f"\nREPORT {REPORT}; PAGE 1\n")
    assert "substantial compute commitments" in text
    caption = text.index("Consolidated Statements of Operations")
    marker = text.index(f"REPORT {REPORT}; HTML TABLE 109; rows 1-")
    row = text.index("Revenue | $ 41 | $ 386 | $ 4,585 | $ 1,163 | $ 16,286")
    assert caption < marker < row
    assert "Year Ended December 31, | Six Months Ended June 30,\n2023 | 2024 | 2025 | 2025 | 2026" in text
    assert "3,447" not in text and "18.42" not in text  # no computed answers in the index
    nvidia = r.extract(b"<table><tr><td>a</td><td>1</td></tr></table>", "X", "NVIDIA")
    assert nvidia == "\nREPORT X; HTML TABLE 1; rows 1-1\n\na | 1\n"


def test_long_tables_repeat_their_header_rows_per_group():
    rows = "".join(_row(f"Line {i}", "1", "2", "3", "4", "5") for i in range(1, 15))
    html = ("<table>" + PERIODS + YEARS.format(unit="(in millions)") + rows + "</table>").encode()
    text = r.extract_document(html.decode(), "X")
    assert text.count("Year Ended December 31, | Six Months Ended June 30,") == 2
    assert text.count("(in millions) | 2023 | 2024 | 2025 | 2025 | 2026") == 2 and "rows 13-17" in text


def test_column_spans_and_captions_are_recorded_by_the_parser():
    parser = r.Tables()
    parser.feed('<p>Statement (In millions)</p><table><tr><td colspan="3">A</td><td>B</td></tr></table>')
    parser.close()
    assert parser.tables == [[["A", "B"]]] and parser.spans == [[[3, 1]]]
    assert parser.captions == ["Statement (In millions)"]


@pytest.fixture()
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    for key in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _record(definition="revenue"):
    from finrail.core import tracker
    item, _raw, cells, found, p = fixture(definition)
    value = r.check_and_compute(item, definition, p, found, cells)
    rec = tracker.save("finance.reports.analyze", "", {"report_id": REPORT, "definition": definition}, kind="planned", source="test")
    tracker.update(rec["id"], status="running")
    tracker.update(rec["id"], status="done", result=value)
    return rec["id"], value


def test_multiple_refuses_without_a_completed_revenue_run(state):
    with pytest.raises(ContractViolation, match="ask for its revenue first"):
        r.multiple(REPORT, "300000")
    rid, _value = _record("net_loss")
    with pytest.raises(ContractViolation, match="revenue"):
        r.multiple(REPORT, "300000", rid)
    with pytest.raises(ContractViolation, match="ask for its revenue first"):
        r.multiple(REPORT, "300000")
    with pytest.raises(ContractViolation, match="No run"):
        r.multiple(REPORT, "300000", "not-a-run")
    with pytest.raises(ContractViolation, match="Anthropic"):
        r.multiple("NVDA-Q2-FY2027", "300000")


def test_multiple_is_decimal_division_labelled_as_the_persons_figure(state):
    rid, _value = _record()
    out = r.multiple(REPORT, "300000")
    assert out["price_to_sales"] == str((Decimal(300000) / Decimal(16286)).quantize(Decimal("0.01"))) == "18.42"
    assert out["revenue_run"] == rid and out["revenue_usd_m"] == "16286" and out["valuation_usd_m"] == "300000"
    assert "not advice" in out["label"] and "not annualised" in out["basis"] and "share count blank" in out["share_count"]
    assert r.multiple(REPORT, "162860", rid)["price_to_sales"] == "10.00"
    for bad in ("", "-5", "abc", "0", "1e9", "300,000"):
        with pytest.raises(ContractViolation):
            r.multiple(REPORT, bad)
    from finrail.core import dispatch_core
    assert dispatch_core.dispatch("finance.reports.multiple", {"report_id": REPORT, "valuation_usd_m": "300000"})["result"]["price_to_sales"] == "18.42"


def _jev_down(_body):
    raise ask.decide.JevError("offline")


def test_keyword_router_sends_anthropic_questions_to_the_right_workflow():
    out = ask.route("What revenue did Anthropic report in its S-1?", ask=_jev_down)
    assert (out["workflow"], out["report_id"], out["figure"]) == ("finance.reports.analyze", REPORT, "revenue")
    out = ask.route("What does Anthropic's S-1 say about compute commitments?", ask=_jev_down)
    assert (out["workflow"], out["report_id"]) == ("finance.reports.search", REPORT)
    out = ask.route("At a $300bn valuation, what is Anthropic's price to sales?", ask=_jev_down)
    assert (out["workflow"], out["report_id"]) == ("finance.reports.multiple", REPORT)
    out = ask.route("What net loss did Anthropic report in its S-1?", ask=_jev_down)
    assert (out["workflow"], out["figure"]) == ("finance.reports.analyze", "net_loss")


def test_jev_figure_outside_the_choices_falls_back_to_keywords():
    def invents(_body):
        return {"workflow": {"choice": "finance.reports.analyze", "confidence": 0.9}, "report": {"choice": REPORT, "confidence": 0.9},
                "span": {"choice": "one", "confidence": 0.9}, "figure": {"choice": "ebitda", "confidence": 0.9}}
    assert ask.route("What was Anthropic's revenue?", ask=invents)["router"] == "keywords"
    assert "ANTH-S1-2026" in ask.REPORTS and "anthropic" in ask.NAMED and "finance.reports.multiple" in ask.workflows()


def test_ask_door_runs_the_revenue_check_and_the_multiple(state):
    from console.chat import answer
    calls = []

    def dispatch(name, payload=None, task=""):
        calls.append((name, payload))
        if name == "finance.ask.route":
            return {"result": ask.route(payload["question"], ask=_jev_down)}
        return {"id": "run-1", "result": {"price_to_sales": "18.42", "valuation_usd_m": "300000", "revenue_usd_m": "16286",
                                          "revenue_run": "rev-1", "label": "Arithmetic on a figure you supplied."}}
    turn = answer(dispatch, "What revenue did Anthropic report in its S-1?", [])
    assert turn["answer"]["kind"] == "fcf" and turn["answer"]["definition"] == "revenue"
    assert ("finance.reports.analyze", {"report_id": REPORT, "definition": "revenue"}) in calls
    turn = answer(dispatch, "At a $300bn valuation, what is Anthropic's price to sales?", [])
    assert turn["answer"]["kind"] == "multiple" and turn["answer"]["result"]["price_to_sales"] == "18.42"
    assert ("finance.reports.multiple", {"report_id": REPORT, "valuation_usd_m": "300000"}) in calls
    before = len(calls)
    turn = answer(dispatch, "What is Anthropic's price to sales multiple?", [])
    assert turn["answer"]["kind"] == "multiple" and "State the valuation" in turn["answer"]["error"]
    assert len(calls) == before + 1  # only the router ran; nothing was computed without the person's figure


def test_proof_page_shows_the_revenue_workpaper_and_divides_on_request(state):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from console.app import app
    rid, _value = _record()
    client = TestClient(app)
    page = client.get(f"/proof?run={rid}").text
    assert "16,286" in page and "Anthropic revenue" in page and "A multiple on the disclosed figure" in page
    assert "Total" in page and "Reconciled" in page
    page = client.post("/proof/multiple", data={"run_id": rid, "valuation_usd_m": "300000"}).text
    assert "18.42×" in page and "Arithmetic on a figure you supplied" in page
    page = client.post("/proof/multiple", data={"run_id": rid, "valuation_usd_m": "lots"}).text
    assert "positive decimal string" in page and "18.42" not in page


def test_ask_page_answers_an_anthropic_revenue_question_offline(state):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from console.app import app
    rid, _value = _record()
    client = TestClient(app)
    page = client.post("/ask", data={"question": "What revenue did Anthropic report in its S-1?"}, follow_redirects=True).text
    assert "Revenue: <b>$16,286m</b>" in page and "ties to the figure Anthropic reports" in page
    assert "finance.reports.analyze" in page and rid in page
    assert "price to sales" in page  # the follow-up chip offers the multiple on the checked revenue


# ── the house rule and the stock-compensation reconciliation ───────────────────

def test_house_figure_removes_stock_comp_and_recomputes_the_issuer_bridge():
    item, _raw, cells, found, p = fixture("house_operating_income")
    assert cells["aoi_settlement"]["value"] == "0"  # a printed dash, read as nil only because the row says so
    out = r.check_and_compute(item, "house_operating_income", p, found, cells)
    assert out["amount"] == "-567" and out["formula"] == "adjusted_operating_income - stock_comp"
    assert out["issuer_figure"] == {"label": "Adjusted Operating Income (Loss)", "amount": "1618"}
    assert out["difference"] == [{"label": "Stock-based compensation expense", "amount": "2185"}]
    bridge = [(b["sign"], b["label"], b["amount"]) for b in out["reconciled"]["bridge"]]
    assert bridge == [("-", "Operating loss", "693"), ("+", "Stock-based compensation expense", "2185"),
                      ("+", "Non-cash charitable contributions (1)", "126"), ("+", "Bartz settlement (2)", "0")]
    assert out["reconciled"]["value"] == "1618" and out["reconciled"]["table"] == 61
    assert out["house_rule"]["id"] == "HR-1" and out["house_rule"]["illustrative"] is True
    assert "House rule HR-1 applied in code" in out["checks"]
    assert r.replay(out)["amount"] == "-567"
    assert all(r.replay(out, attack)["refused"] for attack in r.ATTACKS)


def test_an_issuer_bridge_that_does_not_recompute_is_refused():
    item, _raw, cells, found, p = fixture("house_operating_income", charitable="127")
    with pytest.raises(ContractViolation, match="does not recompute from its own bridge"):
        r.check_and_compute(item, "house_operating_income", p, found, cells)


def test_a_dash_is_zero_only_on_rows_that_declare_it():
    period, year = "Six Months Ended June 30,", "2026"
    rows = [["", period], ["", "2025", "2026"], ["Line", "1", "—"]]
    spans = [[1, 2], [1, 1, 1], [1, 1, 1]]
    with pytest.raises(ContractViolation, match="not a number"):
        r._cell_under(rows, spans, "Line", period, year)
    assert r._cell_under(rows, spans, "Line", period, year, blank_is_zero=True)["signed_value"] == 0


def test_adding_back_stock_comp_is_refused_by_the_house_rule_before_anything_runs():
    with pytest.raises(ContractViolation, match="House rule HR-1"):
        r.ReportAnalyze().plan(REPORT, "adjusted_operating_income")
    assert r.ReportAnalyze().plan(REPORT, "house_operating_income")["definition"] == "house_operating_income"
    assert r.ReportAnalyze.steps[0]["name"] == "House rule"


def test_router_sends_the_house_and_add_back_questions_to_the_checked_workflow():
    out = ask.route("What is Anthropic's operating result under our house rules?", ask=_jev_down)
    assert (out["workflow"], out["report_id"], out["figure"]) == ("finance.reports.analyze", REPORT, "house_operating_income")
    out = ask.route("Add back stock compensation the way Anthropic does.", ask=_jev_down)
    assert (out["workflow"], out["figure"]) == ("finance.reports.analyze", "adjusted_operating_income")


def _record_house(state):
    return _record("house_operating_income")


def test_ask_door_answers_the_house_figure_then_refuses_the_add_back(state):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from console.app import app
    rid, _value = _record("house_operating_income")
    client = TestClient(app)
    page = client.post("/ask", data={"question": "What is Anthropic's operating result under our house rules?"}, follow_redirects=True).text
    assert "Under your house rules: <b>−$567m</b>" in page and "+$1,618m adjusted" in page and "$2,185m gap" in page
    assert "Less Stock-based compensation expense" in page and rid in page
    assert "Add back stock compensation the way Anthropic does." in page  # the next chip sets up the refusal
    chat = page.split("name='chat' value='")[1].split("'")[0]
    page = client.post("/ask", data={"question": "Add back stock compensation the way Anthropic does.", "chat": chat},
                       follow_redirects=True).text
    assert "Refused. House rule HR-1 (illustrative)" in page
    assert "House rule · Kernel" in page and "s-fail" in page.split("House rule · Kernel")[0].rsplit("<li", 1)[1]


def test_proof_page_draws_the_bridge_and_the_rules_page_lists_the_house_rule(state):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from console.app import app
    rid, _value = _record("house_operating_income")
    client = TestClient(app)
    page = client.get(f"/proof?run={rid}").text
    assert "(567)" in page and "1,618" in page and "(2,185)" in page and "(693)" in page
    assert "House rule HR-1 (illustrative):" in page and "House rule HR-1 applied." in page and "W/P OP-1" in page
    rules = client.get("/rules").text
    assert "House rules, applied by code to every figure" in rules and "HR-1" in rules and "illustrative" in rules


# ── the opener: three true profit lines from one filing ────────────────────────

def test_house_run_carries_the_profit_lines_signed_as_the_filing_prints_them():
    item, _raw, cells, found, p = fixture("house_operating_income")
    out = r.check_and_compute(item, "house_operating_income", p, found, cells)
    lines = {line["key"]: (line["amount"], line["table"]) for line in out["lines"]}
    assert lines == {"revenue": ("16286", 109), "net_loss": ("-80748", 109), "fv_convertibles": ("-72388", 109),
                     "aoi_operating_loss": ("-693", 61), "adjusted_operating_income": ("1618", 61)}
    assert r.replay(out)["amount"] == "-567"  # the lines are context; the checked figure is unchanged


def test_router_reads_did_it_make_money_as_the_profit_lines():
    out = ask.route("Did Anthropic make money, according to its S-1?", ask=_jev_down)
    assert (out["workflow"], out["report_id"], out["figure"]) == ("finance.reports.analyze", REPORT, "made_money")


def test_ask_door_opens_with_three_true_lines_then_the_house_figure(state):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from console.app import app
    from console.chat import MONEY_Q, HOUSE_Q, SUGGESTIONS
    assert SUGGESTIONS[0] == MONEY_Q
    rid, _value = _record("house_operating_income")
    client = TestClient(app)
    page = client.post("/ask", data={"question": MONEY_Q}, follow_redirects=True).text
    assert "Three true answers, one filing." in page and "Which one belongs in your memo depends on your method." in page
    ladder = page.split("class='line-ladder'")[1].split("</table>")[0]
    for name, amount, cell in (("Net loss", "−$80,748m", "T109 · R"), ("of which non-cash", "−$72,388m", "T109 · R"),
                               ("Operating loss", "−$693m", "T61 · R"), ("Adjusted operating income", "+$1,618m", "T61 · R")):
        row = ladder.split(f">{name}<", 1)[1].split("</tr>")[0]
        assert amount in row and cell in row, (name, row)
    assert ladder.index(">Net loss<") < ladder.index(">Operating loss<") < ladder.index(">Adjusted operating income<")
    assert "Which line?" in page and rid in page
    assert HOUSE_Q.replace("'", "&#x27;") in page  # the next chip is the house figure


def test_a_profit_question_with_no_figure_picked_gets_the_profit_lines(state):
    from console.chat import answer

    def jev_without_figure(body):
        return {"workflow": {"choice": "finance.reports.analyze", "confidence": 0.9},
                "report": {"choice": REPORT, "confidence": 1.0}, "span": {"choice": "one", "confidence": 1.0},
                "figure": {"choice": "none", "confidence": 0.9}}
    calls = []

    def dispatch(name, payload=None, task=""):
        calls.append((name, payload))
        if name == "finance.ask.route":
            return {"result": ask.route(payload["question"], ask=jev_without_figure)}
        return {"id": "run-1"}
    turn = answer(dispatch, "Was Anthropic profitable, according to its S-1?", [])
    assert turn["route"]["figure"] == "made_money" and turn["route"]["figure_by"] == "code, from the words"
    assert ("finance.reports.analyze", {"report_id": REPORT, "definition": "house_operating_income"}) in calls
    turn = answer(dispatch, "What revenue did Anthropic report in its S-1?", [])
    assert turn["route"]["figure"] == "revenue"


# ── what the web says about Anthropic, read by Jev, decided by code ─────────────

PRESS = {"url": "https://example.test/anthropic-s1", "publisher": "example.test", "source_tier": "press",
         "title": "Anthropic files for IPO",
         "content": ("Anthropic reported revenue of $16.3 billion in the first half of 2026. "
                     "The company swung to an operating profit of $1.6 billion in the first half of 2026. "
                     "Anthropic lost $80.7 billion in the first six months of 2026. "
                     "Its annualised run-rate revenue reached $46 billion. "
                     "Anthropic posted adjusted operating income of $1.6 billion for the first half of 2026. "
                     "The filing runs to more than 400 pages.")}


def _reader(picks):
    def answer(body):
        out = {}
        for i, (line, period, conf) in enumerate(picks):
            out[f"line_{i}"] = {"choice": line, "confidence": conf}
            out[f"quarter_{i}"] = {"choice": period, "confidence": conf}
        assert "operating_loss" in body["questions"]["line_0"]["criteria"]  # Anthropic's lines, not NVIDIA's
        return out
    return answer


def test_web_claims_about_anthropic_are_read_by_jev_and_decided_by_code(state):
    from finrail.extensions.finance import claims
    _record("house_operating_income")
    picks = [("revenue", REPORT, 0.97), ("operating_loss", REPORT, 0.95), ("net_loss", REPORT, 0.96),
             ("revenue", "other", 0.9), ("adjusted_operating_income", REPORT, 0.96)]
    out = claims.check_web(REPORT, search=lambda q, c: [PRESS], ask=_reader(picks))
    verdicts = [c["verdict"] for c in out["claims"]]
    assert verdicts == ["matches", "wrong_line", "matches", "unchecked", "matches"]
    mixup = out["claims"][1]
    assert "operating result for the first half of 2026 is −$693m" in mixup["detail"]
    assert "$1,600m is Anthropic's adjusted operating income ($1,618m)" in mixup["detail"]
    assert out["reader"] == "jev" and out["counts"]["wrong_line"] == 1
    assert len(out["claims"]) == 5  # the sentence with no money figure is never put to Jev


def test_code_reads_the_period_so_a_run_rate_is_never_the_s1_half():
    from finrail.extensions.finance import claims
    assert claims.anthropic_period("Revenue of $16.3 billion in the first half of 2026.") == REPORT
    assert claims.anthropic_period("Annualised run-rate revenue of $46 billion.") == "other"
    assert claims.anthropic_period("Revenue of $4.6 billion in 2025.") == "other"
    assert claims.anthropic_period("Revenue of $16.3 billion.") == ""
    out = claims.read(["Its annualised run-rate revenue reached $46 billion in the first half."],
                      ask=_reader([("revenue", REPORT, 0.99)]), company="Anthropic")
    assert out["readings"][0]["sure"] is False and out["readings"][0]["period_conflict"] == "other"


def test_without_jev_keyword_rules_read_anthropic_claims_and_say_so(state):
    from finrail.extensions.finance import claims
    _record("house_operating_income")
    out = claims.check_web(REPORT, search=lambda q, c: [PRESS], ask=_jev_down)
    assert out["reader"] == "keywords"
    assert [c["verdict"] for c in out["claims"]][:3] == ["matches", "wrong_line", "matches"]


def test_web_claims_about_anthropic_need_the_checked_lines_first(state):
    from finrail.extensions.finance import claims
    with pytest.raises(ContractViolation, match="Ask whether Anthropic made money first"):
        claims.check_web(REPORT, search=lambda q, c: [PRESS], ask=_jev_down)


def test_router_sends_the_web_question_to_tavily_without_jev():
    from console.chat import WEB_Q
    out = ask.route(WEB_Q, ask=_jev_down)
    assert (out["workflow"], out["report_id"]) == ("finance.reports.webclaims", REPORT)


@pytest.mark.parametrize("sentence,verdict", [
    ("Anthropic posted a net loss of more than $80 billion in the first half of 2026.", "matches"),   # 80,748 is more than 80,000
    ("Anthropic posted a net loss of nearly $81 billion in the first half of 2026.", "matches"),       # 80,748 is just under 81,000
    ("Anthropic posted a net loss of about $81 billion in the first half of 2026.", "matches"),
    ("Anthropic posted a net loss of $80 billion in the first half of 2026.", "loose"),                # 80,748 is outside 80,000 ± 500, within one unit
    ("Anthropic posted a net loss of $79 billion in the first half of 2026.", "wrong"),
    ("Anthropic posted a net loss of more than $90 billion in the first half of 2026.", "wrong"),
    ("Anthropic posted a net loss of more than $70 billion in the first half of 2026.", "wrong"),      # true, but loose by a whole unit
])
def test_more_than_nearly_and_about_are_read_as_stated(state, sentence, verdict):
    from finrail.extensions.finance import claims
    _record("house_operating_income")
    page = {**PRESS, "content": sentence}
    out = claims.check_web(REPORT, search=lambda q, c: [page], ask=_reader([("net_loss", REPORT, 0.97)]))
    assert out["claims"][0]["verdict"] == verdict, out["claims"][0]["detail"]
    if verdict == "wrong" and "more than" in sentence:
        assert "The claim says more than $" in out["claims"][0]["detail"]


def test_the_convertible_notes_charge_is_its_own_checked_line(state):
    from finrail.extensions.finance import claims
    _record("house_operating_income")
    page = {**PRESS, "content": "A non-cash charge of $72.4 billion on convertible notes drove the loss in the first half of 2026."}
    out = claims.check_web(REPORT, search=lambda q, c: [page], ask=_reader([("convertible_notes_charge", REPORT, 0.95)]))
    assert out["claims"][0]["verdict"] == "matches" and "−$72,388m" in out["claims"][0]["detail"]
    assert claims._anth_keywords(page["content"], "")["line"] == "convertible_notes_charge"


def test_nvidia_claims_read_qualifiers_too():
    from decimal import Decimal
    from finrail.extensions.finance import claims
    rec = {"id": "r1", "result": {"period": "Q2 FY2027", "amount": "21700", "inputs": []}}  # 21,700: outside $21bn ± 500, inside "more than $21bn"
    assert claims.judge(Decimal("21000"), Decimal("500"), "free_cash_flow", rec, {"kind": "min", "word": "more than"})["verdict"] == "matches"
    assert claims.judge(Decimal("21000"), Decimal("500"), "free_cash_flow", rec)["verdict"] == "wrong"


def test_a_sentence_with_no_period_is_ruled_out_by_code_not_by_jev(state):
    from finrail.extensions.finance import claims
    _record("house_operating_income")
    page = {**PRESS, "content": "That loss was mostly a $72 billion non-cash charge on its convertible bonds."}
    out = claims.check_web(REPORT, search=lambda q, c: [page], ask=_reader([("convertible_notes_charge", REPORT, 0.99)]))
    assert out["claims"][0]["verdict"] == "unchecked" and "names no period" in out["claims"][0]["detail"]


def test_web_summary_names_only_what_happened():
    from console.chat import _web_summary
    assert _web_summary({"matches": 1}) == "Tavily found <b>1</b> claim on the web. <b>1</b> matches the filing."
    assert ("<b>2</b> match the filing. <b>1</b> is loosely rounded. <b>1</b> puts a figure on the wrong line."
            in _web_summary({"matches": 2, "loose": 1, "wrong_line": 1, "definition": 0}))


def test_a_quarter_named_in_the_sentence_is_ruled_out_by_code_even_when_jev_is_unsure(state):
    # The live tomtunguz.com sentence: Jev was 70% on the period, code reads "second quarter".
    from finrail.extensions.finance import claims
    _record("house_operating_income")
    page = {**PRESS, "content": "It narrowed every quarter of 2026, & in the second quarter Anthropic reported $974M of "
                                "operating income, an 8.4% GAAP margin & 19% adjusted."}
    out = claims.check_web(REPORT, search=lambda q, c: [page], ask=_reader([("operating_loss", "other", 0.70)]))
    assert out["claims"][0]["verdict"] == "unchecked" and "names another period" in out["claims"][0]["detail"]


def test_web_search_runs_both_queries_and_merges_pages_by_url(state):
    from finrail.extensions.finance import claims
    _record("house_operating_income")
    asked = []
    one = {**PRESS, "url": "https://a.example/1", "content": "Anthropic posted a net loss of $80.7 billion in the first half of 2026."}
    two = {**PRESS, "url": "https://b.example/2", "content": "Anthropic's revenue was $16.3 billion in the first six months of 2026."}
    def search(q, c):
        asked.append(q)
        return [one, two] if len(asked) == 1 else [two]
    out = claims.check_web(REPORT, search=search, ask=_reader([("net_loss", REPORT, 0.97), ("revenue", REPORT, 0.97)]))
    assert asked == list(claims.ANTH_QUERIES) and out["pages"] == 2
    assert [c["verdict"] for c in out["claims"]] == ["matches", "matches"]
