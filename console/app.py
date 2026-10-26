"""Finrail web console.

A thin shell over the harness, and nothing more: every page reads or acts
through the normal capability path (`dispatch_core.dispatch`), so the rules
a capability declares (approval, background, verification) apply in the
browser exactly as they do on the command line. The console holds no
credentials of its own and adds no privileged path around the dispatcher.
Approvals go through `dispatch_core.approve_safely`, which means the same
caller rule applies here too: an agent cannot approve its own runs, and the
page says so when it happens.
"""

from __future__ import annotations

import html
import json
import os

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from finrail.core import dispatch_core, llm, registry, tracker
from finrail.core.errors import FinrailError
from finrail.core.settings import state

app = FastAPI(title="Finrail")

CSS = """
body { font-family: ui-sans-serif, system-ui, sans-serif; margin: 2rem auto;
       max-width: 62rem; padding: 0 1rem; color: #14213d; }
nav a { margin-right: 1rem; }
table { border-collapse: collapse; width: 100%; }
td, th { border: 1px solid #ccd; padding: .35rem .5rem; text-align: left;
         vertical-align: top; font-size: .92rem; }
pre { background: #f4f4f8; padding: 1rem; overflow-x: auto; white-space: pre-wrap; }
.thesis { font-size: 1.15rem; }
.verdict { font-weight: 700; }
button { padding: .35rem .8rem; }
input[type=text] { width: 32rem; max-width: 90%; padding: .3rem; }
"""

NAV = ("<nav><a href='/'>Home</a><a href='/capabilities'>Capabilities</a>"
       "<a href='/rules'>Rules</a><a href='/screen'>Screen</a>"
       "<a href='/dataroom'>Data room</a><a href='/compute'>Compute</a>"
       "<a href='/memo'>Memo</a><a href='/approvals'>Approvals</a>"
       "<a href='/runs'>Runs &amp; ledger</a></nav><hr>")


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)} · Finrail</title><style>{CSS}</style>"
        f"</head><body>{NAV}<h1>{html.escape(title)}</h1>{body}</body></html>")


_POSTS = {"count": 0}


