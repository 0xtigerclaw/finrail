"""Finrail web console.

A thin shell over the harness, and nothing more: every page reads or acts
through the normal capability path (`dispatch_core.dispatch`), so the rules
a capability declares (approval, background, verification) apply in the
browser exactly as they do on the command line. The console holds no
credentials of its own and adds no privileged path around the dispatcher.
Approvals go through `dispatch_core.approve_safely`, which means the same
caller rule applies here too: an agent cannot approve its own runs, and the
page says so when it happens.

The console and the `finrail` CLI are two front doors to one dispatcher.
Each workflow page shows the exact CLI command that does the same thing,
and the CLI page is generated from the live capability registry, so the
two surfaces cannot drift apart.
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
:root {
  --ink: #12263f; --ink-2: #54657d; --paper: #f7f9fc; --card: #ffffff;
  --line: #dfe6ef; --accent: #1b4b8f; --accent-hover: #143a70;
  --accent-ink: #ffffff; --side: #eef2f8; --side-ink: #33465e;
  --side-dim: #54657d; --code: #0e1a2c;
  --ok: #146c43; --ok-bg: #e7f4ec; --warn: #8a5a00; --warn-bg: #fdf3e0;
  --bad: #a02c2c; --bad-bg: #fdecec; --info: #1f4e79; --info-bg: #e9f1f9;
  --shadow: 0 1px 2px rgba(18, 38, 63, .05), 0 1px 3px rgba(18, 38, 63, .07);
  --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, monospace;
}
* { box-sizing: border-box; }
::selection { background: #c9dcf5; }
body { margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, sans-serif;
       background: var(--paper); color: var(--ink); }
a { color: var(--accent); text-underline-offset: 3px; }
h1 { font-size: 1.5rem; font-weight: 650; text-wrap: balance; margin: 0 0 .4rem; }
h2 { font-size: 1.05rem; font-weight: 650; margin: 1.7rem 0 .55rem; text-wrap: balance; }
h3 { font-size: .97rem; font-weight: 650; margin: 1.05rem 0 .4rem; }
p { line-height: 1.55; text-wrap: pretty; }
.lede { color: var(--ink-2); max-width: 46rem; margin-top: 0; }
.layout { display: grid; grid-template-columns: 15.5rem 1fr; min-height: 100dvh; }
aside { background: var(--side); color: var(--side-ink); padding: 1.25rem 1rem;
        position: sticky; top: 0; height: 100dvh; overflow-y: auto;
        scrollbar-width: thin; scrollbar-color: #b9c7da transparent; }
.brand { color: var(--ink); font-weight: 700; font-size: 1.12rem; }
.brand small { display: block; color: var(--side-dim); font-weight: 400;
               font-size: .78rem; margin-top: .3rem; line-height: 1.45; }
.navgroup { margin-top: 1.4rem; }
.navgroup > span { display: block; color: var(--side-dim); font-size: .7rem;
                   text-transform: uppercase; margin-bottom: .4rem; }
aside nav a { display: block; color: var(--side-ink); text-decoration: none;
              padding: .44rem .6rem; border-radius: .5rem; font-size: .92rem; }
aside nav a:hover { background: #e2eaf5; color: var(--ink); }
aside nav a[aria-current="page"] { background: var(--card); color: var(--accent);
              font-weight: 650; box-shadow: var(--shadow); }
a:focus-visible, button:focus-visible, input:focus-visible,
textarea:focus-visible, summary:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 2px; }
.term button:focus-visible { outline-color: #9db9dd; }
main { padding: 1.7rem 2.1rem 3rem; max-width: 72rem; width: 100%; }
.topbar { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center;
          margin-bottom: 1.25rem; }
.pill { display: inline-flex; align-items: center; gap: .4rem; font-size: .8rem;
        background: var(--card); box-shadow: var(--shadow);
        border-radius: 999px; padding: .3rem .75rem; color: var(--ink-2); }
.num { font-variant-numeric: tabular-nums; }
.mono { font-family: var(--mono); font-size: .86em; }
.statstrip { display: flex; flex-wrap: wrap; gap: 0; background: var(--card);
             border-radius: .875rem; box-shadow: var(--shadow);
             padding: 1rem 1.2rem; margin: 1rem 0; }
.statstrip .stat { padding: 0 1.4rem 0 0; margin-right: 1.4rem; }
.statstrip .stat + .stat { border-left: 1px solid var(--line); padding-left: 1.4rem; }
.statstrip .v { font-size: 1.25rem; font-weight: 700; font-variant-numeric: tabular-nums; }
.statstrip .l { color: var(--ink-2); font-size: .83rem; max-width: 13rem; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
         gap: .8rem; margin: 1rem 0; }
.card { background: var(--card); border-radius: .875rem;
        box-shadow: var(--shadow); padding: 1.05rem 1.15rem; }
.card .big { font-size: 1.7rem; font-weight: 700; font-variant-numeric: tabular-nums; }
.card .lbl { color: var(--ink-2); font-size: .85rem; margin-top: .2rem; }
table { border-collapse: collapse; width: 100%; background: var(--card);
        border-radius: .875rem; overflow: hidden; box-shadow: var(--shadow); }
td, th { border-bottom: 1px solid var(--line); padding: .5rem .65rem;
         text-align: left; vertical-align: top; font-size: .92rem; }
tr:last-child td { border-bottom: none; }
th { background: #f2f6fb; font-weight: 600; color: var(--ink-2); font-size: .83rem; }
button, .btn { display: inline-block; padding: .48rem 1rem; border-radius: .5rem;
       border: 1px solid var(--accent); background: var(--accent);
       color: var(--accent-ink); font-size: .92rem; cursor: pointer;
       text-decoration: none; box-shadow: var(--shadow);
       transition: background-color 150ms ease-out; }
button:hover, .btn:hover { background: var(--accent-hover); }
button.ghost, .btn.ghost { background: var(--card); color: var(--accent); }
button.ghost:hover, .btn.ghost:hover { background: #eef3fa; }
button.danger { background: var(--card); color: var(--bad); border-color: var(--bad); }
button.danger:hover { background: var(--bad-bg); }
button:disabled { opacity: .55; cursor: not-allowed; }
input[type=text], textarea { width: 34rem; max-width: 100%; padding: .5rem .65rem;
       border: 1px solid #c3d0e0; border-radius: .5rem; font-size: .95rem;
       background: var(--card); color: var(--ink); caret-color: var(--accent); }
input[type=text]:focus { border-color: var(--accent); }
label { display: block; font-size: .85rem; color: var(--ink-2); margin: .7rem 0 .3rem; }
form.inline { display: inline; }
.badge { display: inline-block; font-size: .82rem; font-weight: 700;
         padding: .3rem .7rem; border-radius: 999px; text-transform: uppercase; }
.b-verified, .b-done { background: var(--ok-bg); color: var(--ok); }
.b-reported, .b-pending, .b-running { background: var(--warn-bg); color: var(--warn); }
.b-rejected, .b-failed, .b-contradicted { background: var(--bad-bg); color: var(--bad); }
.b-partial, .b-conflicting, .b-unresolved { background: var(--info-bg); color: var(--info); }
.b-read, .b-planned, .b-judged, .b-auto { background: #eef2f8; color: var(--ink-2); }
.alert { background: var(--bad-bg); color: var(--bad);
         border-radius: .875rem; padding: .85rem 1.05rem; margin: 1rem 0; }
.notice { background: var(--info-bg); color: var(--info);
          border-radius: .875rem; padding: .85rem 1.05rem; margin: 1rem 0; }
.term { background: var(--code); color: #dbe4f2; border-radius: .875rem;
        padding: .9rem 1rem; font-family: var(--mono); font-size: .86rem;
        overflow-x: auto; margin: .8rem 0; }
.term .cmd { white-space: nowrap; }
.term .cmd::before { content: "$ "; color: #6ee7c8; }
.term button { float: right; background: #1b2940; border-color: #33507e;
               color: #c3cede; padding: .2rem .6rem; font-size: .78rem; box-shadow: none; }
.term button:hover { background: #24365a; }
blockquote { margin: .45rem 0; padding: .6rem .85rem; border-radius: .5rem;
             background: var(--paper); font-size: .92rem; }
details.raw { margin-top: .8rem; }
details.clieq { margin-top: 1.5rem; color: var(--ink-2); font-size: .92rem; }
details.clieq summary { cursor: pointer; }
.flowlist { list-style: none; margin: 1rem 0; padding: 0; }
.flowlist li { display: flex; gap: .85rem; align-items: baseline;
               padding: .7rem 0; border-bottom: 1px solid var(--line); }
.flowlist li:last-child { border-bottom: none; }
.flowlist .n { flex: none; display: inline-flex; align-items: center;
               justify-content: center; width: 1.5rem; height: 1.5rem;
               border-radius: 999px; background: var(--accent);
               color: var(--accent-ink); font-weight: 700; font-size: .85rem;
               transform: translateY(.3rem); }
.flowlist .t { font-weight: 650; }
.flowlist .d { color: var(--ink-2); }
details.raw pre, pre.raw { background: #eef2f8; border-radius: .875rem; padding: 1rem;
             overflow-x: auto; white-space: pre-wrap; font-size: .84rem; }
.steps { display: grid; grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
         gap: .8rem; }
.step .n { display: inline-flex; align-items: center; justify-content: center;
           width: 1.6rem; height: 1.6rem; border-radius: 999px;
           background: var(--accent); color: var(--accent-ink);
           font-weight: 700; font-size: .9rem; margin-bottom: .5rem; }
.empty { border: 1px dashed #c3d0e0; border-radius: .875rem; padding: 1.4rem;
         background: var(--card); color: var(--ink-2); }
footer { color: var(--ink-2); font-size: .84rem; margin-top: 2.5rem;
         border-top: 1px solid var(--line); padding-top: 1rem; }
@media (max-width: 54rem) {
  .layout { grid-template-columns: 1fr; }
  aside { position: static; height: auto; padding: .9rem 1rem .6rem; }
  .brand small { display: none; }
  .navgroup { margin-top: .7rem; }
  .navgroup > span { display: none; }
  aside nav { display: flex; flex-wrap: wrap; gap: .25rem; }
  aside nav a { padding: .35rem .6rem; background: #e2eaf5; }
  main { padding: 1.1rem 1rem 2.5rem; }
}
"""

