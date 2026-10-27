"""Deterministic console contracts for the investment-workspace redesign.

These checks exercise rendered HTML, real temporary run records and normal
read paths. They never contact a model provider or grant approval authority.
Browser checks cover visual layout, keyboard interaction and clipboard APIs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import importlib

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402


@dataclass
class _Element:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    @property
    def text(self):
        return "".join(child.text if isinstance(child, _Element) else child
                       for child in self.children)

    def find_all(self, tag=None, **attrs):
        found = []
        for child in self.children:
            if not isinstance(child, _Element):
                continue
            if ((tag is None or child.tag == tag)
                    and all(child.attrs.get(key) == value
                            for key, value in attrs.items())):
                found.append(child)
            found.extend(child.find_all(tag, **attrs))
        return found


class _HTML(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, content):
        super().__init__(convert_charrefs=True)
        self.root = _Element("document")
        self.stack = [self.root]
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        node = _Element(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)

    def find_all(self, tag=None, **attrs):
        return self.root.find_all(tag, **attrs)


def _words(node):
    return " ".join(node.text.split())


@pytest.fixture(autouse=True)
def isolated_console(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRAIL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("FINRAIL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FINRAIL_ENGINE", "claude-cli")
    for key in ("NEBIUS_API_KEY", "FINRAIL_ENGINE_API_KEY", "TAVILY_API_KEY",
                "FINRAIL_DEMO_ENTITY", "FINRAIL_DEMO_POST_LIMIT",
                "FINRAIL_DEMO_WORKSPACE"):
        monkeypatch.delenv(key, raising=False)
    from finrail.core import llm

    def no_provider(*_args, **_kwargs):
        raise AssertionError("UI contract tests must not call a model provider")

    monkeypatch.setattr(llm, "call", no_provider)
    console = importlib.import_module("console.app")
    monkeypatch.setitem(console._POSTS, "count", 0)
    return console


@pytest.fixture()
def client(isolated_console):
    with TestClient(isolated_console.app) as instance:
        yield instance


ROUTES = ("/", "/screen", "/dataroom", "/compute", "/memo", "/approvals",
          "/runs", "/capabilities", "/rules", "/cli")


@pytest.mark.parametrize("path", ROUTES)
def test_workspace_routes_have_accessible_wayfinding(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    document = _HTML(response.text)
    assert len(document.find_all("h1")) == 1
    assert _words(document.find_all("h1")[0])
    mains = document.find_all("main", id="main-content")
    assert len(mains) == 1
    assert document.find_all("a", href="#main-content"), "Keyboard skip link is missing"
    navs = document.find_all("nav", **{"aria-label": "Main navigation"})
    assert len(navs) == 1, "The primary navigation needs a unique landmark name"
    current = [link for link in navs[0].find_all("a")
               if link.attrs.get("aria-current") == "page"]
    assert len(current) == 1
    assert current[0].attrs["href"] == path
    assert "synthetic" in _words(document.root).lower(), "The public demo must disclose synthetic data"


def test_mobile_navigation_has_a_real_controlled_region(client):
    document = _HTML(client.get("/").text)
    controls = document.find_all("button", **{"aria-controls": "workspace-nav"})
    assert len(controls) == 1
    assert controls[0].attrs.get("aria-expanded") in {"false", "true"}
    assert _words(controls[0]) or controls[0].attrs.get("aria-label")
    assert len(document.find_all(id="workspace-nav")) == 1


@pytest.mark.parametrize("path,mime", [
    ("/assets/app.css", "text/css"),
    ("/assets/app.js", "javascript"),
])
def test_packaged_assets_are_served_as_assets(client, path, mime):
    response = client.get(path)
    assert response.status_code == 200
    assert mime in response.headers["content-type"]
    assert response.content
    assert not response.text.lstrip().startswith("<!doctype html")


@pytest.mark.parametrize("action,field", [
    ("/dataroom/propose", "task"),
    ("/memo/draft", "task"),
    ("/memo/release", "memo_id"),
])
def test_empty_forms_return_a_recoverable_html_page(client, action, field):
    for data in ({}, {field: ""}):
        response = client.post(action, data=data)
        assert response.status_code in {200, 400, 422}
        assert response.headers["content-type"].startswith("text/html")
        document = _HTML(response.text)
        assert document.find_all("h1")
        assert document.find_all(role="alert"), "Explain why the action could not proceed"
        forms = document.find_all("form", action=action)
        assert forms, "Validation must retain a form the visitor can correct"
        fields = forms[0].find_all("input", name=field) + forms[0].find_all("textarea", name=field)
        assert fields and "required" in fields[0].attrs
        assert document.find_all("a", href="#main-content")


def test_failed_draft_preserves_input_without_injecting_html(client, isolated_console, monkeypatch):
    original = isolated_console._dispatch
    supplied = '<img src=x onerror=alert(1)> State revenue from the cited source.'
    submitted = []

    def dispatch(name, payload=None, task=""):
        if name == "finance.dataroom.propose":
            submitted.append(task)
            return {"status": "failed", "error": "Claim check refused the supplied figure."}
        return original(name, payload, task=task)

    monkeypatch.setattr(isolated_console, "_dispatch", dispatch)
    response = client.post("/dataroom/propose", data={"task": supplied})
    assert response.status_code in {200, 400, 422}
    assert submitted == [supplied]
    document = _HTML(response.text)
    fields = document.find_all("input", name="task") + document.find_all("textarea", name="task")
    assert any((node.attrs.get("value", "") if node.tag == "input" else node.text) == supplied
               for node in fields)
    assert not any("onerror" in node.attrs for node in document.find_all())
    assert document.find_all(role="alert")


def test_compute_defaults_to_a_traceable_readable_margin(client):
    document = _HTML(client.get("/compute").text)
    mains = document.find_all("main", id="main-content")
    assert mains
    text = _words(mains[0])
    assert "15.98%" in text
    assert "0.159751" in text
    assert all(cell in text for cell in ("IncomeStatement!B2", "IncomeStatement!B4", "IncomeStatement!B5"))
    assert all(value in text for value in ("48.2", "29.1", "11.4"))
    selected = [link for link in mains[0].find_all("a")
                if link.attrs.get("aria-current") == "page"]
    assert len(selected) == 1
    assert "metric=margin" in selected[0].attrs.get("href", "")


def test_memo_release_asks_for_the_artifact_id_not_a_run_id(client):
    document = _HTML(client.get("/memo").text)
    fields = document.find_all("input", name="memo_id")
    assert len(fields) == 1
    matching = document.find_all("label", **{"for": fields[0].attrs.get("id")})
    assert matching
    assert "memo id" in _words(matching[0]).lower()
    assert "run id" not in _words(matching[0]).lower()


def _pending_release():
    from finrail.core import tracker
    return tracker.save(
        "finance.memo.release", "Release the synthetic demo memo",
        {"memo_id": "demo-memo", "title": "Meridian Components",
         "sha256": "a" * 64, "full_text": "Synthetic memo for Meridian Components."},
        status=tracker.PENDING, kind="planned", source="console",
    )


def test_pending_approval_is_inspectable_but_not_offered_as_browser_execution(client):
    record = _pending_release()
    document = _HTML(client.get("/approvals").text)
    assert f"finrail approve {record['id']}" in _words(document.root)
    assert "Synthetic memo for Meridian Components." in _words(document.root)
    assert "a" * 64 in _words(document.root)
    for button in document.find_all("button"):
        assert not ("approve and execute" in _words(button).lower()
                    and "disabled" not in button.attrs)


def test_console_approval_route_keeps_the_authority_check(client, isolated_console, monkeypatch):
    from finrail.core import tracker
    record = _pending_release()
    calls = []

    def refused(rid, actor=""):
        calls.append((rid, actor))
        return {"id": rid, "approved": False, "status": "pending",
                "note": "Approval requires the operator's terminal."}

    monkeypatch.setattr(isolated_console.dispatch_core, "approve_safely", refused)
    response = client.post(f"/approvals/{record['id']}/approve")
    assert response.status_code in {200, 403}
    assert response.headers["content-type"].startswith("text/html")
    assert calls == [(record["id"], "console")]
    assert tracker.load(record["id"])["status"] == tracker.PENDING
    assert "operator" in response.text.lower() and "terminal" in response.text.lower()


def test_real_runs_link_to_read_only_detail(client, isolated_console, monkeypatch):
    record = _pending_release()
    listing = _HTML(client.get("/runs").text)
    assert listing.find_all("a", href=f"/runs/{record['id']}")

    def no_dispatch(*_args, **_kwargs):
        raise AssertionError("Inspecting a run must not dispatch or replay its action")

    monkeypatch.setattr(isolated_console, "_dispatch", no_dispatch)
    response = client.get(f"/runs/{record['id']}")
    assert response.status_code == 200
    document = _HTML(response.text)
    assert len(document.find_all("h1")) == 1
    assert record["id"] in _words(document.root)
    assert "Synthetic memo for Meridian Components." in _words(document.root)
    assert "pending" in _words(document.root).lower() or "awaiting" in _words(document.root).lower()


def test_missing_run_is_a_helpful_html_404(client):
    response = client.get("/runs/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    document = _HTML(response.text)
    assert document.find_all("h1")
    assert document.find_all("a", href="/runs")


def test_search_text_cannot_become_page_markup(client):
    query = '<img src=x onerror=alert(1)>'
    response = client.get("/dataroom", params={"q": query})
    assert response.status_code == 200
    document = _HTML(response.text)
    fields = document.find_all("input", name="q")
    assert len(fields) == 1 and fields[0].attrs.get("value") == query
    assert not any("onerror" in node.attrs for node in document.find_all())


def test_model_call_filter_preserves_evidence_after_other_activity(client):
    from finrail.core import events, tracker
    record = tracker.save(
        "finance.memo.draft", "Draft a synthetic memo", {},
        status=tracker.PENDING, kind="judged", source="console",
    )
    events.append(events.LEDGER, "model_call", {
        "capability": "finance.memo.draft",
        "engine": "nebius",
        "engine_model": "nvidia/nemotron-3-super-120b-a12b",
        "model": "nvidia/nemotron-3-super-120b-a12b",
        "prompt": "MODEL_PROMPT_MARKER: Summarize the synthetic source.",
        "answer": "MODEL_ANSWER_MARKER: The source records EUR 48.2m revenue.",
        "usage": {"input_tokens": 56, "output_tokens": 93},
    }, run=record["id"])
    # A busy ledger must not make the last actual model call disappear merely
    # because more than eight unrelated activity events followed it.
    for _ in range(10):
        events.append(events.LEDGER, "message", {
            "text": "NON_MODEL_ONLY_MARKER",
        }, run=record["id"])
    response = client.get("/runs", params={"kind": "model_call"})
    assert response.status_code == 200
    document = _HTML(response.text)
    main = document.find_all("main", id="main-content")[0]
    text = _words(main)
    assert "MODEL_PROMPT_MARKER" in text
    assert "MODEL_ANSWER_MARKER" in text
    assert "nebius" in text and "56" in text and "93" in text
    assert "NON_MODEL_ONLY_MARKER" not in text


def test_document_search_matches_source_titles(client):
    page = client.get('/dataroom', params={'q': 'contract'}).text
    assert 'No matching sources' not in page
    assert 'Matching passage' in page
    assert '31 December 2027' in page


def test_prepared_memo_is_readable_cited_and_keeps_release_pending(client, isolated_console, monkeypatch):
    from console.demo import seed_demo_workspace
    from finrail.core import tracker
    monkeypatch.setenv('FINRAIL_DEMO_WORKSPACE', '1')
    manifest = seed_demo_workspace()
    monkeypatch.setattr(isolated_console, '_DEMO_WORKSPACE', manifest)
    page = client.get('/memo').text
    assert 'Prepared investment memo' in page
    assert 'Manually prepared from synthetic sources' in page
    assert 'Open diligence' in page
    assert '/dataroom?doc=contract-acme' in page
    assert manifest['memo_id'] in page
    assert tracker.awaiting(tracker.load(manifest['records']['memo-review']))
    assert 'Illustrative demo record' in isolated_console._event_html({'kind': 'run', 'data': {'source': 'demo-fixture'}})
