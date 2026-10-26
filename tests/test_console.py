"""Console tests: read pages render structured output, fully offline."""

import pytest

pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))


@pytest.fixture()
def client():
    from console.app import app
    return TestClient(app)


def test_home_shows_counts_and_cli(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "suggest evidence" in page.text
    assert "Start the demo" in page.text
    assert "/cli" in page.text


def test_cli_page_generated_from_registry(client):
    page = client.get("/cli")
    assert page.status_code == 200
    assert "finrail get finance.compute.metric" in page.text
    assert "finrail do finance.memo.release" in page.text


def test_compute_metric_card_cites_cells(client):
    page = client.get("/compute", params={"metric": "margin"})
    assert page.status_code == 200
    assert "0.159751" in page.text
    assert "IncomeStatement!B2" in page.text


def test_screen_fixture_verdicts_rendered(client):
    page = client.get("/screen")
    assert page.status_code == 200
    assert "reported" in page.text
    assert "rejected" in page.text


def test_dataroom_doc_view(client):
    page = client.get("/dataroom", params={"doc": "im-2026"})
    assert page.status_code == 200
    assert "48.2" in page.text


def test_approvals_empty_state_has_next_action(client):
    page = client.get("/approvals")
    assert page.status_code == 200
    assert "Nothing is waiting" in page.text
    assert "/dataroom" in page.text


def test_demo_seed_makes_screen_run_completable(client, monkeypatch):
    monkeypatch.setenv("FINRAIL_DEMO_ENTITY", "04123456")
    page = client.get("/screen")
    assert "Entity on record" in page.text
    assert "operator (public demo seed)" in page.text
    run = client.post("/screen/run", data={"company_id": "04123456"})
    assert run.status_code == 200
    assert "done" in run.text
