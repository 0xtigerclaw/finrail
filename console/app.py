"""Finrail investment workspace. Presentation never bypasses the CLI dispatcher."""
from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from finrail.core import dispatch_core, llm, registry, tracker
from finrail.core.errors import FinrailError
from finrail.core.settings import state
from .views import (e as _e, badge as _badge, icon, panel, empty, table,
                    cli as _cli, cli_equiv as _cli_equiv, raw as _raw,
                    fields, company_context, time_label, run_link)

app = FastAPI(title="Finrail")
app.mount("/assets", StaticFiles(directory=Path(__file__).parent / "static"), name="assets")
_POSTS = {"count": 0}

def _seed_demo_entity() -> None:
    """Public-demo affordance, operator-declared: when FINRAIL_DEMO_ENTITY
    names a company_id, pre-write the confirmed-entity record so the demo's
    screen run can execute. This replaces no approval: the operator made
    the confirmation when they set the variable, and the record says so.
    Unset, nothing is seeded and the normal confirm-then-run flow applies.
    """
    company_id = os.environ.get("FINRAIL_DEMO_ENTITY") or ""
    if not company_id:
        return
    from finrail.extensions.finance import _entity_candidates, _root
    path = _root() / "entity.json"
    if path.exists():
        return
    match = [c for c in _entity_candidates("")
             if c["company_id"] == company_id]
    if not match:
        return
    path.write_text(json.dumps(
        {"company": match[0]["company"], "company_id": company_id,
         "confirmed_by": "operator (public demo seed)"}, indent=2))

def _entity_record() -> dict:
    from finrail.extensions.finance import _root
    path = _root() / "entity.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except ValueError:
        return {}

_seed_demo_entity()
from .demo import seed_demo_workspace
_DEMO_WORKSPACE = seed_demo_workspace()

def _icon(paths: str) -> str:
    return ("<svg width='16' height='16' viewBox='0 0 24 24' fill='none' "
            "stroke='currentColor' stroke-width='1.7' stroke-linecap='round' "
            "stroke-linejoin='round' aria-hidden='true'>" + paths + "</svg>")


ICONS = {
    "home": _icon("<rect x='3' y='3' width='7' height='7' rx='1'/>"
                  "<rect x='14' y='3' width='7' height='7' rx='1'/>"
                  "<rect x='3' y='14' width='7' height='7' rx='1'/>"
                  "<rect x='14' y='14' width='7' height='7' rx='1'/>"),
    "screen": _icon("<path d='M12 3l7 3v5c0 4.5-3 8.5-7 10-4-1.5-7-5.5-7-10V6z'/>"
                    "<path d='M9 12l2 2 4-4'/>"),
    "dataroom": _icon("<path d='M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z'/>"),
    "compute": _icon("<rect x='5' y='3' width='14' height='18' rx='2'/>"
                     "<path d='M8 7h8'/><path d='M8 12h.01M12 12h.01M16 12h.01M8 16h.01M12 16h.01M16 16h.01'/>"),
    "memo": _icon("<path d='M6 2h9l5 5v15H6z'/><path d='M14 2v6h6'/>"
                  "<path d='M9 13h6M9 17h6'/>"),
    "approvals": _icon("<circle cx='12' cy='12' r='9'/><path d='M8.5 12.5l2.5 2.5 5-5.5'/>"),
    "runs": _icon("<path d='M4 6h16M4 12h16M4 18h10'/>"),
    "capabilities": _icon("<path d='M12 2l8 4.5v9L12 20l-8-4.5v-9z'/><path d='M12 11L4 6.5M12 11l8-4.5M12 11v9'/>"),
    "rules": _icon("<path d='M12 3v18'/><path d='M5 7h14'/><path d='M5 7l-2.5 6a3.5 3.5 0 0 0 5 0L5 7zM19 7l-2.5 6a3.5 3.5 0 0 0 5 0L19 7z'/><path d='M8 21h8'/>"),
    "cli": _icon("<rect x='3' y='4' width='18' height='16' rx='2'/>"
                 "<path d='M7 9l3 3-3 3M12 15h5'/>"),
}

BRAND_MARK = ("<svg width='22' height='22' viewBox='0 0 24 24' fill='none' "
              "stroke='#1b4b8f' stroke-width='2' stroke-linecap='round' "
              "aria-hidden='true'><path d='M5 20L14 4'/><path d='M10 20l9-16'/>"
              "<path d='M4 16h9M7 10h9'/></svg>")



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

def _engine_line() -> str:
    eng = llm.engine()
    if eng == "claude-cli":
        return "engine: claude-cli (local development only, never a judged deployment)"
    _base, _key, model = llm._http_config()
    host = _base.split("//")[-1].split("/")[0] if _base else "(not configured)"
    return f"engine: {eng} · model: {model} · endpoint: {host}"

NAV = [
    ('Workspace', [('home', '/', 'Overview'), ('reports', '/reports', 'Public reports'), ('screen', '/screen', 'Screening'),
                   ('dataroom', '/dataroom', 'Data room'), ('compute', '/compute', 'Analysis'),
                   ('memo', '/memo', 'IC memos'), ('approvals', '/approvals', 'Approvals')]),
    ('System', [('runs', '/runs', 'Activity & ledger'), ('capabilities', '/capabilities', 'Capabilities'),
                ('rules', '/rules', 'Governance rules'), ('cli', '/cli', 'Command reference')]),
]
NAMES = {'finance.screen.run': 'Public-risk screen', 'finance.screen.entity': 'Entity confirmation',
         'finance.dataroom.propose': 'Data-room record', 'finance.memo.draft': 'IC memo draft',
         'finance.memo.release': 'Memo release', 'finance.compute.metric': 'Financial analysis',
         'finance.reports.search': 'Published-report search', 'finance.reports.analyze': 'Source-checked free cash flow'}

ICONS['reports'] = ICONS['dataroom']


def _status(rec: dict) -> tuple[str, str]:
    if rec.get('status') in ('pending', 'running') and (rec.get('worker_pid') or rec.get('background') or rec.get('status') == 'running'):
        return 'running', 'Running'
    if tracker.awaiting(rec):
        return 'pending', 'Awaiting approval'
    status = rec.get('status') or 'pending'
    return status, ''


