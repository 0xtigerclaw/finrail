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


def test_home_is_the_ask_door(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "Evaluate investment opportunities" in page.text and ">How it works<" in page.text and "hero-how" in page.text
    assert "DeepSeek Harness" in page.text
    assert page.text.index("Did Anthropic make money") < page.text.index("What was NVIDIA")  # demo questions first
    assert "/cli" in page.text


def test_proof_page_offers_a_live_run_on_a_fresh_instance(client):
    page = client.get("/proof")
    assert "Every plugin declares its rules in code" in page.text
    assert "Run it live" in page.text and "house_operating_income" in page.text


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
    assert "Open Ask" in page.text


def test_demo_seed_makes_screen_run_completable(client, monkeypatch):
    monkeypatch.setenv("FINRAIL_DEMO_ENTITY", "04123456")
    page = client.get("/screen")
    assert "Entity on record" in page.text
    assert "operator (public demo seed)" in page.text
    run = client.post("/screen/run", data={"company_id": "04123456"})
    assert run.status_code == 200

    # Submission starts one background run. Follow its read-only detail page;
    # polling must never replay the POST or start a second screen.
    import re
    import time

    link = re.search(r"(?:data-refresh-url|href)=['\"](/runs/[0-9a-f]+)['\"]", run.text)
    assert link, "The submitted workflow must link to its real run"
    deadline = time.monotonic() + 5
    while True:
        outcome = client.get(link.group(1))
        assert outcome.status_code == 200
        if "Completed" in outcome.text or "Failed" in outcome.text:
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    assert "Completed" in outcome.text
    assert "fixture" in outcome.text.lower()
    assert "reported" in outcome.text.lower()
    assert "rejected" in outcome.text.lower()


def test_masthead_names_the_configured_nemotron_version(monkeypatch):
    from console import app as console_app
    monkeypatch.setattr(console_app.llm, "engine", lambda: "nebius")
    for model, label in (("nvidia/Nemotron-3.5-Super-VL", "NVIDIA Nemotron 3.5 Super on Nebius"),
                         ("nvidia/nemotron-3-super-120b-a12b", "NVIDIA Nemotron 3 Super on Nebius")):
        monkeypatch.setattr(console_app.llm, "_http_config", lambda model=model: ("u", "k", model))
        assert console_app._engine_short() == label
