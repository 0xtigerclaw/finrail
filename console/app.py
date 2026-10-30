"""Finrail investment workspace. Presentation never bypasses the CLI dispatcher."""
from __future__ import annotations

import html
import hashlib
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
_ASSET_VERSION = hashlib.sha256(b''.join(
    (Path(__file__).parent / 'static' / name).read_bytes()
    for name in ('app.css', 'app.js'))).hexdigest()[:12]
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
              "stroke='currentColor' stroke-width='2' stroke-linecap='round' "
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

PRIMARY = [('home', '/', 'Ask'), ('reports', '/reports', 'Filings'), ('proof', '/proof', 'Proof'), ('runs', '/runs', 'Ledger'),
           ('rules', '/rules', 'Rules')]
MORE = [('approvals', '/approvals', 'Approvals'), ('screen', '/screen', 'Screening (synthetic)'),
        ('capabilities', '/capabilities', 'Capabilities'), ('cli', '/cli', 'Command reference')]
NAMES = {'finance.screen.run': 'Public-risk screen', 'finance.screen.entity': 'Entity confirmation',
         'finance.dataroom.propose': 'Data-room record', 'finance.memo.draft': 'IC memo draft',
         'finance.memo.release': 'Memo release', 'finance.compute.metric': 'Financial analysis',
         'finance.reports.search': 'Published-report search', 'finance.reports.analyze': 'Source-checked figure',
         'finance.reports.multiple': 'Multiple at a stated valuation',
         'finance.reports.record': 'Record a figure', 'finance.reports.webclaims': 'Web claims checked',
         'finance.reports.challenge': 'Proof challenge', 'finance.reports.corpus': 'Corpus check',
         'finance.screen.fixture': 'Public-risk screen (synthetic)', 'finance.ask.route': 'Question routed'}

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


def _nav_link(key: str, href: str, label: str, active: str, pending: int) -> str:
    selected = key == active
    count = f"<span class='nav-count'>{pending}</span>" if key == 'approvals' and pending else ''
    return (f"<a class='nav-link{' is-active' if selected else ''}' href='{href}'"
            + (" aria-current='page'" if selected else '') + f"><span>{label}</span>{count}</a>")


def _engine_short() -> str:
    eng = llm.engine()
    if eng == 'claude-cli':
        return 'Local development engine'
    _base, _key, model = llm._http_config()
    name = (model or 'model not set').split('/')[-1]
    return f"NVIDIA Nemotron on {eng.title()}" if 'nemotron' in name else f"{name} on {eng.title()}"