def _pending() -> list[dict]:
    return [record for record in tracker.recent(50) if tracker.awaiting(record)]


def _page(title: str, body: str, active: str = '', lede: str = '', actions: str = '', status_code: int = 200) -> HTMLResponse:
    pending = len(_pending())
    nav = ''
    for group, items in NAV:
        links = ''
        for key, href, label in items:
            selected = key == active
            count = f"<span class='nav-count'>{pending}</span>" if key == 'approvals' and pending else ''
            links += (f"<a class='nav-link{' is-active' if selected else ''}' href='{href}'"
                      + (" aria-current='page'" if selected else '')
                      + f"><span class='nav-icon'>{ICONS.get(key, '')}</span><span>{label}</span>{count}</a>")
        nav += f"<div class='nav-group'><p class='nav-label'>{group}</p>{links}</div>"
    section = 'System' if active in ('runs', 'capabilities', 'rules', 'cli') else 'Workspace'
    limit = max(0, int(os.environ.get('FINRAIL_DEMO_POST_LIMIT') or 0))
    allowance = (f"{max(0, limit - _POSTS['count'])} of {limit} demo actions remaining on this instance. " if limit else '')
    subtitle = f"<p class='page-lede'>{_e(lede)}</p>" if lede else ''
    engine = llm.engine()
    configured = 'Local development' if engine == 'claude-cli' else f"{engine.title()} configured"
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='theme-color' content='#f7f8fb'>"
        f"<title>{_e(title)} · Finrail</title>"
        "<link rel='stylesheet' href='/assets/app.css?v=2'><script src='/assets/app.js?v=2' defer></script>"
        "</head><body><a class='skip-link' href='#main-content'>Skip to main content</a>"
        "<div class='workspace-shell'><aside class='sidebar' id='workspace-nav' aria-label='Workspace navigation'>"
        f"<button type='button' class='nav-close btn-icon' data-nav-close aria-label='Close navigation'>{icon('close')}</button><a class='brand' href='/' aria-label='Finrail workspace'><span class='brand-mark'>{BRAND_MARK}</span>"
        "<span class='brand-wordmark'>Finrail<span class='brand-caption'>Investment workspace</span></span></a>"
        f"<div class='workspace-context'><span class='context-icon'>{icon('building')}</span>"
        "<span class='context-copy'><strong>Demo workspace</strong><span>Public reports &amp; synthetic cases</span></span></div>"
        f"<nav aria-label='Main navigation'>{nav}</nav>"
        "<div class='sidebar-footer'><details class='environment'><summary>Environment details</summary>"
        f"<p>{_e(_engine_line())}</p><a href='/rules'>View governance rules</a></details>"
        "<span class='brand-caption'>Model proposes. Code verifies.</span></div></aside>"
        "<button type='button' class='nav-backdrop' data-nav-backdrop tabindex='-1' aria-label='Close navigation' hidden></button>"
        "<div class='app-column'><header class='topbar'>"
        f"<button type='button' class='nav-toggle btn-icon' data-nav-toggle aria-controls='workspace-nav' aria-expanded='false' aria-label='Open navigation'>{icon('menu')}</button>"
        f"<div class='breadcrumb'><span>{section}</span>{icon('chevron',14)}<strong>{_e(title)}</strong></div>"
        f"<div class='topbar-right'><span class='demo-chip'>{'Public reports' if active == 'reports' else 'Demo workspace'}</span><span class='provider-label'>{_e(configured)}</span>"
        "<span class='avatar' aria-hidden='true'>F</span></div></header>"
        f"<main class='app-main' id='main-content' tabindex='-1'><div class='page-header'>"
        f"<div><h1 class='page-title'>{_e(title)}</h1>{subtitle}</div><div class='page-actions'>{actions}</div></div>"
        f"{body}<details class='demo-notice'><summary>{icon('info',16)}About this demo</summary>"
        f"<p>{_e(allowance)}Public reports use published issuer disclosures. Meridian and Northwind workflows use labelled synthetic data. This is a shared demo; records reset when the instance restarts. "
        "Approvals execute only from the operator’s terminal. No browser action can release a decision.</p></details>"
        "<footer class='workspace-footer'>Evidence before answers. Human authority before action.</footer></main></div></div>"
        "<div id='ui-status' class='sr-only' role='status' aria-live='polite' aria-atomic='true'></div></body></html>",
        status_code=status_code)


def _error(message: str, back: str = '/dataroom', label: str = 'Return to data room') -> str:
    blocked = 'claim check refused' in str(message).lower()
    title = 'Claim blocked' if blocked else 'The request could not be completed'
    description = 'No record was created. Choose a checked source that supports the figure.' if blocked else 'Review the message below, then try the action again. No automatic retry was made.'
    tone = 'outcome-blocked' if blocked else 'outcome-error'
    return (f"<section class='outcome {tone}' role='alert'><div class='outcome-header'>{icon('shield' if blocked else 'alert',24)}"
            f"<div><h2>{title}</h2><p>{description}</p></div></div><div class='outcome-body'>"
            f"<p class='error-detail'>{_e(message)}</p><a class='btn btn-secondary' href='{_e(back)}'>{_e(label)}</a></div></section>")


def _result_block(out: dict) -> str:
    if out.get('error'):
        return _error(out['error'])
    result = out.get('result') if out.get('result') is not None else out
    return fields(result) + _raw(out) if isinstance(result, dict) else f"<pre class='raw'>{_e(result)}</pre>"