NAV = [
    ("Demo flow", [("home", "/", "Overview"),
                   ("screen", "/screen", "1 · Public-risk screen"),
                   ("dataroom", "/dataroom", "2 · Data room"),
                   ("compute", "/compute", "3 · Compute"),
                   ("memo", "/memo", "4 · IC memo"),
                   ("approvals", "/approvals", "5 · Approvals")]),
    ("Inspect", [("runs", "/runs", "Runs & ledger"),
                 ("capabilities", "/capabilities", "Capabilities"),
                 ("rules", "/rules", "Rules"), ("cli", "/cli", "CLI")]),
]


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

COPY_JS = """
document.querySelectorAll("[data-copy]").forEach(function (b) {
  b.addEventListener("click", function () {
    navigator.clipboard.writeText(b.getAttribute("data-copy"));
    b.textContent = "Copied";
    setTimeout(function () { b.textContent = "Copy"; }, 1500);
  });
});
"""


def _e(value) -> str:
    return html.escape(str(value))


def _page(title: str, body: str, active: str = "", lede: str = "") -> HTMLResponse:
    nav = ""
    for group, items in NAV:
        links = "".join(
            f"<a href='{href}'"
            + (" aria-current='page'" if key == active else "")
            + f">{ICONS.get(key, '')}{label}</a>"
            for key, href, label in items)
        nav += f"<div class='navgroup'><span>{group}</span><nav>{links}</nav></div>"
    lede_html = f"<p class='lede'>{lede}</p>" if lede else ""
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_e(title)} · Finrail</title>"
        f"<style>{CSS}</style></head><body>"
        "<div class='layout'><aside>"
        f"<div class='brand'>{BRAND_MARK}<div>Finrail<small>A finance-first agent harness. The model "
        "suggests evidence; only code awards it trust; only a human releases a "
        "decision.</small></div></div>"
        f"{nav}</aside><main>"
        f"<div class='topbar'><span class='pill'>{_e(_engine_line())}</span>"
        "</div>"
        f"<h1>{_e(title)}</h1>{lede_html}{body}"
        "<footer>Same dispatcher as the CLI. No privileged browser path.</footer>"
        f"</main></div><script>{COPY_JS}</script></body></html>")


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