def _page(title: str, body: str, active: str = '', lede: str = '', actions: str = '', status_code: int = 200,
          header: bool = True) -> HTMLResponse:
    pending = len(_pending())
    primary = ''.join(_nav_link(k, h, l, active, pending) for k, h, l in PRIMARY)
    more_active = any(k == active for k, _h, _l in MORE)
    more = ''.join(_nav_link(k, h, l, active, pending) for k, h, l in MORE)
    nav = (primary + f"<details class='nav-more'><summary class='nav-link{' is-active' if more_active else ''}'>"
           f"{'More: ' + _e(dict((k, l) for k, _h, l in MORE)[active]) if more_active else 'More'}</summary>"
           f"<div class='nav-menu'><p class='nav-label'>Approvals and system</p>{more}</div></details>")
    limit = max(0, int(os.environ.get('FINRAIL_DEMO_POST_LIMIT') or 0))
    allowance = (f"{max(0, limit - _POSTS['count'])} of {limit} demo actions left. " if limit else '')
    subtitle = f"<p class='page-lede'>{_e(lede)}</p>" if lede else ''
    page_header = (f"<div class='page-header'><div><h1 class='page-title'>{_e(title)}</h1>{subtitle}</div>"
                   f"<div class='page-actions'>{actions}</div></div>") if header else ''
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='theme-color' content='#0c0f12'>"
        # Dark by default. ?theme=light switches this tab to the light version and ?theme= switches back.
        # It runs before the stylesheet so the page never paints in the wrong theme first.
        "<script>try{const q=new URLSearchParams(location.search).get('theme');"
        "if(q!==null)sessionStorage.setItem('finrail-theme',q);const t=sessionStorage.getItem('finrail-theme');"
        "if(t)document.documentElement.dataset.theme=t}catch(_){}</script>"
        f"<title>{_e(title)} · Finrail</title>"
        "<link rel='preconnect' href='https://fonts.googleapis.com'><link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
        "<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap'>"
        f"<link rel='stylesheet' href='/assets/app.css?v={_ASSET_VERSION}'><script src='/assets/app.js?v={_ASSET_VERSION}' defer></script>"
        "</head><body><a class='skip-link' href='#main-content'>Skip to main content</a>"
        "<div class='workspace-shell'><header class='masthead'>"
        f"<button type='button' class='nav-toggle btn-icon' data-nav-toggle aria-controls='workspace-nav' aria-expanded='false' aria-label='Open navigation'>{icon('menu')}</button>"
        f"<a class='brand' href='/' aria-label='Finrail proof'><span class='brand-mark'>{BRAND_MARK}</span><span class='brand-wordmark'>Finrail</span></a>"
        "<aside class='sidebar' id='workspace-nav' aria-label='Workspace navigation'>"
        f"<button type='button' class='nav-close btn-icon' data-nav-close aria-label='Close navigation'>{icon('close')}</button>"
        f"<nav aria-label='Main navigation'>{nav}</nav></aside>"
        f"<span class='provider-label' title='{_e(_engine_line())}'>{_e(_engine_short())}</span></header>"
        "<button type='button' class='nav-backdrop' data-nav-backdrop tabindex='-1' aria-label='Close navigation' hidden></button>"
        f"<main class='app-main' id='main-content' tabindex='-1'>{page_header}"
        f"{body}<details class='demo-notice'><summary>{icon('info',16)}About this demo</summary>"
        f"<p>{_e(allowance)}The filings are real NVIDIA, Nebius, ASML and Anthropic disclosures. Only the Northwind screening company is synthetic, and it is labelled. "
        "This demo is shared, and its records reset when the server restarts. Approvals run only from the operator’s terminal. Nothing in a browser can release a decision.</p></details>"
        "<footer class='workspace-footer'>Evidence before answers. Human authority before action.</footer></main></div>"
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


def _refusal(message: str, back: str = '/reports', label: str = 'Return to reports') -> str:
    return (f"<section class='outcome outcome-refused' role='alert'><div class='outcome-header'>{icon('shield',24)}"
            f"<div><h2>Not verified. No answer issued.</h2><p>The proof chain stopped at a check. Retrieval alone never certifies a number, and no fallback answer was calculated from unchecked evidence.</p></div></div><div class='outcome-body'>"
            f"<p class='error-detail'>{_e(message)}</p><a class='btn btn-secondary' href='{_e(back)}'>{_e(label)}</a></div></section>")


def _check_refused(out: dict) -> bool:
    """A check said no (ContractViolation), as opposed to a service that failed. Only
    the first is a refusal by the proof chain; an outage must not be told as one."""
    return any(str(item).startswith('ContractViolation') for item in out.get('errors') or [])


def _approve_ref(rid: str, proposal) -> str:
    """`<id>@<hash>` for the proposal on screen: the operator's approval then holds only
    for what this page showed them (dispatch_core._approvable_digest)."""
    return f"{rid}@{tracker.proposal_digest(proposal)[:12]}" if proposal is not None else rid


def _result_block(out: dict) -> str:
    if out.get('error'):
        return _error(out['error'])
    result = out.get('result') if out.get('result') is not None else out
    return fields(result) + _raw(out) if isinstance(result, dict) else f"<pre class='raw'>{_e(result)}</pre>"


def _run_block(out: dict, approvals_link: bool = True) -> str:
    reports = out.get('capability', '').startswith('finance.reports.')
    if out.get('error'):
        return _error(out['error'], '/reports', 'Return to reports') if reports else _error(out['error'])
    status, label = _status(out)
    if status == 'failed' and reports and _check_refused(out):
        return _refusal(' '.join(str(item).removeprefix('ContractViolation: ') for item in out['errors']))
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
        result += "<p>Review the full proposal below. Approval executes only from the operator’s terminal.</p>" + _cli(f"finrail approve {_approve_ref(rid, out.get('proposal'))}")
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


def _model_call(rid: str) -> dict | None:
    calls = [event for event in _read_events('model_call')
             if event.get('run') == rid or (event.get('data') or {}).get('run') == rid]
    return calls[0] if calls else None