def _run_block(out: dict, approvals_link: bool = True) -> str:
    if out.get('error'):
        if out.get('capability', '').startswith('finance.reports.'):
            return _error(out['error'], '/reports', 'Return to reports')
        return _error(out['error'])
    status, label = _status(out)
    rid = str(out.get('id') or '')
    title = {'done': 'Work completed', 'running': 'Work is in progress', 'pending': 'Operator approval required', 'failed': 'This run needs attention'}.get(status, 'Run outcome')
    refresh = f" data-run-refresh data-refresh-url='/runs/{_e(rid)}'" if status == 'running' and rid else ''
    tone = 'outcome-success' if status == 'done' else 'outcome-error' if status == 'failed' else ''
    result = (f"<section class='outcome {tone}'{refresh}><div class='outcome-header'>{icon('check' if status == 'done' else 'clock',24)}"
              f"<div><h2>{title}</h2><p>{_e(NAMES.get(out.get('capability'), out.get('capability', '')))}</p></div>"
              f"{_badge(status,label)}</div><div class='outcome-body'>")
    if out.get('demo_fixture'):
        result += "<div class='notice demo-record'>Illustrative demo record. This seeded example was authored from the synthetic fixture, not generated by a live model call.</div>"
    if rid:
        result += f"<div class='run-reference'><span>Run ID</span><code>{_e(rid)}</code>{run_link(rid)}</div>"
    if status == 'running':
        result += "<p role='status'>The worker is processing this request. This page checks its GET status; your submission will not be replayed.</p>"
    elif status == 'pending' and approvals_link:
        result += "<p>Review the full proposal below. Approval executes only from the operator’s terminal.</p>" + _cli(f'finrail approve {rid}')
    if out.get('errors'):
        result += "<div class='alert' role='alert'>" + '<br>'.join(_e(item) for item in out['errors']) + '</div>'
    proposal = out.get('proposal')
    if proposal:
        result += "<h3>Full proposal</h3>" + fields(proposal)
    value = out.get('result')
    if value is not None:
        result += '<h3>Result</h3>'
        if out.get('capability', '').startswith('finance.reports.') and isinstance(value, dict):
            from .reports import result_html
            result += result_html(value)
        elif isinstance(value, dict) and 'findings' in value:
            result += _findings_html(value)
        else:
            result += fields(value) if isinstance(value, dict) else f"<pre class='raw'>{_e(value)}</pre>"
        if isinstance(value, dict) and value.get('memo_id'):
            result += _cli(value['memo_id'])
            result += f"<a class='btn btn-secondary' href='/memo?memo_id={quote(str(value['memo_id']),safe='')}'>Prepare release proposal</a>"
    return result + '</div></section>' + _raw(out)


def _run_rows(records: list[dict]) -> str:
    rows = ''
    for record in records:
        status, label = _status(record)
        rid = str(record.get('id') or '')
        name = NAMES.get(record.get('capability'), record.get('capability', 'Run'))
        rows += (f"<tr><td><a class='company-name' href='/runs/{quote(rid,safe='')}'>{_e(name)}</a>"
                 f"{'<span class=demo-record>Illustrative demo record</span>' if record.get('demo_fixture') else ''}<span class='company-meta'>{_e(record.get('task') or rid)}</span></td><td>{_badge(status,label)}</td>"
                 f"<td class='num'>{_e(time_label(record.get('created')))}</td><td>{run_link(rid,'Inspect')}</td></tr>")
    return rows


@app.get('/', response_class=HTMLResponse)
def home():
    from finrail.extensions.finance import _room
    room = _room()
    records = tracker.recent(6)
    pending = _pending()
    company_rows = (
        "<tr><td><div class='company-cell'><span class='company-avatar'>MC</span><div>"
        "<a class='company-name' href='/dataroom'>Meridian Components</a><span class='company-meta'>Precision components · Synthetic</span></div></div></td>"
        f"<td>Data room<span class='company-meta'>{len(room['documents'])} source documents</span></td>"
        f"<td>{_badge('checked','Source-backed analysis')}</td><td><a class='inline-action' href='/dataroom'>Open workspace{icon('arrow',16)}</a></td></tr>"
        "<tr><td><div class='company-cell'><span class='company-avatar'>NC</span><div>"
        "<a class='company-name' href='/screen'>Northwind Components</a><span class='company-meta'>Public-risk fixture · Synthetic</span></div></div></td>"
        "<td>Risk screening<span class='company-meta'>Leadership, regulatory, cyber, distress</span></td>"
        f"<td>{_badge('info','Evidence review')}</td><td><a class='inline-action' href='/screen'>Review screen{icon('arrow',16)}</a></td></tr>")
    companies = panel('Company workspaces', table(['Company', 'Evidence', 'Workflow', ''], company_rows, 'Synthetic company workspaces'), '<span class="panel-meta">2 synthetic datasets</span>')
    recent = panel('Recent work', table(['Activity', 'Status', 'Created', ''], _run_rows(records), 'Recent work') if records else empty('Your workspace is ready', 'Screen a company, inspect the data room, or calculate a metric. Your work will appear here.', '<a class="btn btn-secondary" href="/dataroom">Explore the data room</a>', 'activity'), '<a class="inline-action" href="/runs">View activity</a>')
    review = (f"<p class='review-summary'><strong>{len(pending)}</strong> proposal{'s' if len(pending)!=1 else ''} waiting for an operator.</p>"
              '<a class="btn btn-secondary" href="/approvals">Open review queue</a>') if pending else empty('No approvals waiting', 'Draft a record or memo to prepare a proposal for operator review.', '<a class="inline-action" href="/memo">Draft an IC memo</a>', 'shield')
    guide = ("<ol class='guide-list'><li><a href='/screen'>Confirm, then screen</a><p>Inspect evidence and gate verdicts for Northwind.</p></li>"
             "<li><a href='/dataroom'>Try to plant a figure</a><p>Watch the 24.5% margin claim get blocked.</p></li>"
             "<li><a href='/compute'>Verify the numbers</a><p>Five metrics calculated from cited workbook cells.</p></li></ol>")
    report_intro = panel('Work with published reports', '<p>Explore six quarterly reports from NVIDIA, Nebius and ASML. Reconstruct NVIDIA free cash flow with retrieved evidence and checked source cells.</p><a class="btn" href="/reports">Open public reports</a>')
    body = (report_intro + "<div class='content-grid'><div class='primary-column'>" + companies + recent + "</div><div class='secondary-column'>"
            + panel('Review queue', review) + panel('Start the demo', guide) + '</div></div>'
            + "<div class='trust-strip'><div class='trust-step'>" + icon('file') + "<div><strong>Model proposes</strong><span>Source-backed drafts</span></div></div>"
            + "<div class='trust-step'>" + icon('shield') + "<div><strong>Code verifies</strong><span>Evidence and arithmetic checks</span></div></div>"
            + "<div class='trust-step'>" + icon('check') + "<div><strong>Operator releases</strong><span>Approval bound to the full proposal</span></div></div></div>")
    return _page('Workspace overview', body, 'home', 'The model can suggest evidence. Only code awards trust, and only a person releases a decision.', '<a class="btn" href="/dataroom">Open data room</a>')