def _badge(status) -> str:
    s = str(status or "").lower()
    return f"<span class='badge b-{_e(s)}'>{_e(s)}</span>"


def _cli(cmd: str) -> str:
    return (f"<div class='term'><button type='button' data-copy='{_e(cmd)}' "
            f"aria-label='Copy command'>Copy</button>"
            f"<span class='cmd'>{_e(cmd)}</span></div>")


def _cli_equiv(cmd: str) -> str:
    return ("<details class='clieq'><summary>Same thing from the terminal"
            f"</summary>{_cli(cmd)}</details>")


def _raw(out) -> str:
    return ("<details class='raw'><summary>Raw result (JSON)</summary><pre>"
            + _e(json.dumps(out, indent=2, default=str)) + "</pre></details>")


def _result_block(out: dict) -> str:
    if out.get("error"):
        return f"<div class='alert' role='alert'>Failed: {_e(out['error'])}</div>"
    body = out.get("result") if out.get("result") is not None else out
    return ("<pre class='raw'>"
            + _e(json.dumps(body, indent=2, default=str)) + "</pre>")


def _run_block(out: dict, approvals_link: bool = True) -> str:
    """A proposed or finished run, shown as its status and proposal."""
    if out.get("error"):
        return f"<div class='alert' role='alert'>Failed: {_e(out['error'])}</div>"
    status = out.get("status", "")
    body = (f"<div class='card'><p>{_badge(status)} "
            f"<span class='mono'>{_e(out.get('capability', ''))}</span> · run "
            f"<span class='mono'>{_e(out.get('id', ''))}</span></p>")
    if status == "pending" and approvals_link:
        body += ("<p>This proposal now waits for a person. Review and act on "
                 "it on the <a href='/approvals'>Approvals</a> page, or from "
                 "the terminal:</p>"
                 + _cli(f"finrail approve {out.get('id', '<id>')}"))
    proposal = out.get("proposal")
    if proposal:
        body += ("<h3>Proposal</h3><pre class='raw'>"
                 + _e(json.dumps(proposal, indent=2, default=str)) + "</pre>")
    result = out.get("result")
    if result is not None:
        body += ("<h3>Result</h3><pre class='raw'>"
                 + _e(json.dumps(result, indent=2, default=str)) + "</pre>")
    return body + "</div>" + _raw(out)


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
    pending = len([r for r in tracker.recent(50) if tracker.awaiting(r)])
    runs = tracker.recent(25)
    body = (
        f"<p><span class='num'>{len(caps)}</span> capabilities loaded · "
        f"<span class='num'>{len(finance)}</span> finance · "
        f"<span class='num'>{pending}</span> waiting for a person · "
        f"<span class='num'>{len(runs)}</span> recent runs</p>"
        "<p><a class='btn' href='/screen'>Start the demo: confirm the "
        "entity</a></p>"
        "<ol class='flowlist'>"
        "<li><span class='n'>1</span><div><a class='t' href='/screen'>"
        "Confirm, then screen</a><div class='d'>A person confirms the legal "
        "entity; the evidence gate awards every status.</div></div></li>"
        "<li><span class='n'>2</span><div><a class='t' href='/dataroom'>"
        "Try to plant a figure</a><div class='d'>Draft the planted 24.5% "
        "margin into a record; the claim check refuses it.</div></div></li>"
        "<li><span class='n'>3</span><div><a class='t' href='/compute'>"
        "Deterministic compute</a><div class='d'>Five metrics from cited "
        "workbook cells, recomputed by Python.</div></div></li>"
        "<li><span class='n'>4</span><div><a class='t' href='/memo'>"
        "Draft, then release</a><div class='d'>A memo from gated evidence "
        "only, released through a hash-bound approval.</div></div></li>"
        "<li><span class='n'>5</span><div><a class='t' href='/runs'>"
        "Read the ledger</a><div class='d'>Every judged call "
        "reconstructable: prompt, answer, engine, usage.</div></div></li>"
        "</ol>"
        "<p>Prefer the terminal? The <a href='/cli'>CLI page</a> lists "
        "every command from the live registry.</p>")
    return _page("Finrail", body, active="home",
                 lede="A finance-first agent harness where the model can "
                      "suggest evidence, but only code can award it trust, "
                      "and only a human can release a decision. Every figure "
                      "stated here is either carried verbatim by a cited "
                      "source or computed deterministically from cited "
                      "workbook cells.")


