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


def s1_html(*, year="2026", revenue="$ 16,286", total="$ 16,286", unit_caption=True) -> bytes:
    unit = "(In millions, except share amounts which are in thousands, and per share data)" if unit_caption else "(per share data)"
    operations = _table(["F-7", "Anthropic, PBC", "Consolidated Statements of Operations", unit], "",
                        _row("Revenue", "$ 41", "$ 386", "$ 4,585", "$ 1,163", revenue),
                        _row("Operating expenses:"), _row("Total operating expenses", "592", "3,366", "12,645", "3,298", "16,979"),
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
    parts = []
    for number in range(1, 121):
        parts.append({72: summary, 109: operations, 114: cash, 120: note}.get(number, "<table></table>"))
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
    assert item["rag_file_ids"] == [] and item["indexed_uris"] == [f"gs://{item['bucket']}/{item['parsed_object']}"]
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
    out = ask.route("What was Anthropic's revenue in the first half of 2026?", ask=_jev_down)
    assert (out["workflow"], out["report_id"], out["figure"]) == ("finance.reports.analyze", REPORT, "revenue")
    out = ask.route("What does Anthropic's S-1 say about compute commitments?", ask=_jev_down)
    assert (out["workflow"], out["report_id"]) == ("finance.reports.search", REPORT)
    out = ask.route("At a $300bn valuation, what is Anthropic's price to sales?", ask=_jev_down)
    assert (out["workflow"], out["report_id"]) == ("finance.reports.multiple", REPORT)
    out = ask.route("What was Anthropic's net loss in the first half of 2026?", ask=_jev_down)
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
    turn = answer(dispatch, "What was Anthropic's revenue in the first half of 2026?", [])
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
    page = client.post("/ask", data={"question": "What was Anthropic's revenue in the first half of 2026?"}, follow_redirects=True).text
    assert "Revenue: <b>$16,286m</b>" in page and "ties to the figure Anthropic reports" in page
    assert "finance.reports.analyze" in page and rid in page
    assert "price to sales" in page  # the follow-up chip offers the multiple on the checked revenue