def _dispatch(name: str, payload: dict = None, task: str = "") -> dict:
    registry.load()
    cap = registry.command(name)
    if cap.kind != "read":
        limit = int(os.environ.get("FINRAIL_DEMO_POST_LIMIT") or 0)
        if limit and _POSTS["count"] >= limit:
            return {"status": "failed", "capability": name,
                    "error": "the public demo's judged-call budget is "
                             "exhausted for now; the read pages and the "
                             "archived results remain available"}
        _POSTS["count"] += 1
    try:
        return dispatch_core.dispatch(name, payload or {}, task=task,
                                      source="console")
    except FinrailError as exc:
        return {"status": "failed", "capability": name, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the page reports, never hides
        return {"status": "failed", "capability": name,
                "error": f"{type(exc).__name__}: {exc}"}


def _result_block(out: dict) -> str:
    if out.get("error"):
        return f"<pre>✗ {html.escape(str(out['error']))}</pre>"
    body = out.get("result") if out.get("result") is not None else out
    return ("<pre>" + html.escape(
        json.dumps(body, indent=2, default=str)) + "</pre>")


def _engine_line() -> str:
    eng = llm.engine()
    if eng == "claude-cli":
        return "engine: claude-cli (local development only, never a judged deployment)"
    _base, _key, model = llm._http_config()
    host = _base.split("//")[-1].split("/")[0] if _base else "(not configured)"
    return f"engine: {eng} · model: {model} · endpoint: {host}"


@app.get("/", response_class=HTMLResponse)
def home():
    registry.load()
    caps = registry.commands()
    finance = [n for n in caps if n.startswith("finance.")]
    body = (
        "<p class='thesis'>A finance-first agent harness where the model can "
        "suggest evidence, but only code can award it trust, and only a human "
        "can release a decision.</p>"
        f"<p>{html.escape(_engine_line())}</p>"
        f"<p>{len(caps)} capabilities loaded, {len(finance)} of them finance. "
        "Every figure a capability states is either carried verbatim by a "
        "cited source or computed deterministically from cited workbook "
        "cells. A screen's VERIFIED status is awarded by a deterministic "
        "evidence gate, never by the model. A memo is released only through "
        "a hash-bound human approval, and the released artefact is re-read "
        "and re-hashed afterwards.</p>"
        "<h2>Demonstration path</h2><ol>"
        "<li><a href='/screen'>Screen</a>: confirm the legal entity, then run "
        "the gated public-risk screen.</li>"
        "<li><a href='/dataroom'>Data room</a>: search the synthetic Meridian "
        "data room and try to draft the planted 24.5% margin into a record. "
        "The claim check refuses it.</li>"
        "<li><a href='/compute'>Compute</a>: deterministic metrics with "
        "cited cells.</li>"
        "<li><a href='/memo'>Memo</a>: draft from gated evidence, then "
        "release through <a href='/approvals'>Approvals</a>.</li>"
        "<li><a href='/runs'>Runs &amp; ledger</a>: every model call, with "
        "engine, model, usage and the full prompt, is a ledger record.</li>"
        "</ol>")
    return _page("Finrail", body)


@app.get("/capabilities", response_class=HTMLResponse)
def capabilities():
    registry.load()
    rows = "".join(
        f"<tr><td>{html.escape(c.name)}</td><td>{html.escape(c.bundle)}</td>"
        f"<td>{html.escape(c.kind)}</td>"
        f"<td>{html.escape(c.description)}</td></tr>"
        for c in sorted(registry.commands().values(), key=lambda c: c.name))
    return _page("Capabilities",
                 f"<table><tr><th>capability</th><th>pack</th><th>kind</th>"
                 f"<th>description</th></tr>{rows}</table>")


@app.get("/rules", response_class=HTMLResponse)
def rules():
    registry.load()
    rows = ""
    for cap in sorted(registry.commands().values(), key=lambda c: c.name):
        r = cap.rules()
        flags = ", ".join(f"{k}={r[k]}" for k in
                          ("read_only", "autonomous", "confirm", "background"))
        rows += (f"<tr><td>{html.escape(cap.name)}</td><td>{flags}</td>"
                 f"<td>{html.escape(r.get('verified_by') or '')}</td>"
                 f"<td>{html.escape(r.get('reason') or '')}</td></tr>")
    return _page("Rules, rendered from the declarations",
                 "<p>There is one declaration site per capability (its class "
                 "attributes). This page renders from those declarations, so "
                 "the documentation cannot drift from the enforcement.</p>"
                 f"<table><tr><th>capability</th><th>rules</th>"
                 f"<th>verified by</th><th>why</th></tr>{rows}</table>")


@app.get("/screen", response_class=HTMLResponse)
def screen_page():
    candidates = _dispatch("finance.screen.candidates",
                           {"company": "Northwind"})
    fixture = _dispatch("finance.screen.fixture", {"company_id": "04123456"})
    archive = _dispatch("finance.screen.archive")
    rows = "".join(
        f"<tr><td>{html.escape(str(c.get('company')))}</td>"
        f"<td>{html.escape(str(c.get('company_id')))}</td>"
        f"<td><form method='post' action='/screen/entity'>"
        f"<input type='hidden' name='company' value='{html.escape(str(c.get('company')))}'>"
        f"<input type='hidden' name='company_id' value='{html.escape(str(c.get('company_id')))}'>"
        f"<button>Confirm this entity</button></form></td></tr>"
        for c in candidates.get("result", {}).get("candidates", []))
    return _page("Public-risk screen",
                 "<p>Step 1: a person confirms the legal entity. Step 2: the "
                 "screen runs and the deterministic gate awards each status. "
                 "The model never chooses the entity and never awards "
                 "VERIFIED.</p>"
                 f"<table><tr><th>company</th><th>number</th><th></th></tr>"
                 f"{rows}</table>"
                 "<form method='post' action='/screen/run'>"
                 "<input type='hidden' name='company_id' value='04123456'>"
                 "<button>Run screen for the confirmed entity</button></form>"
                 "<h2>Fixture screen, classified live by the gate</h2>"
                 "<p>No credentials needed: the quotes below are validated "
                 "verbatim and the statuses awarded by deterministic code "
                 "at request time.</p>" + _result_block(fixture) +
                 "<h2>Screens on record</h2>" + _result_block(archive))


@app.post("/screen/entity")
def screen_entity(company: str = Form(...), company_id: str = Form(...)):
    out = _dispatch("finance.screen.entity",
                    {"company": company, "company_id": company_id})
    return _page("Entity confirmation", _result_block(out))


@app.post("/screen/run")
def screen_run(company_id: str = Form(...)):
    out = _dispatch("finance.screen.run", {"company_id": company_id})
    return _page("Screen result", _result_block(out))


@app.get("/dataroom", response_class=HTMLResponse)
def dataroom_page(q: str = "", doc: str = ""):
    body = ("<form method='get'><input type='text' name='q' "
            f"value='{html.escape(q)}' placeholder='search the data room'>"
            "<button>Search</button></form>")
    if q:
        body += _result_block(_dispatch(
            "finance.dataroom.search", {"query": q, "company": "Meridian"}))
    if doc:
        body += _result_block(_dispatch("finance.dataroom.record",
                                        {"doc": doc}))
    body += ("<h2>Draft a record entry (judged, claim-checked)</h2>"
             "<form method='post' action='/dataroom/propose'>"
             "<input type='text' name='task' placeholder='e.g. state the "
             "FY2025 revenue, or try the planted 24.5% margin'>"
             "<button>Propose</button></form>"
             "<p>Documents: <a href='?doc=im-2026'>im-2026</a> · "
             "<a href='?doc=contract-acme'>contract-acme</a> · "
             "<a href='?doc=board-2026-03'>board-2026-03</a> · "
             "<a href='?doc=draft-note'>draft-note (unverified, planted)</a></p>")
    return _page("Data room: Meridian Components (synthetic)", body)


@app.post("/dataroom/propose")
def dataroom_propose(task: str = Form(...)):
    out = _dispatch("finance.dataroom.propose", task=task)
    return _page("Record proposal", _result_block(out))


@app.get("/compute", response_class=HTMLResponse)
def compute_page(metric: str = ""):
    body = ("<form method='get'><input type='text' name='metric' "
            "placeholder='margin | growth | leverage | covenant_headroom | "
            "cash_conversion'><button>Compute</button></form>")
    if metric:
        body += _result_block(_dispatch("finance.compute.metric",
                                        {"metric": metric}))
    body += ("<p>Arithmetic is Python, from the cited workbook cells, and is "
             "recomputed independently before it is reported. The model may "
             "select a metric; it never does the maths.</p>")
    return _page("Deterministic compute", body)


@app.get("/memo", response_class=HTMLResponse)
def memo_page():
    body = ("<form method='post' action='/memo/draft'>"
            "<input type='text' name='task' value='Draft an IC memo for "
            "Meridian Components from the data room documents'>"
            "<button>Draft memo</button></form>"
            "<p>The draft may state only figures carried by cited sources; "
            "anything else is listed as a gap. Release is a separate, "
            "hash-bound approval on the <a href='/approvals'>Approvals</a> "
            "page.</p>")
    return _page("IC memo", body)


@app.post("/memo/draft")
def memo_draft(task: str = Form(...)):
    out = _dispatch("finance.memo.draft", task=task)
    return _page("Memo draft", _result_block(out))


@app.post("/memo/release")
def memo_release(memo_id: str = Form(...)):
    out = _dispatch("finance.memo.release", {"memo_id": memo_id})
    return _page("Memo release proposal", _result_block(out))


@app.get("/approvals", response_class=HTMLResponse)
def approvals():
    registry.load()
    pending = [r for r in tracker.recent(50) if tracker.awaiting(r)]
    if not pending:
        body = "<p>Nothing is waiting for approval.</p>"
    else:
        body = ""
        for rec in pending:
            cap = registry.command(rec["capability"])
            try:
                shown = cap.render(rec.get("proposal") or {})
            except Exception:  # noqa: BLE001
                shown = json.dumps(rec.get("proposal"), indent=2, default=str)
            body += (f"<h3>{html.escape(rec['capability'])} · run "
                     f"{html.escape(rec['id'])}</h3><pre>"
                     f"{html.escape(str(shown))}</pre>"
                     f"<form method='post' action='/approvals/{rec['id']}/approve'>"
                     "<button>Approve and execute</button></form>"
                     f"<form method='post' action='/approvals/{rec['id']}/drop'>"
                     "<button>Drop</button></form>")
    return _page("Approvals", body)


@app.post("/approvals/{rid}/approve")
def approve(rid: str):
    result = dispatch_core.approve_safely(rid, actor="console")
    if not result.get("approved"):
        result = {**result, "note": (result.get("note") or "") +
                  " | In this public deployment that refusal is expected: "
                  "approval executes only from the operator's own terminal "
                  "or chat door, never from a process an agent started. "
                  "The proposal above remains pending and unexecuted."}
    return _page("Approval result", _result_block(result))


@app.post("/approvals/{rid}/drop")
def drop(rid: str):
    result = tracker.drop(rid, by="console")
    return _page("Dropped", _result_block(result))


@app.get("/runs", response_class=HTMLResponse)
def runs():
    rows = "".join(
        f"<tr><td>{html.escape(r['id'])}</td><td>{html.escape(r['status'])}</td>"
        f"<td>{html.escape(r['capability'])}</td>"
        f"<td>{html.escape(str(r.get('verdict') or ''))}</td></tr>"
        for r in tracker.recent(25))
    ledger = state() / "events" / "_ledger.jsonl"
    calls = ""
    if ledger.exists():
        lines = ledger.read_text().splitlines()[-8:]
        calls = "<pre>" + html.escape("\n".join(lines)) + "</pre>"
    return _page("Runs & ledger",
                 f"<table><tr><th>run</th><th>status</th><th>capability</th>"
                 f"<th>verdict</th></tr>{rows}</table>"
                 "<h2>Latest model calls (model_call events)</h2>"
                 "<p>Each event carries the full prompt, the answer, the "
                 "engine, the model and the usage, so a judged call is "
                 "reconstructable from the ledger alone.</p>" + calls)


def main():
    import uvicorn
    port = os.environ.get("FINRAIL_PORT") or os.environ.get("PORT") or "8000"
    uvicorn.run(app, host=os.environ.get("FINRAIL_HOST", "127.0.0.1"),
                port=int(port))


if __name__ == "__main__":
    main()