@app.get("/capabilities", response_class=HTMLResponse)
def capabilities():
    registry.load()
    rows = "".join(
        f"<tr><td class='mono'>{_e(c.name)}</td><td>{_e(c.bundle)}</td>"
        f"<td>{_badge(c.kind)}</td><td>{_e(c.description)}</td></tr>"
        for c in sorted(registry.commands().values(), key=lambda c: c.name))
    return _page("Capabilities",
                 "<p>What exists, read from the registry at request time. "
                 "The same list is one terminal command:</p>"
                 + _cli("finrail capabilities")
                 + f"<table><tr><th>capability</th><th>pack</th><th>kind</th>"
                   f"<th>description</th></tr>{rows}</table>",
                 active="capabilities",
                 lede="Reads run immediately. Planned and judged capabilities "
                      "propose first and execute only under their declared "
                      "approval rules.")


@app.get("/rules", response_class=HTMLResponse)
def rules():
    registry.load()
    rows = ""
    for cap in sorted(registry.commands().values(), key=lambda c: c.name):
        r = cap.rules()
        flags = ", ".join(f"{k}={r[k]}" for k in
                          ("read_only", "autonomous", "confirm", "background"))
        rows += (f"<tr><td class='mono'>{_e(cap.name)}</td>"
                 f"<td class='mono'>{_e(flags)}</td>"
                 f"<td>{_e(r.get('verified_by') or '')}</td>"
                 f"<td>{_e(r.get('reason') or '')}</td></tr>")
    return _page("Rules, rendered from the declarations",
                 "<p>There is one declaration site per capability, its class "
                 "attributes. This page renders from those declarations, so "
                 "the documentation cannot drift from the enforcement.</p>"
                 + _cli("finrail rules")
                 + f"<table><tr><th>capability</th><th>rules</th>"
                   "<th>verified by</th><th>why</th></tr>" + rows + "</table>",
                 active="rules")