@app.get('/reports', response_class=HTMLResponse)
def reports_page(run: str = ''):
    from .reports import workspace
    body = ''
    if run:
        rec = _load_run(run)
        if rec and rec.get('capability', '').startswith('finance.reports.'):
            body += _run_block(rec)
        else:
            body += _error('This report run is unavailable. It may have reset with the demo instance.', '/reports', 'Return to reports')
    body += workspace(bool(os.environ.get('FINRAIL_RAG_CORPUS')))
    return _page('Public reports', body, 'reports', 'Real issuer disclosures. Retrieved evidence. Numbers checked against the original report.')


@app.post('/reports/search', response_class=HTMLResponse)
def report_search(report_id: str = Form(''), query: str = Form('')):
    return _report_action('finance.reports.search', {'report_id': report_id, 'query': query})


@app.post('/reports/analyze', response_class=HTMLResponse)
def report_analyze(report_id: str = Form(''), definition: str = Form('issuer_fcf')):
    return _report_action('finance.reports.analyze', {'report_id': report_id, 'definition': definition})


def _report_action(name: str, payload: dict):
    out = _dispatch(name, payload)
    if out.get('id'):
        return RedirectResponse('/reports?run=' + quote(out['id'], safe=''), status_code=303)
    return _page('Report request needs attention', _error(out.get('error', 'The report action failed.'), '/reports', 'Return to reports'), 'reports', status_code=400)


def _findings_html(fixture: dict) -> str:
    result = fixture.get('result') or fixture
    findings = result.get('findings') or []
    if not findings:
        return fields(result)
    counts = result.get('status_counts') or {}
    summary = ''.join(_badge(key, f'{value} {key}') for key, value in sorted(counts.items()))
    body = f"<div class='verdict-summary'>{summary}<span class='company-meta'>Quotes discarded: {_e(result.get('quotes_discarded',0))}</span></div>"
    for finding in findings:
        candidate = finding.get('candidate') or {}
        evidence = ''
        for item in finding.get('evidence') or []:
            evidence += (f"<div class='evidence-source'><strong>{_e(item.get('publisher',''))}</strong>"
                         f"<span class='company-meta'>{_e(item.get('source_tier',''))}</span>"
                         f"<blockquote>{_e(item.get('quote',''))}</blockquote></div>")
        assertions = ''.join(f'<li>{_e(item)}</li>' for item in candidate.get('assertions') or [])
        body += ("<article class='finding-row'><div class='finding-top'>" + _badge(finding.get('status'))
                 + f"<span class='company-meta'>{_e(candidate.get('category','').capitalize())}</span></div>"
                 f"<h3>{_e(candidate.get('claim',''))}</h3><details class='disclosure'><summary>Assertions and evidence</summary>"
                 f"<ul>{assertions}</ul>{evidence or '<p>No checked supporting evidence.</p>'}</details></article>")
    return body


@app.get('/screen', response_class=HTMLResponse)
def screen_page():
    _seed_demo_entity()
    candidates = _dispatch('finance.screen.candidates', {'company':'Northwind'})
    fixture = _dispatch('finance.screen.fixture', {'company_id':'04123456'})
    archive = _dispatch('finance.screen.archive')
    entity = _entity_record()
    rows = ''
    for item in candidates.get('result',{}).get('candidates',[]):
        rows += (f"<tr><td><strong>{_e(item.get('company'))}</strong><span class='company-meta'>Synthetic screening fixture</span></td>"
                 f"<td class='mono'>{_e(item.get('company_id'))}</td><td><form method='post' action='/screen/entity'>"
                 f"<input type='hidden' name='company' value='{_e(item.get('company'))}'><input type='hidden' name='company_id' value='{_e(item.get('company_id'))}'>"
                 "<button class='btn btn-secondary btn-small'>Confirm this entity</button></form></td></tr>")
    confirmation = table(['Company','Company number',''], rows, 'Confirm the screening entity') if rows else empty('No entity candidates', 'The fixture findings below remain available.')
    on_record = (f"<div class='notice'>{icon('check')}<span>Entity on record: <strong>{_e(entity.get('company'))}</strong> "
                 f"<span class='mono'>{_e(entity.get('company_id'))}</span> · {_e(entity.get('confirmed_by','operator confirmed'))}</span></div>") if entity else '<div class="notice">Confirm the legal entity before running a screen.</div>'
    action = ("<form class='form-actions' method='post' action='/screen/run'><input type='hidden' name='company_id' value='04123456'>"
              f"<button class='btn'{' disabled' if not entity else ''}>Run screen for the confirmed entity</button></form>")
    body = company_context('Northwind Components', 'Company number 04123456 · Public-risk screening', 'NC')
    body += '<div class="workflow-grid"><div class="primary-column">' + panel('Entity confirmation', '<p>A person confirms first. The model never chooses the entity.</p>'+confirmation+on_record+action)
    body += panel('Evidence findings', '<p class="section-description">Fixture screen · Classified live, no credentials needed. One publisher can report; it can never verify.</p>'+_findings_html(fixture)) + '</div>'
    body += '<div class="secondary-column">'+panel('Screening scope', '<ul class="plain-list"><li>Leadership changes</li><li>Regulatory exposure</li><li>Cyber incidents</li><li>Financial distress</li></ul><p class="section-description">Verdicts are awarded by the deterministic evidence gate, never the model.</p>') + _raw(archive,'Screens on record')+'</div></div>'
    return _page('Public-risk screening', body+_cli_equiv('finrail get finance.screen.candidates company=Northwind'), 'screen', 'Confirm the company, inspect the evidence, and understand the verdict.')


@app.post('/screen/entity')
def screen_entity(company: str = Form(''), company_id: str = Form('')):
    if not company.strip() or not company_id.strip():
        return _page('Entity confirmation', _error('Choose a company and company number before confirming.', '/screen','Return to screening'), 'screen')
    return _page('Entity confirmation', _run_block(_dispatch('finance.screen.entity',{'company':company,'company_id':company_id})), 'screen')