def _proof_body(run: str = '', outcome: dict | None = None, multiple: dict | None = None) -> str:
    from . import proof
    rec = proof.latest(run)
    body = proof.hero()
    if rec and rec.get('status') == 'done' and isinstance(rec.get('result'), dict):
        call = _model_call(rec['id'])
        body += proof.workpaper(rec, call)
        body += "<div class='proof-grid'>" + proof.rail(rec, call) + proof.declaration() + "</div>"
        body += proof.challenge(rec, outcome)
        body += proof.multiple_section(rec, multiple)
    elif rec and _status(rec)[0] == 'running':
        body += proof.empty('running', rec['id'])
    elif rec:
        body += _run_block(rec, approvals_link=False) + proof.empty()
    else:
        body += proof.empty()
    return body + proof.other_rails()


@app.get('/', response_class=HTMLResponse)
def home(chat: str = '', t: int | None = None, q: str = ''):
    from . import chat as conversation
    return _page('Ask', conversation.page(chat or conversation.new_id(), conversation.load(chat), _engine_short(), t, q[:300]), 'home', header=False)


@app.post('/ask')
def ask(question: str = Form(''), chat: str = Form(''), force: str = Form('')):
    from . import chat as conversation
    cid = chat if re.fullmatch(r'[a-f0-9]{10}', chat or '') else conversation.new_id()
    turns = conversation.load(cid)
    if len(question.strip()) >= 3:
        turns.append(conversation.answer(_dispatch, question.strip()[:1500], turns, force))
        conversation.save(cid, turns[-30:])
    return RedirectResponse(f'/?chat={cid}#latest', status_code=303)


@app.get('/proof', response_class=HTMLResponse)
def proof_page(run: str = ''):
    return _page('Proof', _proof_body(run if _load_run(run) else ''), 'proof', header=False)


@app.post('/proof/run')
def proof_run(report_id: str = Form('NVDA-Q2-FY2027'), definition: str = Form('')):
    # An empty definition lets the capability pick the report's default (NVIDIA: issuer FCF; Anthropic: revenue).
    out = _dispatch('finance.reports.analyze', {'report_id': report_id, 'definition': definition})
    if out.get('id'):
        return RedirectResponse('/proof?run=' + quote(out['id'], safe=''), status_code=303)
    from . import proof
    return _page('Proof', proof.hero() + proof.empty(error=out.get('error', 'The proof run could not start.')), 'proof', header=False, status_code=400)


@app.post('/proof/challenge', response_class=HTMLResponse)
def proof_challenge(run_id: str = Form(''), attack: str = Form('')):
    run_id = run_id if _load_run(run_id) else ''
    outcome = _dispatch('finance.reports.challenge', {'run_id': run_id, 'attack': attack}) if run_id else None
    return _page('Proof', _proof_body(run_id, outcome), 'proof', header=False)


@app.post('/proof/multiple', response_class=HTMLResponse)
def proof_multiple(run_id: str = Form(''), valuation_usd_m: str = Form('')):
    rec = _load_run(run_id)
    report_id = ((rec or {}).get('result') or {}).get('report_id', '')
    outcome = _dispatch('finance.reports.multiple', {'report_id': report_id, 'run_id': run_id, 'valuation_usd_m': valuation_usd_m.strip()}) if rec else None
    return _page('Proof', _proof_body(run_id, None, outcome), 'proof', header=False)


@app.get('/reports', response_class=HTMLResponse)
def reports_page(run: str = ''):
    body = ''
    if run:
        rec = _load_run(run)
        if rec and rec.get('capability', '').startswith('finance.reports.'):
            body += _run_block(rec)
        else:
            body += _error('This report run is unavailable. It may have reset with the demo instance.', '/reports', 'Return to reports')
    configured = bool(os.environ.get('FINRAIL_RAG_CORPUS'))
    from .reports import filings
    corpus = _dispatch('finance.reports.corpus') if configured else {'error': 'the managed corpus is not configured on this instance.'}
    body += filings(configured, corpus)
    return _page('Filings', body, 'reports', 'Seven real filings from NVIDIA, Nebius, ASML and Anthropic: what was archived, what was indexed, and what has been checked.')


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
        body += panel('Review queue',empty('Nothing is waiting for approval','Ask to record a checked NVIDIA figure. A proposal that passes the claim check waits here for a person.','<a class="btn btn-secondary" href="/">Open Ask</a>','shield'))
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
                    + digest + _raw(proposal, "Full proposal object") + "<h3>Approve from the operator’s terminal</h3>"+_cli(f"finrail approve {_approve_ref(rid, rec.get('proposal'))}")
                   + f"<form class='form-actions' method='post' action='/approvals/{quote(rid,safe='')}/drop' data-confirm='Drop this proposal? It will not execute.'><button class='btn btn-danger'>Drop proposal</button></form>")
        body += panel(NAMES.get(rec['capability'],rec['capability']),content)
    return _page('Approvals',body,'approvals','Review the exact proposal before the operator approves it.')