def _findings_html(fixture: dict) -> str:
    result = fixture.get("result") or fixture
    findings = result.get("findings") or []
    if not findings:
        return _result_block(fixture)
    cards = ""
    for f in findings:
        cand = f.get("candidate") or {}
        assertions = "".join(f"<li>{_e(a)}</li>"
                             for a in cand.get("assertions") or [])
        evidence = ""
        for ev in f.get("evidence") or []:
            evidence += (
                f"<p><strong>{_e(ev.get('publisher', ''))}</strong> · "
                f"{_e(ev.get('source_tier', ''))} · supports "
                f"{_e(ev.get('supports_assertions') or [])}</p>"
                f"<blockquote>{_e(ev.get('quote', ''))}</blockquote>")
        cards += (
            "<div class='card'><p>" + _badge(f.get("status"))
            + f" <span class='badge b-read'>{_e(cand.get('category', ''))}</span></p>"
            f"<h3>{_e(cand.get('claim', ''))}</h3>"
            f"<details><summary>Assertions and evidence</summary>"
            f"<ul>{assertions}</ul>{evidence}</details></div>")
    counts = result.get("status_counts") or {}
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    return (f"<p>Status counts: <span class='num'>{_e(summary)}</span> · "
            f"quotes discarded as unverifiable: "
            f"<span class='num'>{_e(result.get('quotes_discarded', 0))}</span></p>"
            + "<div class='steps'>" + cards + "</div>")


@app.get("/screen", response_class=HTMLResponse)
def screen_page():
    candidates = _dispatch("finance.screen.candidates",
                           {"company": "Northwind"})
    fixture = _dispatch("finance.screen.fixture", {"company_id": "04123456"})
    archive = _dispatch("finance.screen.archive")
    rows = "".join(
        f"<tr><td>{_e(c.get('company'))}</td>"
        f"<td class='mono'>{_e(c.get('company_id'))}</td>"
        f"<td><form method='post' action='/screen/entity'>"
        f"<input type='hidden' name='company' value='{_e(c.get('company'))}'>"
        f"<input type='hidden' name='company_id' value='{_e(c.get('company_id'))}'>"
        "<button>Confirm this entity</button></form></td></tr>"
        for c in candidates.get("result", {}).get("candidates", []))
    if not rows:
        rows = ("<tr><td colspan='3'>No candidates answered. The archived "
                "and fixture screens below remain available.</td></tr>")
    return _page("Public-risk screen",
                 "<h2>Confirm the entity, then run</h2>"
                 "<p>The model never chooses the entity; a person confirms "
                 "the exact company and registry number first.</p>"
                 f"<table><tr><th>company</th><th>number</th><th></th></tr>"
                 f"{rows}</table>"
                 "<p>Then run the gated screen for the confirmed entity:</p>"
                 "<form method='post' action='/screen/run'>"
                 "<input type='hidden' name='company_id' value='04123456'>"
                 "<button>Run screen for the confirmed entity</button></form>"
                 + _cli_equiv("finrail get finance.screen.candidates "
                              "company=Northwind")
                 + "<h2>Fixture screen, classified live by the gate</h2>"
                 "<p>Classified live by the gate, no credentials needed. "
                 "One publisher per assertion can report a finding; it can "
                 "never verify it.</p>" + _findings_html(fixture)
                 + "<details class='clieq'><summary>Screens on record</summary>"
                 + _result_block(archive) + "</details>",
                 active="screen",
                 lede="Leadership, regulatory, cyber and distress screening "
                      "where escalation is deterministic even though "
                      "discovery is not.")


@app.post("/screen/entity")
def screen_entity(company: str = Form(...), company_id: str = Form(...)):
    out = _dispatch("finance.screen.entity",
                    {"company": company, "company_id": company_id})
    return _page("Entity confirmation", _run_block(out), active="screen")


@app.post("/screen/run")
def screen_run(company_id: str = Form(...)):
    out = _dispatch("finance.screen.run", {"company_id": company_id})
    return _page("Screen result", _run_block(out), active="screen")