@app.post('/screen/run')
def screen_run(company_id: str = Form('')):
    if not company_id.strip():
        return _page('Screen result',_error('Confirm a legal entity before screening.','/screen','Return to screening'),'screen')
    out = _dispatch('finance.screen.run',{'company_id':company_id})
    return _page('Screen result',_run_block(out),'screen')

def _dataroom_body(q: str = '', doc: str = '', task: str = '', error: str = '') -> str:
    from finrail.extensions.finance import _room
    docs = _room()['documents']
    chosen = doc or 'im-2026'
    search = ("<form class='search-form' method='get' action='/dataroom'><label class='sr-only' for='q'>Search the data room</label>"
              f"<span aria-hidden='true'>{icon('search')}</span><input type='search' id='q' name='q' value='{_e(q)}' placeholder='Search documents, figures, or terms'>"
              "<button class='btn btn-secondary' type='submit'>Search</button></form>")
    results = ''
    if q:
        out = _dispatch('finance.dataroom.search', {'query':q,'company':'Meridian'})
        hits = list((out.get('result') or {}).get('hits',[]))
        # Document names and IDs are searchable in addition to source passages.
        seen = {hit.get('doc') for hit in hits}
        for source in docs:
            if source['id'] not in seen and q.casefold() in (source['title'] + ' ' + source['id']).casefold():
                hits.append({'doc': source['id'], 'title': source['title'], 'passage': source['text'][:400]})
        rows = ''.join(f"<tr><td><a href='/dataroom?doc={quote(str(h.get('doc','')),safe='')}&amp;q={quote(q,safe='')}'>{_e(h.get('title',''))}</a></td><td>{_e(h.get('passage',''))}</td></tr>" for h in hits)
        results = panel(f'Search results for “{q}”', table(['Source','Matching passage'],rows,'Data-room search results') if rows else empty('No matching sources', 'Try revenue, covenant, or customer. You can also choose a document below.'))
    listing = ''
    for item in docs:
        checked = item.get('status') != 'unverified_draft'
        selected = item['id'] == chosen
        listing += (f"<a class='document-row{' document-selected' if selected else ''}' href='/dataroom?doc={quote(item['id'],safe='')}'"
                    + (" aria-current='true'" if selected else '') + f">{icon('file',20)}<span><strong class='document-title'>{_e(item['title'].split(' (synthetic')[0])}</strong>"
                    f"<span class='document-meta'>{_e(item['id'])} · {'Checked source' if checked else 'Unverified draft'}</span></span>{icon('chevron',14)}</a>")
    out = _dispatch('finance.dataroom.record', {'doc':chosen})
    record = out.get('result') or {}
    if out.get('error'):
        detail = _error(out['error'],'/dataroom','Return to documents')
    else:
        checked = record.get('status') != 'unverified_draft'
        detail = (f"<div class='document-view'><div class='document-toolbar'>{_badge('checked' if checked else 'unverified_draft')}"
                  f"<code>{_e(record.get('id') or chosen)}</code></div><h2>{_e(record.get('title','Source document'))}</h2>"
                  f"<div class='document-content'><p>{_e(record.get('text',''))}</p></div>"
                  + ("<div class='notice'>This draft cannot support a financial figure. Use it as an open question, then check a source.</div>" if not checked else "<p class='form-hint'>Financial claims must be supported by the exact source text shown here.</p>") + '</div>')
    error_html = f"<div class='alert' role='alert'>{_e(error)}</div>" if error else ''
    draft = (error_html + "<p class='section-description'>Describe the record you need. The claim check validates its figures against the cited document before review.</p>"
             "<form method='post' action='/dataroom/propose'><div class='form-field'><label class='form-label' for='task'>Drafting task</label>"
             f"<textarea id='task' name='task' rows='4' required aria-describedby='draft-hint' placeholder='For example: summarize FY2025 revenue and cite the supporting source.'>{_e(task)}</textarea>"
             "<p class='form-hint' id='draft-hint'>Use checked evidence. Unsupported figures will be blocked.</p></div>"
             "<div class='form-actions'><button class='btn' type='submit'>Propose record</button></div></form>"
             "<div class='example-actions'><span>Try a task</span>"
             "<button class='inline-action' type='button' data-fill-target='task' data-fill-value='State that Meridian revenue FY2025 was EUR 48.2m, citing im-2026.'>Supported revenue claim</button>"
             "<button class='inline-action' type='button' data-fill-target='task' data-fill-value='State that Meridian achieved a 24.5% EBITDA margin in FY2025.'>Test the planted 24.5% margin</button></div>")
    body = company_context('Meridian Components', 'Data room · 4 synthetic source documents') + search + results
    body += "<div class='dataroom-layout'><section class='panel document-list-panel'><div class='panel-header'><h2>Documents</h2><span class='panel-meta'>4 sources</span></div><div class='document-list'>" + listing + '</div></section><section class="panel">' + detail + '</section></div>'
    return body + panel('Draft a record entry', draft) + _cli_equiv('finrail do finance.dataroom.propose "State the FY2025 revenue"')


@app.get('/dataroom',response_class=HTMLResponse)
def dataroom_page(q: str = '', doc: str = ''):
    return _page('Data room',_dataroom_body(q,doc),'dataroom','Read the source. Trace the figure. Prepare a record for review.')


@app.post('/dataroom/propose')
def dataroom_propose(task: str = Form('')):
    if not task.strip():
        return _page('Data room',_dataroom_body(task=task,error='Describe the record you want to draft.'),'dataroom','Read the source. Trace the figure. Prepare a record for review.')
    out = _dispatch('finance.dataroom.propose',task=task.strip())
    body = _run_block(out)
    if out.get('error'):
        body += panel('Revise the drafting task', "<form method='post' action='/dataroom/propose'><div class='form-field'><label for='task'>Drafting task</label>"
                      f"<textarea id='task' name='task' required rows='4'>{_e(task)}</textarea></div><div class='form-actions'><button class='btn'>Propose revised record</button></div></form>")
    return _page('Record proposal',company_context('Meridian Components','Data-room record · Synthetic')+body,'dataroom')


METRIC_LABELS = {'margin':'EBITDA margin','growth':'Revenue growth','leverage':'Net leverage','covenant_headroom':'Covenant headroom','cash_conversion':'Cash conversion'}


