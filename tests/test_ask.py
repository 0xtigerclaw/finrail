"""The Ask door: the router picks, the picked capability answers, nothing else writes."""
import pytest

pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from finrail.extensions.finance import ask  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    for key in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    from console.app import app
    return TestClient(app)


def _ask(client, question, **extra):
    return client.post("/ask", data={"question": question, **extra}, follow_redirects=True).text


def test_without_jev_keyword_rules_route_and_say_so(client):
    page = _ask(client, "Any regulatory red flags on Northwind?")
    assert "Keyword rules picked" in page and "Jev was unreachable" in page
    assert "finance.screen.fixture" in page


def test_unanswerable_question_runs_nothing(client):
    page = _ask(client, "Should we buy NVIDIA?")
    assert "won’t answer that" in page


def test_jev_below_the_bar_asks_the_person_to_pick(client, monkeypatch):
    def unsure(_body):
        return {"workflow": {"choice": "finance.reports.analyze", "confidence": 0.6,
                             "probabilities": {"finance.reports.analyze": 0.6, "finance.reports.search": 0.3, "none": 0.1}},
                "report": {"choice": "NVDA-Q2-FY2027", "confidence": 0.9},
                "metric": {"choice": "margin", "confidence": 0.5}, "span": {"choice": "one", "confidence": 0.9}}
    monkeypatch.setattr(ask.decide, "asker", lambda **_kw: unsure)
    page = _ask(client, "Is NVIDIA's cash flow real?")
    assert "60% sure" in page and "nothing ran" in page
    assert "name='force' value='finance.reports.analyze'" in page
    assert "Source-checked figure" in page  # the capability's own ask_label


def test_jev_cannot_route_outside_the_offered_choices():
    def invents(_body):
        return {"workflow": {"choice": "wire_money", "confidence": 0.99}, "report": {"choice": "NVDA-Q2-FY2027"},
                "metric": {"choice": "margin"}, "span": {"choice": "one"}}
    out = ask.route("Send the money", ask=invents)
    assert out["router"] == "keywords"


def test_screen_sentence_comes_from_the_gate_counts(client):
    page = _ask(client, "Any regulatory red flags on Northwind?")
    assert "verified findings on Northwind" in page or "verified finding on Northwind" in page
    assert "Evidence gate · Kernel" in page and "quotes discarded" in page


def test_trace_names_router_plugin_declaration_and_verdict(client):
    page = _ask(client, "Any regulatory red flags on Northwind?")
    assert "Route · Pick · Jev (TypeSafe)" in page and "enforced in <code>ask.route()</code>" in page
    assert "Plugin · <code>finance.screen.fixture</code>" in page
    assert "Evidence gate · Kernel" in page and "enforced in <code>gate.py, _classify()</code>" in page
    assert "class='verdict v-pass'>Answered" in page


def test_unanswerable_trace_shows_nothing_ran(client):
    page = _ask(client, "Should we buy NVIDIA?")
    assert "None ran" in page and "Nothing ran" in page


def test_router_choices_are_the_capabilities_that_declare_ask():
    offered = ask.workflows()
    assert "finance.reports.analyze" in offered and "finance.reports.record" in offered
    assert "finance.compute.metric" not in offered  # Meridian is not askable any more
    assert "finance.memo.release" not in offered  # declares no ask, so it can never be routed to
    assert offered["none"]


def test_trace_is_drawn_from_the_capability_declaration(client, monkeypatch):
    from finrail.core import registry
    registry.load()
    monkeypatch.setattr(type(registry.command("finance.screen.fixture")), "steps", (
        {"name": "Tally", "by": "Abacus", "kind": "code", "where": "tally()", "rule": "Beads only."},
        {"name": "Recount", "by": "Kernel", "kind": "check", "where": "recount()", "rule": "Count again."}))
    page = _ask(client, "Any regulatory red flags on Northwind?")
    assert "Tally · Abacus" in page and "enforced in <code>tally()</code>" in page
    assert "The 2 steps below are its own declaration" in page


