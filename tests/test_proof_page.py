"""The proof page shows a recorded run; the tamper replay is decided by the checker."""
import pytest

pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from finrail.core import tracker  # noqa: E402
from finrail.extensions.finance import public_reports as r  # noqa: E402
from test_public_reports import fixture  # noqa: E402


@pytest.fixture()
def recorded(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    item, _raw, cells, found, candidate = fixture()
    monkeypatch.setattr(r, "report", lambda _rid: item)
    value = r.check_and_compute(item, "issuer_fcf", candidate, found, cells)
    rec = tracker.save("finance.reports.analyze", "", {"report_id": item["source_id"]},
                       kind="planned", source="test")
    tracker.update(rec["id"], status="running")
    tracker.update(rec["id"], status="done", result=value)
    return rec["id"], value


@pytest.mark.parametrize("attack", list(r.ATTACKS))
def test_every_attack_is_refused_by_the_checker(recorded, attack):
    _rid, value = recorded
    assert r.replay(value)["amount"] == value["amount"]
    out = r.replay(value, attack)
    assert out["refused"] and out["refused_by"] == "check_and_compute"


def test_proof_page_draws_the_recorded_run_and_its_declaration(recorded):
    from console.app import app
    rid, value = recorded
    page = TestClient(app).get("/proof").text
    assert "48,554" in page and rid in page
    assert "Who did what, under which rule" in page
    assert "Declared once, in code" in page and "must equal the figure the issuer reports" in page


def test_challenge_route_shows_the_checker_refusal(recorded):
    from console.app import app
    rid, _value = recorded
    page = TestClient(app).post("/proof/challenge", data={"run_id": rid, "attack": "invented_row"}).text
    assert "Refused by" in page
    assert "not present in the retrieved evidence" in page