@app.get('/compute',response_class=HTMLResponse)
def compute_page(metric: str = 'margin'):
    metric = metric or 'margin'
    tabs = ''.join(f"<a class='tab{' is-active' if key==metric else ''}' href='/compute?metric={key}'"+(" aria-current='page'" if key==metric else '')+f">{label}</a>" for key,label in METRIC_LABELS.items())
    out = _dispatch('finance.compute.metric',{'metric':metric})
    data = out.get('result') or {}
    body = company_context('Meridian Components','FY2025 financials · EUR millions unless stated')
    body += "<nav class='tabs' aria-label='Financial metric'>"+tabs+'</nav>'
    if not out.get('error') and 'value' in data:
        value = float(data['value'])
        displayed = f'{value:.2f}×' if metric=='leverage' else f'{value*100:.2f}%'
        formula = str(data.get('formula',''))
        result = (f"<div class='metric-main'><h2>{_e(METRIC_LABELS.get(metric,metric))}</h2><div class='metric-value num'>{displayed}</div>"
                  f"<p class='metric-formula'>{_e(formula)}</p><p class='form-hint'>Raw value <code>{_e(data['value'])}</code> · independently recomputed</p></div>"
                  "<div class='metric-proof'>"+icon('shield',24)+"<div><strong>Calculated, then verified</strong><p>Python computes the metric from cited workbook cells and checks it independently. The model never does the maths.</p></div></div>")
        rows = ''.join(f"<tr><td>{_e(i.get('label'))}</td><td class='mono'>{_e(i.get('cell'))}</td><td class='num'>{_e(i.get('value'))}</td></tr>" for i in data.get('inputs',[]))
        body += panel('Calculation',result,extra='metric-panel')
        body += panel('Source inputs', table(['Workbook input','Cell reference','Value (EURm)'],rows,'Source workbook cells')+f"<p class='form-hint'>{_e(data.get('workbook'))}</p>")
        body += _raw(out,'Calculation record (JSON)')
    else:
        body += _error(out.get('error') or 'This metric is unavailable.','/compute','Return to financial analysis')
    return _page('Financial analysis',body+_cli_equiv(f'finrail get finance.compute.metric metric={metric}'),'compute','Every number follows a source. Every calculation is checked.')


def _memo_preview(memo_id: str) -> str:
    from finrail.extensions.finance import _root
    if not re.fullmatch(r'[a-f0-9]{12}', memo_id):
        return ''
    path = _root() / 'memos' / f'{memo_id}.json'
    if not path.exists():
        return ''
    try:
        draft = json.loads(path.read_text())
    except (ValueError, OSError):
        return ''
    illustrative = bool(_DEMO_WORKSPACE and _DEMO_WORKSPACE.get('memo_id') == memo_id)
    provenance = ("<p class='demo-record'>Illustrative demo record · Manually prepared from synthetic sources</p>" if illustrative else '')
    content = provenance + f"<h3>{_e(draft.get('title'))}</h3>"
    for section in draft.get('sections', []):
        cites = ' · '.join(f"<a href='/dataroom?doc={quote(str(c), safe='')}'>{_e(c)}</a>" for c in section.get('citations', []))
        content += f"<section class='memo-section'><h3>{_e(section.get('heading'))}</h3><p>{_e(section.get('body'))}</p><p class='form-hint'>Sources: {cites}</p></section>"
    gaps = ''.join(f'<li>{_e(gap)}</li>' for gap in draft.get('gaps', []))
    content += f"<h3>Open diligence</h3><ul class='plain-list'>{gaps}</ul><p class='form-hint'>Memo ID <code>{_e(memo_id)}</code> · Draft, awaiting operator review</p>"
    return panel('Prepared investment memo' if illustrative else 'Memo draft', content)


def _memo_body(task: str = '', memo_id: str = '', error: str = '') -> str:
    memo_id = memo_id or ((_DEMO_WORKSPACE or {}).get('memo_id') or '')
    preview = _memo_preview(memo_id)
    default = 'Draft an IC memo for Meridian Components from the data room documents'
    message = f"<div class='alert' role='alert'>{_e(error)}</div>" if error else ''
    draft = (message + "<p class='section-description'>Assemble the investment case from checked data-room evidence. Missing evidence is listed as a gap.</p>"
             "<form method='post' action='/memo/draft'><div class='form-field'><label class='form-label' for='task'>Drafting task</label>"
             f"<textarea id='task' name='task' rows='5' required>{_e(task if task else default)}</textarea>"
             "<p class='form-hint'>This draft uses the Meridian data room. Northwind screening is a separate synthetic dataset.</p></div>"
             "<div class='form-actions'><button class='btn'>Draft IC memo</button></div></form>")
    release = ("<p class='section-description'>Use the memo artifact ID returned by the completed draft, rather than its run ID.</p>"
               "<form method='post' action='/memo/release'><div class='form-field'><label class='form-label' for='memo_id'>Memo ID</label>"
               f"<input id='memo_id' name='memo_id' type='text' required value='{_e(memo_id)}' placeholder='Memo artifact ID' aria-describedby='memo-id-hint'>"
               "<p class='form-hint' id='memo-id-hint'>A release proposal includes the full memo and its hash.</p></div>"
               "<div class='form-actions'><button class='btn btn-secondary'>Prepare release proposal</button></div></form>")
    note = ("<p>Figures must be supported by cited documents. Gaps remain explicit.</p><p>Release requires a separate, hash-bound operator approval. Only the operator’s terminal can execute that approval.</p>"
            "<a class='inline-action' href='/approvals'>View approval queue</a>")
    return company_context('Meridian Components','Investment committee memo · Synthetic') + "<div class='workflow-grid'><div class='primary-column'>"+preview+panel('Draft an IC memo',draft)+panel('Prepare a release',release)+"</div><div class='secondary-column'>"+panel('Decision controls',note)+"</div></div>"+_cli_equiv('finrail do finance.memo.draft "Draft an IC memo for Meridian Components"')


@app.get('/memo',response_class=HTMLResponse)
def memo_page(memo_id: str = ''):
    return _page('IC memos',_memo_body(memo_id=memo_id),'memo','Build the case from evidence. Keep the release under human control.')