@app.post('/approvals/{rid}/approve')
def approve(rid: str):
    # REFUSED HERE, ALWAYS. The caller rule judges the process it runs in, and in this
    # route that is the console server, not whoever sent the request: a console started
    # from a terminal passed it for every browser that could reach the port (review,
    # 29 Oct). The server cannot know who clicked, so it never approves.
    note = ('The browser cannot approve. Approval executes only from the operator’s terminal: '
            f'finrail approve {rid}. The proposal remains pending.')
    return _page('Approval result',_error(note,'/approvals','Return to approvals'),'approvals',status_code=403)


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
    shown = {'decision_call': 'Jev call', 'model_call': 'Model call'}.get(kind, kind.replace('_',' ').capitalize())
    provenance = "<span class='demo-record'>Illustrative demo record</span>" if (data.get('demo_fixture') or data.get('source') == 'demo-fixture') else ''
    detail = ''
    if kind == 'decision_call':
        answers = data.get('answers') or {}
        picks = ''.join(f"<li><code>{_e(name)}</code> → <b>{_e((a or {}).get('choice'))}</b> · "
                        f"{_e(round(float((a or {}).get('confidence') or 0) * 100))}% sure</li>"
                        for name, a in answers.items() if isinstance(a, dict))
        detail = (f"<p class='event-meta'>Jev (TypeSafe) · {_e(data.get('model'))} · {_e(data.get('door'))} · "
                  f"{_e(data.get('duration_ms'))} ms · for <code>{_e(data.get('capability') or 'unknown')}</code></p>"
                  + (f"<p class='error-detail'>{_e(data['error'])}</p>" if data.get('error') else f"<ul class='plain-list'>{picks}</ul>")
                  + "<details class='disclosure'><summary>Inspect what Jev was shown and what it picked</summary>"
                  + _raw({'state': data.get('state'), 'questions': data.get('questions')}, 'Questions and state')
                  + _raw(answers, 'Answers, with probabilities') + '</details>')
    elif kind == 'model_call':
        usage = data.get('usage') or {}
        detail = (f"<p class='event-meta'>{_e(data.get('engine') or 'Engine not recorded')} · "
                  f"{_e(data.get('engine_model') or data.get('model') or 'Model not recorded')} · "
                  f"{_e(usage.get('input_tokens',0))} tokens in · {_e(usage.get('output_tokens',0))} tokens out</p>"
                  "<details class='disclosure'><summary>Inspect prompt and answer</summary><h3>Prompt</h3>"
                  f"<pre class='raw'>{_e(data.get('prompt','Not recorded'))}</pre><h3>Answer</h3><pre class='raw'>{_e(data.get('answer','Not recorded'))}</pre>"
                  +_raw(data,'Full model-call record')+'</details>')
    else:
        detail = _raw(data,'Inspect event data')
    return (f"<article class='event-row' id='ev-{_e(event.get('seq',''))}'><span class='activity-icon'>{icon('activity' if kind in ('model_call','decision_call') else 'clock')}</span>"
            f"<div class='activity-copy'><div class='event-heading'><h3>{_e(shown)}</h3><span class='event-time'>{_e(time_label(event.get('at')))}</span></div>"
            f"<span class='company-meta'>Event {_e(event.get('seq',''))} · {_e(event.get('thread',''))}</span>{provenance}{detail}</div></article>")