@app.get("/dataroom", response_class=HTMLResponse)
def dataroom_page(q: str = "", doc: str = ""):
    body = ("<form method='get' action='/dataroom'>"
            "<label for='q'>Search the data room</label>"
            f"<input type='text' id='q' name='q' value='{_e(q)}' "
            "placeholder='revenue, covenant, customer…'> "
            "<button>Search</button></form>")
    if q:
        out = _dispatch("finance.dataroom.search",
                        {"query": q, "company": "Meridian"})
        result = out.get("result") or {}
        hits = result.get("hits") if isinstance(result, dict) else None
        if hits is not None:
            rows = "".join(
                f"<tr><td class='mono'><a href='?doc={_e(h.get('doc'))}'>"
                f"{_e(h.get('doc'))}</a></td><td>{_e(h.get('title'))}</td>"
                f"<td>{_e(h.get('passage'))}</td>"
                f"<td class='num'>{_e(h.get('score'))}</td></tr>"
                for h in hits)
            body += (f"<h2>Hits for “{_e(q)}”</h2><table><tr><th>document</th>"
                     f"<th>title</th><th>passage</th><th>score</th></tr>"
                     f"{rows}</table>" if rows else
                     "<div class='empty'>No documents carry that term. Try "
                     "<span class='mono'>revenue</span> or "
                     "<span class='mono'>covenant</span>.</div>")
        else:
            body += _result_block(out)
    if doc:
        out = _dispatch("finance.dataroom.record", {"doc": doc})
        result = out.get("result") or {}
        if isinstance(result, dict) and result.get("text"):
            body += (f"<h2>{_e(result.get('title', doc))}</h2>"
                     f"<p><span class='mono'>{_e(result.get('id', doc))}</span>"
                     + (f" · {_badge(result.get('status'))}"
                        if result.get("status") else "")
                     + f"</p><div class='card'><p>{_e(result['text'])}</p></div>")
        else:
            body += _result_block(out)
    body += ("<h2>Documents</h2><p>"
             "<a href='?doc=im-2026'>im-2026</a> · "
             "<a href='?doc=contract-acme'>contract-acme</a> · "
             "<a href='?doc=board-2026-03'>board-2026-03</a> · "
             "<a href='?doc=draft-note'>draft-note</a> "
             "(unverified draft, planted)</p>"
             "<h2>Draft a record entry (judged, claim-checked)</h2>"
             "<p>Ask for a true figure and it is drafted for approval. Ask "
             "for the planted 24.5% margin and the claim check refuses it "
             "before any approval exists.</p>"
             "<form method='post' action='/dataroom/propose'>"
             "<label for='task'>Drafting task</label>"
             "<input type='text' id='task' name='task' placeholder='e.g. "
             "state the FY2025 revenue, or try the planted 24.5% margin'> "
             "<button>Propose</button></form>"
             + _cli_equiv('finrail do finance.dataroom.propose "State that '
                          'Meridian achieved a 24.5% EBITDA margin in '
                          'FY2025"'))
    return _page("Data room: Meridian Components (synthetic)", body,
                 active="dataroom",
                 lede="Search and judged drafting over a private data room "
                      "where every figure must be carried by the document it "
                      "cites, and an unverified draft can carry nothing.")


@app.post("/dataroom/propose")
def dataroom_propose(task: str = Form(...)):
    out = _dispatch("finance.dataroom.propose", task=task)
    return _page("Record proposal", _run_block(out), active="dataroom")


@app.get("/compute", response_class=HTMLResponse)
def compute_page(metric: str = ""):
    metrics = ["margin", "growth", "leverage", "covenant_headroom",
               "cash_conversion"]
    chips = " ".join(f"<a class='btn ghost' href='?metric={m}'>{m}</a>"
                     for m in metrics)
    body = f"<p>{chips}</p>"
    if metric:
        out = _dispatch("finance.compute.metric", {"metric": metric})
        result = out.get("result") or {}
        if isinstance(result, dict) and "value" in result:
            rows = "".join(
                f"<tr><td class='mono'>{_e(i.get('cell'))}</td>"
                f"<td>{_e(i.get('label'))}</td>"
                f"<td class='num'>{_e(i.get('value'))}</td></tr>"
                for i in result.get("inputs") or [])
            body += (
                "<div class='card'>"
                f"<div class='lbl'>{_e(result.get('metric'))}</div>"
                f"<div class='big num'>{_e(result.get('value'))}</div>"
                f"<p>{_e(result.get('formula'))}</p>"
                "<table><tr><th>workbook cell</th><th>input</th>"
                f"<th>value (EURm)</th></tr>{rows}</table>"
                f"<p class='lbl'>{_e(result.get('workbook'))}</p></div>")
        else:
            body += _result_block(out)
    body += ("<p>Arithmetic is Python, from the cited workbook cells, and is "
             "recomputed independently before it is reported. The model may "
             "select a metric; it never does the maths.</p>"
             + _cli_equiv("finrail get finance.compute.metric metric=margin"))
    return _page("Deterministic compute", body, active="compute",
                 lede="Five metrics a credit or IC discussion actually "
                      "needs, each traceable to the cells it came from.")