@app.post('/memo/draft')
def memo_draft(task: str = Form('')):
    if not task.strip():
        return _page('IC memos',_memo_body(task=task,error='Describe the memo you want to draft.'),'memo')
    out = _dispatch('finance.memo.draft',task=task.strip())
    body = company_context('Meridian Components','IC memo draft · Synthetic')+_run_block(out)
    if out.get('error'):
        body += _memo_body(task=task)
    return _page('Memo draft',body,'memo')


@app.post('/memo/release')
def memo_release(memo_id: str = Form('')):
    if not memo_id.strip():
        return _page('IC memos',_memo_body(error='Enter the Memo ID from a completed draft.'),'memo')
    return _page('Memo release proposal',_run_block(_dispatch('finance.memo.release',{'memo_id':memo_id.strip()})),'memo')


@app.get('/approvals',response_class=HTMLResponse)
def approvals():
    registry.load()
    pending = _pending()
    notice = (f"<div class='notice'>{icon('shield',20)}<span><strong>Operator approval required.</strong> "
              "Review the full proposal here. Approval executes only from the operator’s terminal; the browser cannot release a decision.</span></div>")
    body = notice
    if not pending:
        body += panel('Review queue',empty('Nothing is waiting for approval','Draft a record in the data room or an IC memo. Proposals requiring approval will appear here.','<a class="btn btn-secondary" href="/dataroom">Explore the data room</a>','shield'))
    for rec in pending:
        rid = str(rec['id'])
        cap = registry.command(rec['capability'])
        try:
            shown = cap.render(rec.get('proposal') or {})
        except Exception:
            shown = json.dumps(rec.get('proposal'),indent=2,default=str)
        proposal = rec.get('proposal') or {}
        digest = (f"<h3>Complete SHA-256</h3><p class='result-value mono'>{_e(proposal['sha256'])}</p>" if proposal.get('sha256') else '')
        content = (("<p class='demo-record'>Illustrative demo record · Pending synthetic proposal</p>" if rec.get('demo_fixture') else '') + f"<div class='review-top'>{_badge('pending')}{run_link(rid)}</div><h3>Full proposal</h3><pre class='raw'>{_e(shown)}</pre>"
                    + digest + _raw(proposal, "Full proposal object") + "<h3>Approve from the operator’s terminal</h3>"+_cli(f'finrail approve {rid}')
                   + f"<form class='form-actions' method='post' action='/approvals/{quote(rid,safe='')}/drop' data-confirm='Drop this proposal? It will not execute.'><button class='btn btn-danger'>Drop proposal</button></form>")
        body += panel(NAMES.get(rec['capability'],rec['capability']),content)
    return _page('Approvals',body,'approvals','Review the exact proposal before the operator approves it.')


@app.post('/approvals/{rid}/approve')
def approve(rid: str):
    result = dispatch_core.approve_safely(rid,actor='console')
    if not result.get('approved'):
        note = str(result.get('note') or '') + ' Approval executes only from the operator’s terminal. The proposal remains pending.'
        return _page('Approval result',_error(note,'/approvals','Return to approvals')+_raw(result),'approvals')
    return _page('Approval result',_result_block(result),'approvals')


@app.post('/approvals/{rid}/drop')
def drop(rid: str):
    try:
        result = tracker.drop(rid,by='console')
    except FinrailError as exc:
        return _page('Proposal not dropped',_error(str(exc),'/approvals','Return to approvals'),'approvals')
    return _page('Proposal dropped',_result_block(result),'approvals')

def _read_events(kind: str = '') -> list[dict]:
    ledger = state() / 'events' / '_ledger.jsonl'
    if not ledger.exists():
        return []
    records = []
    for line in ledger.read_text().splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event,dict) and (not kind or event.get('kind') == kind):
            records.append(event)
    return list(reversed(records[-30:]))


def _event_html(event: dict) -> str:
    data = event.get('data') or {}
    kind = str(event.get('kind') or 'event')
    shown = kind.replace('_',' ').capitalize()
    provenance = "<span class='demo-record'>Illustrative demo record</span>" if (data.get('demo_fixture') or data.get('source') == 'demo-fixture') else ''
    detail = ''
    if kind == 'model_call':
        usage = data.get('usage') or {}
        detail = (f"<p class='event-meta'>{_e(data.get('engine') or 'Engine not recorded')} · "
                  f"{_e(data.get('engine_model') or data.get('model') or 'Model not recorded')} · "
                  f"{_e(usage.get('input_tokens',0))} tokens in · {_e(usage.get('output_tokens',0))} tokens out</p>"
                  "<details class='disclosure'><summary>Inspect prompt and answer</summary><h3>Prompt</h3>"
                  f"<pre class='raw'>{_e(data.get('prompt','Not recorded'))}</pre><h3>Answer</h3><pre class='raw'>{_e(data.get('answer','Not recorded'))}</pre>"
                  +_raw(data,'Full model-call record')+'</details>')
    else:
        detail = _raw(data,'Inspect event data')
    return (f"<article class='event-row'><span class='activity-icon'>{icon('activity' if kind=='model_call' else 'clock')}</span>"
            f"<div class='activity-copy'><div class='event-heading'><h3>{_e(shown)}</h3><span class='event-time'>{_e(time_label(event.get('at')))}</span></div>"
            f"<span class='company-meta'>Event {_e(event.get('seq',''))} · {_e(event.get('thread',''))}</span>{provenance}{detail}</div></article>")


@app.get('/runs',response_class=HTMLResponse)
def runs(kind: str = ''):
    selected = kind if kind == 'model_call' else ''
    records = tracker.recent(25)
    run_table = table(['Work item','Status','Created',''],_run_rows(records),'Run activity') if records else empty('No live work yet','Run a screen or propose a data-room record. The recorded outcome will appear here.','<a class="btn btn-secondary" href="/dataroom">Open data room</a>','activity')
    tabs = ("<nav class='tabs' aria-label='Activity type'>"
            f"<a class='tab{' is-active' if not selected else ''}' href='/runs'>All activity</a>"
            f"<a class='tab{' is-active' if selected else ''}' href='/runs?kind=model_call'>Model calls</a></nav>")
    events = _read_events(selected)
    event_content = '<div class="event-list">'+''.join(_event_html(event) for event in events)+'</div>' if events else empty('No model calls recorded' if selected else 'No ledger events yet','Actual prompts, answers, model names, and token usage appear here after a model-backed workflow. Illustrative demo records are not model calls.','<a class="inline-action" href="/dataroom">Inspect the data room</a>','activity')
    body = panel('Run activity',run_table) + tabs + panel('Model calls' if selected else 'Recent ledger activity',event_content)
    return _page('Activity & ledger',body+_cli_equiv('finrail runs'),'runs','Inspect proposals, outcomes, and the evidence behind each actual model call.')


