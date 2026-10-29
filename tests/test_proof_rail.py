"""Proof-rail contracts: the calculation view must show the evidence chain,
mark the exact source row that supplied each input, and render a check
failure as a designed refusal rather than an error dump."""

from console.reports import result_html
from console.app import _run_block


def _value():
    quote_ocf = "Net cash provided by operating activities 24,077"
    quote_capex = "Purchases of property and equipment (2,677)"
    return {
        "report_id": "NVDA-Q2-FY2027", "company": "NVIDIA",
        "period": "Q2 FY2027", "period_end": "2026-07-26",
        "definition": "issuer_fcf", "metric": "Free cash flow (issuer definition)",
        "amount": "21341", "currency": "USD", "unit": "million",
        "reported_fcf": "21341",
        "reconciliation": "Matches issuer-reported FCF",
        "formula": "operating_cash_flow - cash_purchases - principal_payments",
        "checks": ["Original report SHA-256", "Quarter and source column",
                   "USD millions", "Verbatim retrieved quotes",
                   "Exact archived source cells", "Decimal arithmetic"],
        "source_sha256": "0" * 64, "source_url": "https://example.invalid/report",
        "inputs": [
            {"metric": "operating_cash_flow", "amount": "24077", "table": 4,
             "row_number": 3, "evidence_id": "ev-1", "quote": quote_ocf},
            {"metric": "cash_purchases", "amount": "2677", "table": 4,
             "row_number": 5, "evidence_id": "ev-1", "quote": quote_capex},
            {"metric": "principal_payments", "amount": "59", "table": 4,
             "row_number": 6, "evidence_id": "ev-2", "quote": "Principal payments (59)"},
        ],
        "retrieval": {"backend": "Google Vertex AI RAG Engine (serverless)",
                      "evidence": [
                          {"id": "ev-1", "source_name": "Cash flow statement",
                           "source_uri": "gs://example/report.txt",
                           "text": f"CONDENSED STATEMENTS\n{quote_ocf}\n{quote_capex}\n"},
                          {"id": "ev-2", "source_name": "Cash flow statement",
                           "source_uri": "gs://example/report.txt",
                           "text": "Principal payments (59)\n"},
                          {"id": "ev-3", "source_name": "Other excerpt",
                           "source_uri": "gs://example/other.txt",
                           "text": "Unused retrieved context."},
                      ]},
    }


def test_proof_rail_shows_the_whole_chain():
    html = result_html(_value())
    for label in ("Retrieved", "Rows bound", "Cells checked", "Calculated", "Reconciled"):
        assert label in html
    assert "24,077 − 2,677 − 59" in html
    assert "Matches the issuer-reported $21,341m" in html


def test_used_rows_are_marked_inside_the_filing_excerpt():
    html = result_html(_value())
    assert "filing-excerpt" in html and "Input source" in html
    assert "<mark class='cell-mark'>Net cash provided by operating activities 24,077</mark>" in html
    # Unused evidence stays a collapsed disclosure, not a proof surface.
    assert "Unused retrieved context." in html


def test_failed_reports_run_renders_a_designed_refusal():
    out = {"capability": "finance.reports.analyze", "status": "failed",
           "errors": ["ContractViolation: Source-cell check refused operating_cash_flow: wrong quarter column."]}
    html = _run_block(out)
    assert "Not verified. No answer issued." in html
    assert "outcome-refused" in html
    assert "wrong quarter column" in html


def test_an_outage_is_not_told_as_a_check_refusal():
    out = {"capability": "finance.reports.analyze", "status": "failed",
           "errors": ["OpFailed: Google report service returned HTTP 503."]}
    html = _run_block(out)
    assert "Not verified. No answer issued." not in html
    assert "HTTP 503" in html