@app.get("/memo", response_class=HTMLResponse)
def memo_page():
    body = ("<form method='post' action='/memo/draft'>"
            "<label for='task'>Drafting task</label>"
            "<input type='text' id='task' name='task' value='Draft an IC "
            "memo for Meridian Components from the data room documents'> "
            "<button>Draft memo</button></form>"
            "<p>The draft may state only figures carried by cited sources; "
            "anything else is listed as a gap. A figure the sources do not "
            "carry is refused outright.</p>"
            "<h2>Release a drafted memo</h2>"
            "<form method='post' action='/memo/release'>"
            "<label for='memo_id'>Memo run id (from the draft)</label>"
            "<input type='text' id='memo_id' name='memo_id' "
            "placeholder='memo run id'> <button>Propose release</button></form>"
            "<p>Release is a separate proposal: the full memo text and its "
            "hash are what a person approves, and after release the artefact "
            "is re-read and re-hashed.</p>"
            + _cli_equiv('finrail do finance.memo.draft "Draft an IC memo '
                         'for Meridian Components"'))
    return _page("IC memo", body, active="memo",
                 lede="Drafting from gated evidence only, and release as a "
                      "human decision bound to the exact text approved.")


@app.post("/memo/draft")
def memo_draft(task: str = Form(...)):
    out = _dispatch("finance.memo.draft", task=task)
    return _page("Memo draft", _run_block(out), active="memo")


@app.post("/memo/release")
def memo_release(memo_id: str = Form(...)):
    out = _dispatch("finance.memo.release", {"memo_id": memo_id})
    return _page("Memo release proposal", _run_block(out), active="memo")


@app.get("/approvals", response_class=HTMLResponse)
def approvals():
    registry.load()
    pending = [r for r in tracker.recent(50) if tracker.awaiting(r)]
    if not pending:
        body = ("<div class='empty'><p>Nothing is waiting for approval.</p>"
                "<p>Draft a record in the <a href='/dataroom'>data room</a> "
                "or a memo on the <a href='/memo'>IC memo</a> page and its "
                "proposal will appear here, in full, before anything "
                "executes.</p></div>")
    else:
        body = ("<div class='notice'>Approval executes only from the "
                "operator's own terminal or chat door, never from a process "
                "an agent started. In this public deployment an approval "
                "attempted here is therefore expected to be refused; the "
                "proposal remains pending and unexecuted. From the terminal: "
                "<span class='mono'>finrail approve &lt;id&gt;</span></div>")
        for rec in pending:
            cap = registry.command(rec["capability"])
            try:
                shown = cap.render(rec.get("proposal") or {})
            except Exception:  # noqa: BLE001
                shown = json.dumps(rec.get("proposal"), indent=2, default=str)
            body += (f"<div class='card'><h3><span class='mono'>"
                     f"{_e(rec['capability'])}</span> · run <span class='mono'>"
                     f"{_e(rec['id'])}</span></h3><pre class='raw'>"
                     f"{_e(shown)}</pre>"
                     f"<form class='inline' method='post' "
                     f"action='/approvals/{rec['id']}/approve'>"
                     "<button>Approve and execute</button></form> "
                     f"<form class='inline' method='post' "
                     f"action='/approvals/{rec['id']}/drop' "
                     "onsubmit=\"return confirm('Drop this proposal? It "
                     "will not execute.')\">"
                     "<button class='danger'>Drop</button></form></div>")
    return _page("Approvals", body, active="approvals",
                 lede="The full proposal is what a person approves, never a "
                      "summary. Nothing here executes on a model's say-so.")


@app.post("/approvals/{rid}/approve")
def approve(rid: str):
    result = dispatch_core.approve_safely(rid, actor="console")
    if not result.get("approved"):
        result = {**result, "note": (result.get("note") or "") +
                  " | In this public deployment that refusal is expected: "
                  "approval executes only from the operator's own terminal "
                  "or chat door, never from a process an agent started. "
                  "The proposal above remains pending and unexecuted."}
        return _page("Approval result",
                     f"<div class='alert' role='alert'>{_e(result['note'])}</div>"
                     + _raw(result), active="approvals")
    return _page("Approval result", _result_block(result), active="approvals")


@app.post("/approvals/{rid}/drop")
def drop(rid: str):
    result = tracker.drop(rid, by="console")
    return _page("Dropped", _result_block(result), active="approvals")