def _load_run(rid: str) -> dict | None:
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',rid):
        return None
    try:
        return tracker.load(rid)
    except (FinrailError, OSError, ValueError):
        return None


@app.get('/runs/{rid}',response_class=HTMLResponse)
def run_detail(rid: str):
    rec = _load_run(rid)
    if not rec:
        return _page('Run not found',empty('This run is not available','The ID may be incorrect, or this shared demo instance may have restarted.','<a class="btn btn-secondary" href="/runs">Return to activity</a>','activity'),'runs',status_code=404)
    related = [event for event in _read_events() if event.get('run') == rid or (event.get('data') or {}).get('run') == rid]
    body = _run_block(rec)
    if related:
        body += panel('Recorded events','<div class="event-list">'+''.join(_event_html(event) for event in related)+'</div>')
    return _page('Run detail',body,'runs',f"{NAMES.get(rec.get('capability'),rec.get('capability',''))} · {_e(rid)}",'<a class="btn btn-secondary" href="/runs">Back to activity</a>')


@app.get('/runs/{rid}/status')
def run_status(rid: str):
    rec = _load_run(rid)
    if not rec:
        return JSONResponse({'error':'Run not found'},status_code=404)
    status, label = _status(rec)
    return {'id':rid,'status':status,'label':label,'finished':status in ('done','failed'),'awaiting_approval':tracker.awaiting(rec)}


@app.get('/capabilities',response_class=HTMLResponse)
def capabilities():
    registry.load()
    rows = ''.join(f"<tr><td><strong>{_e(cap.name)}</strong></td><td>{_e(cap.bundle)}</td><td>{_badge(cap.kind)}</td><td>{_e(cap.description)}</td></tr>" for cap in sorted(registry.commands().values(),key=lambda cap:cap.name))
    body = panel('Capability registry',table(['Capability','Pack','Execution','Description'],rows,'Live capability registry'),f"<span class='panel-meta'>{len(registry.commands())} declared capabilities</span>")
    return _page('Capabilities',body+_cli_equiv('finrail capabilities'),'capabilities','Live declarations from the harness. Read-only capabilities execute immediately; planned work follows its declared rules.')


@app.get('/rules',response_class=HTMLResponse)
def rules():
    registry.load()
    rows = ''
    for cap in sorted(registry.commands().values(),key=lambda cap:cap.name):
        ruleset = cap.rules()
        flags = ', '.join(f"{key}={ruleset[key]}" for key in ('read_only','autonomous','confirm','background'))
        rows += (f"<tr><td><strong>{_e(cap.name)}</strong></td><td><code>{_e(flags)}</code></td>"
                 f"<td>{_e(ruleset.get('verified_by') or 'Not declared')}</td><td>{_e(ruleset.get('reason') or '—')}</td></tr>")
    body = panel('Rules, rendered from the declarations',table(['Capability','Execution rules','Verification','Reason'],rows,'Declared governance rules'))
    return _page('Governance rules',body+_cli_equiv('finrail rules'),'rules','One declaration site per capability. The same rules apply in the browser and CLI.')


@app.get('/cli',response_class=HTMLResponse)
def cli_page():
    registry.load()
    examples = ("<p class='section-description'>The browser uses the same dispatcher. Every action has a terminal equivalent.</p>"
                '<h3>Read a calculation</h3>'+_cli('finrail get finance.compute.metric metric=margin')
                +'<h3>Propose a record</h3>'+_cli('finrail do finance.dataroom.propose "State the FY2025 revenue"')
                +'<h3>Resolve a question</h3>'+_cli('finrail ask "what is the leverage on Meridian?"'))
    commands = ''
    for cap in sorted(registry.commands().values(),key=lambda cap:cap.name):
        if not cap.name.startswith(('finance.','tasks.')):
            continue
        verb = 'get' if cap.kind == 'read' else 'do'
        commands += f"<tr><td><code>finrail {verb} {_e(cap.name)}</code></td><td>{_badge(cap.kind)}</td><td>{_e(cap.description)}</td></tr>"
    lifecycle = ''.join(_cli(cmd) for cmd in ['finrail capabilities','finrail rules','finrail runs','finrail approve <run-id>','finrail drop <run-id>','finrail doctor'])
    engine = (_cli('FINRAIL_ENGINE=nebius NEBIUS_API_KEY=… finrail capabilities')
              +_cli('FINRAIL_ENGINE=meta FINRAIL_ENGINE_BASE_URL=… FINRAIL_ENGINE_API_KEY=… FINRAIL_MODEL=… finrail capabilities')
              +'<p>No silent fallback: a failing engine fails the run with a canonical error code.</p>')
    body = panel('The three verbs',examples)+panel('Lifecycle',lifecycle)+panel('Finance and proof-pack commands',table(['Command','Execution','Description'],commands,'CLI command registry'))+panel('Engine configuration',engine)
    return _page('Command reference',body,'cli','Read, propose, inspect, and approve through the terminal.')


from starlette.exceptions import HTTPException as StarletteHTTPException


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return _page('Page not found',empty('This page is not available','Return to the workspace or choose a workflow from navigation.','<a class="btn" href="/">Open workspace</a>'),'home',status_code=404)
    return _page('Request unavailable',_error(str(exc.detail),'/','Open workspace'),status_code=exc.status_code)


def main():
    import uvicorn
    port = os.environ.get("FINRAIL_PORT") or os.environ.get("PORT") or "8000"
    uvicorn.run(app, host=os.environ.get("FINRAIL_HOST", "127.0.0.1"),
                port=int(port))

if __name__ == "__main__":
    main()
