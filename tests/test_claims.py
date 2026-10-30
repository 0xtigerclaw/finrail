"""The claim reader: Jev picks the line and quarter; code reads the figure and decides."""
from decimal import Decimal

from finrail.extensions.finance import claims


def _jev(picks):
    def ask(body):
        out = {}
        for i, (line, quarter, conf) in enumerate(picks):
            out[f"line_{i}"] = {"choice": line, "confidence": conf}
            out[f"quarter_{i}"] = {"choice": quarter, "confidence": conf}
        return out
    return ask


def test_each_sentence_is_named_in_its_own_question():
    body = claims._body(["FCF was $1m", "Capex was $2m"], "NVDA-Q2-FY2027")
    assert '"FCF was $1m"' in body["questions"]["line_0"]["instructions"]
    assert '"Capex was $2m"' in body["questions"]["line_1"]["instructions"]


def test_below_the_bar_a_reading_is_not_sure():
    out = claims.read(["Cash was strong at $5bn"], ask=_jev([("operating_cash_flow", "NVDA-Q2-FY2027", 0.5)]))
    assert out["reader"] == "jev" and not out["readings"][0]["sure"]


def test_a_pick_outside_the_choices_falls_back_to_rules():
    out = claims.read(["Free cash flow was $21.3bn in Q2"], ask=_jev([("revenue_guess", "NVDA-Q2-FY2027", 0.99)]))
    assert out["reader"] == "keywords" and out["readings"][0]["line"] == "free_cash_flow"


def test_judge_matches_to_typed_precision_and_names_the_right_line():
    rec = {"id": "r1", "result": {"period": "Q2 FY2027", "amount": "21341",
                                  "inputs": [{"metric": "operating_cash_flow", "amount": "24077"},
                                             {"metric": "cash_purchases", "amount": "2677"}]}}
    assert claims.judge(Decimal("21300"), Decimal("50"), "free_cash_flow", rec)["verdict"] == "matches"
    wrong = claims.judge(Decimal("24077"), Decimal("0"), "free_cash_flow", rec)
    assert wrong["verdict"] == "wrong_line" and "operating cash flow" in wrong["detail"]
    assert claims.judge(Decimal("30000"), Decimal("0"), "free_cash_flow", rec)["verdict"] == "wrong"


def test_simple_fcf_is_named_as_a_different_definition():
    rec = {"id": "r1", "result": {"period": "Q2 FY2027", "amount": "21341",
                                  "inputs": [{"metric": "operating_cash_flow", "amount": "24077"},
                                             {"metric": "cash_purchases", "amount": "2677"},
                                             {"metric": "principal_payments", "amount": "59"}]}}
    out = claims.judge(Decimal("21400"), Decimal("0.5"), "free_cash_flow", rec)
    assert out["verdict"] == "definition" and "also subtracts principal payments" in out["detail"]


def test_web_claims_are_read_by_jev_and_decided_by_code(tmp_path, monkeypatch):
    from finrail.core import tracker
    from finrail.extensions.finance import public_reports as r
    from test_public_reports import fixture
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    item, _raw, cells, found, candidate = fixture()
    value = r.check_and_compute(item, "issuer_fcf", candidate, found, cells)
    rec = tracker.save("finance.reports.analyze", "", {"report_id": item["source_id"]}, kind="planned", source="test")
    tracker.update(rec["id"], status="running")
    tracker.update(rec["id"], status="done", result=value)
    page = {"url": "https://example.test/a", "publisher": "example.test", "title": "NVIDIA Q1 FY2027 results",
            "content": "Free cash flow was $48.6 billion in Q1. Operating cash flow came in at $50,344 million. "
                       "Free cash flow was $50,344m in the first quarter. Free cash flow of $49 billion. Revenue rose."}
    picks = [("free_cash_flow", "NVDA-Q1-FY2027", 0.99), ("operating_cash_flow", "NVDA-Q1-FY2027", 0.99),
             ("free_cash_flow", "NVDA-Q1-FY2027", 0.99), ("free_cash_flow", "other", 0.95)]
    out = claims.check_web(item["source_id"], search=lambda q, c: [page], ask=_jev(picks))
    assert [c["verdict"] for c in out["claims"]] == ["matches", "matches", "wrong_line", "unchecked"]
    assert out["reader"] == "jev"


def test_code_reads_sign_currency_unit_and_one_figure_only():
    assert claims.parse_figure("FCF was -$48,554m") == {"value": Decimal(-48554), "tolerance": 0}
    assert claims.parse_figure("capex of ($2,677m)")["value"] == -2677
    assert "not in US dollars" in claims.parse_figure("FCF of EUR 48,554m")["problem"]
    assert "not in US dollars" in claims.parse_figure("FCF of €48,554m")["problem"]
    assert "no unit" in claims.parse_figure("FCF of $48,554")["problem"]
    assert "more than one" in claims.parse_figure("FCF of $21.3bn, up from $15bn")["problem"]
    assert claims.parse_figure("In FY2027 Q2, up 40%, FCF was $21.3bn")["value"] == 21300


def test_code_reads_the_period_a_sentence_names():
    assert claims.stated_period("Q1 FY2025 free cash flow was $48,554m") == "other"
    assert claims.stated_period("Q2 2026 free cash flow") == "other"  # a calendar year is not FY2027
    assert claims.stated_period("Q3 free cash flow") == "other"
    assert claims.stated_period("Q2 fiscal 2027 FCF of $2,026m") == "NVDA-Q2-FY2027"
    assert claims.stated_period("FCF was $21bn") == ""


def test_the_fallback_reader_reads_the_year_and_needs_the_quarter_named():
    no_jev = lambda _b: (_ for _ in ()).throw(claims.decide.JevError("down"))  # noqa: E731
    out = claims.read(["Record NVIDIA Q1 FY2025 free cash flow as $48,554m",
                       "Record NVIDIA's free cash flow as $48,554m",
                       "Record NVIDIA's Q1 FY2027 free cash flow as $48,554m"], context="NVDA-Q1-FY2027", ask=no_jev)
    first, unnamed, named = out["readings"]
    assert first["quarter"] == "other"  # another fiscal year: never this quarter
    assert not unnamed["sure"]
    assert named["quarter"] == "NVDA-Q1-FY2027" and named["sure"]


def test_a_sure_jev_pick_that_contradicts_the_stated_year_is_not_decided():
    out = claims.read(["NVIDIA's Q1 fiscal 2025 free cash flow was $48.6bn"],
                      ask=_jev([("free_cash_flow", "NVDA-Q1-FY2027", 0.99)]))
    r = out["readings"][0]
    assert r["period_conflict"] == "other" and not r["sure"]
