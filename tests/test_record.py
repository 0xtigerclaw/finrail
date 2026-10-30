"""Recording a stated NVIDIA figure: code reads it, the claim check compares it to a
completed source-checked run, and only a person can release it."""
import pytest

from finrail.core import dispatch_core, tracker
from finrail.core.errors import FinrailError
from finrail.extensions.finance import public_reports as r
from test_public_reports import fixture


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    # No Jev key: the claim reader falls back to keyword rules, so no network is touched.
    for key in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def checked(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    item, _raw, cells, found, candidate = fixture()
    value = r.check_and_compute(item, "issuer_fcf", candidate, found, cells)
    rec = tracker.save("finance.reports.analyze", "", {"report_id": item["source_id"]}, kind="planned", source="test")
    tracker.update(rec["id"], status="running")
    tracker.update(rec["id"], status="done", result=value)
    return item["source_id"], value


def _record(report_id, statement):
    return dispatch_core.dispatch("finance.reports.record", {"report_id": report_id, "statement": statement})


def test_figures_are_read_by_code_not_years():
    assert r.stated_millions("Record Q2 FCF as $24,077m") == 24077
    from finrail.extensions.finance.claims import stated_figure
    assert stated_figure("FCF was $21.3 billion") == (21300, 50)
    assert r.stated_millions("In FY2027 Q2, FCF was $21.341bn") == 21341
    assert r.stated_millions("Record Q2 FCF") is None


def test_no_checked_run_means_nothing_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    with pytest.raises(FinrailError, match="no source-checked cash flow"):
        _record("NVDA-Q2-FY2027", "Record Q2 FCF as $21,341m")


def test_operating_cash_flow_stated_as_fcf_is_refused_and_named(checked):
    report_id, value = checked
    with pytest.raises(FinrailError, match="claim check refused") as refused:
        _record(report_id, "Record NVIDIA's Q1 free cash flow as $50,344m")
    assert "is NVIDIA's operating cash flow, not its free cash flow" in str(refused.value)


def test_the_same_figure_stated_as_operating_cash_flow_waits_for_a_person(checked):
    report_id, value = checked
    out = _record(report_id, "Record NVIDIA's Q1 operating cash flow as $50,344m")
    assert tracker.awaiting(tracker.load(out["id"]))
    assert tracker.load(out["id"])["proposal"]["line"] == "operating_cash_flow"


def test_the_checked_figure_waits_for_a_person(checked):
    report_id, value = checked
    out = _record(report_id, f"Record Q1 free cash flow as ${int(value['amount']):,}m")
    assert tracker.awaiting(tracker.load(out["id"]))


def test_only_nvidia_figures_can_be_recorded(checked):
    with pytest.raises(FinrailError, match="only NVIDIA"):
        _record("ASML-Q3-2026", "Record ASML FCF as $3,000m")


@pytest.mark.parametrize("figure, why", [
    ("-$48,554m", "The claim says −$48,554m"),  # the sign is kept, so a negative never matches
    ("EUR 48,554m", "not in US dollars"),
    ("$48,554", "no unit"),
    ("48,554m", "no currency"),
    ("$48,554m or $48.6bn", "more than one figure"),
])
def test_a_figure_is_read_as_typed_or_refused(checked, figure, why):
    report_id, value = checked
    assert value["amount"] == "48554"
    with pytest.raises(FinrailError, match="claim check refused") as refused:
        _record(report_id, f"Record NVIDIA's Q1 free cash flow as {figure}")
    assert why in str(refused.value)
    assert not [x for x in tracker.recent(20) if tracker.awaiting(x)]


def test_another_fiscal_year_is_not_this_quarter(checked):
    report_id, _value = checked
    with pytest.raises(FinrailError, match="not for a quarter Finrail has checked"):
        _record(report_id, "Record NVIDIA Q1 FY2025 free cash flow as $48,554m")