def test_every_jev_call_is_written_to_the_ledger(tmp_path, monkeypatch):
    import json
    from finrail.core import decide
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    def fake_http_ask(key, url, timeout, tries, whole):
        return lambda body: {"workflow": {"choice": "finance.reports.analyze", "confidence": 0.9},
                             "report": {"choice": "NVDA-Q2-FY2027", "confidence": 0.9},
                             "span": {"choice": "one", "confidence": 0.9}}
    monkeypatch.setattr(decide, "http_ask", fake_http_ask)
    out = ask.route("What was NVIDIA's free cash flow last quarter?")
    assert out["router"] == "jev" and out["event"]
    ledger = tmp_path / "state" / "events" / "_ledger.jsonl"
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    calls = [ev for ev in events if ev.get("kind") == "decision_call"]
    assert calls and calls[-1]["data"]["capability"] == "finance.ask.route"
    assert calls[-1]["data"]["answers"]["workflow"]["choice"] == "finance.reports.analyze"
    assert "workflow" in calls[-1]["data"]["questions"]


def test_every_answer_offers_followups_never_the_same_question(client):
    page = _ask(client, "Should we buy NVIDIA?")
    assert "Ask next" in page and "Start here" not in page
    chips = page.split("class='suggestions'")[1].split("</form>")[0]
    assert "name='question'" in chips and "Should we buy NVIDIA?" not in chips
    assert "Did Anthropic make money, according to its S-1?" in chips


def test_followups_follow_the_answer():
    from console.chat import followups, SUGGESTIONS
    assert followups([]) == list(SUGGESTIONS)
    search = [{'q': "What did Nebius say about capex in Q2?", 'route': {'workflow': 'finance.reports.search', 'report_id': 'NBIS-Q2-2026'},
               'answer': {'kind': 'search', 'run': 'x'}}]
    nxt = followups(search)
    assert nxt[0] == "What did Nebius say about capex in Q1 2026?" and "What did Nebius say about capex in Q2?" not in nxt
    assert 2 <= len(nxt) <= 4
    record = search + [{'q': "Record NVIDIA's Q2 free cash flow as $21,341m", 'route': {'workflow': 'finance.reports.record', 'report_id': 'NVDA-Q2-FY2027'},
                        'answer': {'kind': 'record', 'run': 'y'}}]
    nxt = followups(record)
    assert nxt[0] == "What does the web say about NVIDIA's free cash flow?"
    assert not {q.lower() for q in nxt} & {t['q'].lower() for t in record}


def _down(_body):
    raise ask.decide.JevError("down")


def test_fallback_asks_when_the_company_is_not_covered():
    out = ask.route("Microsoft free cash flow in Q3 FY2025", ask=_down)
    assert not out["sure"] and "Microsoft" in out["unsure"]
    follow = ask.route("What about Tesla's capex?", {"workflow": "finance.reports.search", "report_id": "NBIS-Q2-2026"}, ask=_down)
    assert not follow["sure"]


def test_fallback_asks_when_the_period_is_not_on_file():
    for q in ("NVIDIA Q3 free cash flow", "NVIDIA FY2025 free cash flow"):
        out = ask.route(q, ask=_down)
        assert not out["sure"] and "no filing for" in out["unsure"]
    assert ask.route("and Q1?", {"workflow": "finance.reports.analyze", "report_id": "NVDA-Q2-FY2027"}, ask=_down)["sure"]


def _jev(report_conf=0.99, span_conf=0.99, report="NVDA-Q2-FY2027"):
    return lambda _b: {"workflow": {"choice": "finance.reports.analyze", "confidence": 0.99},
                       "report": {"choice": report, "confidence": report_conf},
                       "span": {"choice": "one", "confidence": span_conf}}


def test_every_choice_the_run_depends_on_must_clear_the_bar():
    assert ask.route("NVIDIA free cash flow", ask=_jev())["sure"]
    low_report = ask.route("NVIDIA free cash flow", ask=_jev(report_conf=0.05))
    assert not low_report["sure"] and low_report["unsure_of"] == "report"
    low_span = ask.route("NVIDIA free cash flow", ask=_jev(span_conf=0.4))
    assert not low_span["sure"] and low_span["unsure_of"] == "span"
    wrong_quarter = ask.route("NVIDIA Q1 free cash flow", ask=_jev(report="NVDA-Q2-FY2027"))
    assert not wrong_quarter["sure"] and wrong_quarter["unsure_of"] == "period"


def test_an_unsure_report_runs_nothing_even_when_a_person_picks_the_workflow(client, monkeypatch):
    monkeypatch.setattr(ask.decide, "asker", lambda **_kw: _jev(report_conf=0.05))
    page = _ask(client, "NVIDIA free cash flow")
    assert "5% sure of the report" in page and "Nothing ran" in page and "name='force'" not in page
    page = _ask(client, "NVIDIA free cash flow", force="finance.reports.analyze")
    assert "Nothing ran" in page