@app.get("/runs", response_class=HTMLResponse)
def runs():
    rows = "".join(
        f"<tr><td class='mono'>{_e(r['id'])}</td><td>{_badge(r['status'])}</td>"
        f"<td class='mono'>{_e(r['capability'])}</td>"
        f"<td>{_e(r.get('verdict') or '')}</td></tr>"
        for r in tracker.recent(25))
    if not rows:
        rows = ("<tr><td colspan='4'>No runs yet. Draft something in the "
                "data room and it will appear here.</td></tr>")
    ledger = state() / "events" / "_ledger.jsonl"
    calls = ""
    if ledger.exists():
        cards = ""
        for line in ledger.read_text().splitlines()[-8:]:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            data = ev.get("data") or {}
            detail = ""
            if ev.get("kind") == "model_call":
                usage = data.get("usage") or {}
                detail = (f"engine {_e(data.get('engine', ''))} · model "
                          f"{_e(data.get('engine_model', ''))} · tokens in "
                          f"<span class='num'>{_e(usage.get('input_tokens', ''))}"
                          f"</span> out <span class='num'>"
                          f"{_e(usage.get('output_tokens', ''))}</span>")
            cards += (f"<div class='card'><p><span class='mono'>#{_e(ev.get('seq', ''))}"
                      f"</span> {_badge(ev.get('kind', ''))} "
                      f"<span class='mono'>{_e(ev.get('thread', ''))}</span></p>"
                      f"<p>{detail}</p></div>")
        calls = ("<div class='steps'>" + cards + "</div>") if cards else ""
    return _page("Runs & ledger",
                 f"<table><tr><th>run</th><th>status</th><th>capability</th>"
                 f"<th>verdict</th></tr>{rows}</table>"
                 "<h2>Latest model calls (model_call events)</h2>"
                 "<p>Each event carries the full prompt, the answer, the "
                 "engine, the model and the usage, so a judged call is "
                 "reconstructable from the ledger alone.</p>" + calls
                 + _cli_equiv("finrail runs"),
                 active="runs",
                 lede="What was proposed, what happened, and the evidence "
                      "trail behind every judged call.")


@app.get("/cli", response_class=HTMLResponse)
def cli_page():
    registry.load()
    rows = ""
    for cap in sorted(registry.commands().values(), key=lambda c: c.name):
        if not cap.name.startswith(("finance.", "tasks.")):
            continue
        verb = "get" if cap.kind == "read" else "do"
        rows += (f"<tr><td class='mono'>finrail {verb} {_e(cap.name)}</td>"
                 f"<td>{_badge(cap.kind)}</td><td>{_e(cap.description)}</td></tr>")
    body = (
        "<h2>The three verbs</h2>"
        + _cli("finrail get finance.compute.metric metric=margin")
        + _cli('finrail do finance.dataroom.propose "State the FY2025 revenue"')
        + _cli('finrail ask "what is the leverage on Meridian?"')
        + "<p><span class='mono'>get</span> runs a read immediately. "
          "<span class='mono'>do</span> proposes an action and stops for its "
          "declared approval. <span class='mono'>ask</span> resolves plain "
          "language into one exact command; it resolves, it does not act.</p>"
        "<h2>Lifecycle</h2>"
        + _cli("finrail capabilities")
        + _cli("finrail rules")
        + _cli("finrail runs")
        + _cli("finrail approve <run-id>")
        + _cli("finrail drop <run-id>")
        + _cli("finrail doctor")
        + "<h2>Finance and proof-pack commands (live from the registry)</h2>"
          f"<table><tr><th>command</th><th>kind</th><th>description</th></tr>"
          f"{rows}</table>"
        "<h2>Engines are environment, never a capability branch</h2>"
        + _cli("FINRAIL_ENGINE=nebius NEBIUS_API_KEY=… finrail capabilities")
        + _cli("FINRAIL_ENGINE=meta FINRAIL_ENGINE_BASE_URL=… "
               "FINRAIL_ENGINE_API_KEY=… FINRAIL_MODEL=… finrail capabilities")
        + "<p>There is no silent fallback: a down or rejecting engine fails "
          "the run under a canonical error code rather than quietly "
          "answering from another provider.</p>")
    return _page("Command line", body, active="cli",
                 lede="Finrail is a CLI first. Everything this console does "
                      "is available, with the same rules, from a terminal.")


def main():
    import uvicorn
    port = os.environ.get("FINRAIL_PORT") or os.environ.get("PORT") or "8000"
    uvicorn.run(app, host=os.environ.get("FINRAIL_HOST", "127.0.0.1"),
                port=int(port))


if __name__ == "__main__":
    main()