@app.get('/runs',response_class=HTMLResponse)
def runs(kind: str = ''):
    selected = kind if kind in ('model_call', 'decision_call') else ''
    records = tracker.recent(25)
    run_table = table(['Work item','Status','Created',''],_run_rows(records),'Run activity') if records else empty('No live work yet','Run a screen or propose a data-room record. The recorded outcome will appear here.','<a class="btn btn-secondary" href="/dataroom">Open data room</a>','activity')
    tabs = ("<nav class='tabs' aria-label='Activity type'>"
            f"<a class='tab{' is-active' if not selected else ''}' href='/runs'>All activity</a>"
            f"<a class='tab{' is-active' if selected == 'model_call' else ''}' href='/runs?kind=model_call'>Nemotron calls</a>"
            f"<a class='tab{' is-active' if selected == 'decision_call' else ''}' href='/runs?kind=decision_call'>Jev calls</a></nav>")
    events = [ev for ev in _read_events(selected) if selected or ev.get('kind') in ('model_call', 'decision_call')]
    event_content = '<div class="event-list">'+''.join(_event_html(event) for event in events)+'</div>' if events else empty('No model calls recorded' if selected else 'No ledger events yet','Actual prompts, answers, model names, and token usage appear here after a model-backed workflow. Illustrative demo records are not model calls.','<a class="inline-action" href="/dataroom">Inspect the data room</a>','activity')
    titles = {'model_call': 'Nemotron calls: full prompt and answer', 'decision_call': 'Jev calls: what it was shown and what it picked'}
    body = panel('Run activity',run_table) + tabs + panel(titles.get(selected, 'Model and Jev calls'),event_content)
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
    for cap in sorted(registry.commands().values(),key=lambda cap:(not cap.name.startswith('finance.'),not cap.name.startswith('tasks.'),cap.name)):
        ruleset = cap.rules()
        flags = ', '.join(f"{key}={ruleset[key]}" for key in ('read_only','autonomous','confirm','background'))
        steps = ' → '.join(f"{st['name']} ({st['by']})" for st in ruleset.get('steps') or [])
        asked = f"<span class='company-meta'>Asked as: {_e(cap.ask_label)}</span>" if cap.ask else ''
        rows += (f"<tr><td><strong>{_e(cap.name)}</strong>{asked}</td><td><code>{_e(flags)}</code></td>"
                 f"<td>{_e(ruleset.get('verified_by') or ('Nothing to verify: it reads only' if ruleset.get('read_only') else 'Not declared'))}</td><td>{_e(steps or '—')}</td><td>{_e(ruleset.get('reason') or '—')}</td></tr>")
    body = panel('Rules, rendered from the declarations',table(['Capability','Execution rules','Verification','Declared steps','Reason'],rows,'Declared governance rules'))
    from finrail.extensions.finance.public_reports import HOUSE_RULES
    house = ''.join(f"<tr><td><strong>{_e(r['id'])}</strong>{' <span class=company-meta>illustrative</span>' if r.get('illustrative') else ''}</td>"
                    f"<td>{_e(r['title'])}</td><td>{_e(r['text'])}</td></tr>" for r in HOUSE_RULES.values())
    body += panel('Our method, applied by code to every figure',
                  "<p class='section-description'>The fund's standing method, written as constants in code. No model and no analyst can "
                  "change one from the browser; a change is a code change a person approves.</p>"
                  + table(['Rule', 'Title', 'What code enforces'], house, 'Method rules'))
    return _page('Governance rules',body+_cli_equiv('finrail rules'),'rules','One declaration site per capability. The same rules apply in the browser and CLI.')


@app.get('/cli',response_class=HTMLResponse)
def cli_page():
    registry.load()
    examples = ("<p class='section-description'>The browser uses the same dispatcher. Every action has a terminal equivalent.</p>"
                '<h3>Check a figure against its filing</h3>'+_cli('finrail do finance.reports.analyze report_id=NVDA-Q2-FY2027 definition=issuer_fcf')
                +'<h3>Propose a record</h3>'+_cli('finrail do finance.reports.record report_id=NVDA-Q2-FY2027 statement="Q2 free cash flow was $21,341m"')
                +'<h3>Check a figure in Anthropic’s S-1</h3>'+_cli('finrail do finance.reports.analyze report_id=ANTH-S1-2026 definition=revenue')
                +'<h3>A multiple on that checked revenue, at a valuation you state</h3>'+_cli('finrail get finance.reports.multiple report_id=ANTH-S1-2026 valuation_usd_m=300000')
                +'<h3>Route a question</h3>'+_cli('finrail get finance.ask.route question="What was NVIDIA free cash flow last quarter?"'))
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
    from finrail.extensions.console import enabled
    if not enabled():
        # The console is a plugin: turned off, it has no surface at all.
        raise SystemExit("The console plugin is disabled. Turn it on with: "
                         "finrail do plugin.enable name=console")
    port = os.environ.get("FINRAIL_PORT") or os.environ.get("PORT") or "8000"
    uvicorn.run(app, host=os.environ.get("FINRAIL_HOST", "127.0.0.1"),
                port=int(port))

if __name__ == "__main__":
    main()
