"""The proof page: one live run on a real filing, drawn as an auditor's workpaper.

Every number on it comes from a recorded `finance.reports.analyze` run. Nothing here
calculates, checks or awards trust; it shows what the kernel already did, which plugin
did each step, and the rule that held it.
"""
from __future__ import annotations

from finrail.core import registry, tracker
from finrail.extensions.finance.public_reports import ATTACKS, PROFILES
from .views import e, icon

ANALYZE = 'finance.reports.analyze'
MULTIPLE = 'finance.reports.multiple'


def lines(value: dict) -> list:
    """[(metric, source row label, 'Less' or '')] in the recorded definition's order."""
    prof = PROFILES.get(value.get('company'), {})
    spec = (prof.get('definitions') or {}).get(value.get('definition')) or {}
    return [(key, prof['rows'][key]['label'], 'Less' if sign == '-' else '') for sign, key in spec.get('terms', ())]

TICK = ("<svg class='tick' viewBox='0 0 22 16' aria-hidden='true'><path d='M2 9.5 7.5 14 20 2'/></svg>")


def _mark(letter: str, meaning: str) -> str:
    return f"<span class='tick-letter' title='{e(meaning)}' aria-label='{e(meaning)}'>{e(letter)}</span>"


def latest(run_id: str = '') -> dict | None:
    """The run to show: the one asked for, else the newest completed FCF proof."""
    if run_id:
        try:
            rec = tracker.load(run_id)
        except Exception:  # noqa: BLE001 - an unknown id shows the empty sheet
            return None
        return rec if rec.get('capability') == ANALYZE else None
    done = [r for r in tracker.recent(50) if r.get('capability') == ANALYZE and r.get('status') == 'done'
            and isinstance(r.get('result'), dict) and 'amount' in r['result']]
    done.sort(key=lambda r: (r['result'].get('definition') != 'issuer_fcf', r['result'].get('report_id') != 'NVDA-Q2-FY2027'))
    return done[0] if done else None


def _money(value, negative=False) -> str:
    text = f"{int(value):,}"
    return f"({text})" if negative else text


def hero() -> str:
    return ("<header class='proof-hero'><p class='kicker'>A finance harness, inspired by DeepSeek Harness</p>"
            "<h1>Every plugin declares its trust rules. The kernel enforces them.</h1>"
            "<p class='proof-lede'>DeepSeek Harness made everything a plugin. Finrail makes each plugin say, in code, "
            "whether it may act, who must approve, and how its result is checked. Below is one live run on a real filing.</p></header>")


def workpaper(rec: dict, model_call: dict | None) -> str:
    v = rec['result']
    by_metric = {i['metric']: i for i in v['inputs']}
    rows = ''
    for key, label, prefix in lines(v):
        item = by_metric.get(key)
        if not item:
            continue
        ref = f"T{item.get('table', '?')} · R{item.get('row_number', '?')} · C{item.get('html_cell', '?')}"
        rows += (f"<tr><td class='wp-label'>{f'<span class=wp-less>{prefix}</span>' if prefix else ''}{e(label)}</td>"
                 f"<td class='wp-ref'>{e(ref)}</td><td class='wp-num'>{_money(item['amount'], bool(prefix))}</td>"
                 f"<td class='wp-ticks'>{TICK}{_mark('q', 'Quote attached by code, not written by the model')}</td></tr>")
    issuer = bool(v.get('reconciled')) or v.get('definition') == 'issuer_fcf'
    total = 'Free cash flow' if 'fcf' in v.get('definition', '') else v['metric'].split(',')[0]
    rows += (f"<tr class='wp-total'><td class='wp-label'>{e(total)}, recomputed</td><td class='wp-ref'>Decimal, in code</td>"
             f"<td class='wp-num'><span class='double'>{_money(v['amount'])}</span></td>"
             f"<td class='wp-ticks'>{_mark('Σ', 'Recomputed in code with Decimal arithmetic')}"
             + (_mark('R', 'Reconciled to the issuer-reported figure') if issuer else '') + "</td></tr>")
    if v.get('reconciled'):
        r = v['reconciled']
        rows += (f"<tr class='wp-issuer'><td class='wp-label'>Issuer figure: {e(r['label'])}</td><td class='wp-ref'>Same filing, table {e(r.get('table', '?'))}</td>"
                 f"<td class='wp-num'>{_money(r['value'])}</td><td class='wp-ticks'>{_mark('R', 'Reconciled to the issuer-reported figure')}</td></tr>")
    elif issuer:
        rows += (f"<tr class='wp-issuer'><td class='wp-label'>Issuer-reported free cash flow</td><td class='wp-ref'>Same filing, table 6</td>"
                 f"<td class='wp-num'>{_money(v['reported_fcf'])}</td><td class='wp-ticks'>{_mark('R', 'Reconciled to the issuer-reported figure')}</td></tr>")
    proposer = 'model call not in this ledger'
    if model_call:
        data = model_call.get('data') or {}
        proposer = (data.get('engine_model') or data.get('model') or 'model not named').split('/')[-1] + ' on Nebius'
    verdict = (f"Reconciled. The recomputed total equals {e(v['company'])}’s own figure in the same filing." if issuer
               else f"Different definition. {e(v.get('reconciliation', ''))}, and the answer says so.")
    legend = ("<dl class='wp-legend'>"
              f"<div><dt>{TICK}</dt><dd>Agreed to the archived source cell (file SHA-256 checked)</dd></div>"
              f"<div><dt>{_mark('q', 'q')}</dt><dd>Row text attached by code; the model only picked the row</dd></div>"
              f"<div><dt>{_mark('Σ', 'Σ')}</dt><dd>Recomputed in code, never by the model</dd></div>"
              + (f"<div><dt>{_mark('R', 'R')}</dt><dd>Reconciled to the issuer’s figure in the same filing</dd></div>" if issuer else '')
              + "</dl>")
    return ("<section class='workpaper' aria-labelledby='wp-title'>"
            "<div class='wp-head'><div><p class='wp-ref-line'>W/P FCF-1 · Live run</p>"
            f"<h2 id='wp-title'>{e(v['company'])} {e(total.lower())}, {e(v['period'])}</h2>"
            f"<p class='wp-meta'>Period ended {e(v['period_end'])} · USD millions · {e(v['metric'])}</p></div>"
            f"<div class='wp-answer'><span class='wp-amount'>${_money(v['amount'])}m</span><span class='wp-verdict'>{e(verdict)}</span></div></div>"
            "<div class='wp-sign'>"
            f"<span><b>Proposed by</b> {e(proposer)}</span><span><b>Checked by</b> Finrail kernel (code)</span>"
            f"<span><b>Run</b> <a href='/runs/{e(rec['id'])}'><code>{e(rec['id'])}</code></a></span></div>"
            "<div class='table-wrap wp-table'><table><caption class='sr-only'>Free cash flow workpaper</caption>"
            "<thead><tr><th scope='col'>Line item</th><th scope='col'>Source cell</th><th scope='col' class='wp-num'>USD millions</th><th scope='col'><span class='sr-only'>Checks</span></th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div>{legend}"
            f"<p class='wp-foot'>Filing fingerprint <code>{e(v['source_sha256'][:16])}…</code> · "
            f"<a href='{e(v['source_url'])}' target='_blank' rel='noopener noreferrer'>Open {e(v['company'])}’s filing{icon('arrow', 14)}</a></p></section>")


def multiple_section(rec: dict, outcome: dict | None) -> str:
    """For a checked Anthropic revenue run: one human input, one division, labelled as such."""
    v = rec['result']
    if v.get('definition') != 'revenue':
        return ''
    result = ''
    if outcome:
        value = outcome.get('result') or {}
        if outcome.get('error'):
            result = f"<div class='alert' role='alert'>{e(outcome['error'])}</div>"
        elif value.get('price_to_sales'):
            result = (f"<div class='multiple-result' role='status'><p class='report-amount'>{e(value['price_to_sales'])}×"
                      f"<span>valuation / six-month revenue</span></p>"
                      f"<p><code>{e(value['valuation_usd_m'])} / {e(value['revenue_usd_m'])} = {e(value['price_to_sales'])}</code></p>"
                      f"<p>{e(value['label'])}</p><p class='company-meta'>{e(value['basis'])}</p></div>")
    return ("<section class='break-it multiple' id='multiple' aria-labelledby='multiple-title'><h2 id='multiple-title'>A multiple on the disclosed figure</h2>"
            f"<p>Price-to-sales is the one figure you supply divided by the revenue checked above (${int(v['amount']):,}m, six months). "
            "The harness does the division in Decimal and labels the result as arithmetic on your figure. It never looks up a price, "
            "annualises, or forecasts. The S-1 leaves the post-offering share count blank, so a share price cannot be turned into a valuation from the filing.</p>"
            f"<form method='post' action='/proof/multiple#multiple' class='break-actions'><input type='hidden' name='run_id' value='{e(rec['id'])}'>"
            "<label class='form-label' for='valuation'>Valuation, USD millions</label>"
            "<input id='valuation' name='valuation_usd_m' inputmode='decimal' pattern='[0-9]+(\\.[0-9]+)?' required placeholder='300000'>"
            "<button class='btn btn-secondary'>Divide</button></form>" + result + "</section>")


def rail(rec: dict, model_call: dict | None) -> str:
    """The steps `finance.reports.analyze` declares, with what this run showed at each."""
    v = rec['result']
    usage = ((model_call or {}).get('data') or {}).get('usage') or {}
    tokens = f" · {usage.get('input_tokens', 0):,} tokens in, {usage.get('output_tokens', 0):,} out" if usage else ''
    seen = {'Retrieve': f"{len(v['retrieval']['evidence'])} excerpts from {v['report_id']}",
            'Propose': f"Picked {len(v['inputs'])} row IDs and their values{tokens}",
            'Bind': f"Attached the exact retrieved text of {len(v['inputs'])} rows",
            'Check': f"{len(v['checks'])} checks passed",
            'Compute': f"${int(v['amount']):,}m in Decimal" + (", equal to the issuer’s figure" if v.get('reconciled') or v.get('definition') == 'issuer_fcf' else '')}
    registry.load()
    kinds = {'retrieve': 'plugin', 'model': 'model'}
    items = ''.join(
        f"<li class='rail-step rail-{kinds.get(st['kind'], 'code')}'><span class='rail-n'>{i}</span>"
        f"<div class='rail-who'><strong>{e(st['name'])}</strong><span class='rail-actor'>{e(st['by'])}</span></div>"
        f"<div class='rail-did'>{e(seen.get(st['name'], st['rule']))}</div>"
        f"<div class='rail-rule'>{e(st['rule'])}<code>{e(st['where'])}</code></div></li>"
        for i, st in enumerate(registry.command(ANALYZE).steps, 1))
    return ("<section class='rail-section' aria-labelledby='rail-title'><h2 id='rail-title'>Who did what, under which rule</h2>"
            f"<p class='rail-source'>Drawn from the steps <code>{ANALYZE}</code> declares in code.</p>"
            "<ol class='harness-rail'>" + items + "</ol></section>")


def declaration() -> str:
    registry.load()
    rules = registry.command(ANALYZE).rules()
    lines = ''.join(f"<div><dt>{e(k)}</dt><dd>{e(rules.get(k))}</dd></div>"
                    for k in ('read_only', 'autonomous', 'confirm', 'background'))
    return ("<aside class='declaration' aria-labelledby='decl-title'><h2 id='decl-title'>Declared once, in code</h2>"
            "<p>Read live from the registry. The browser and the CLI obey the same declaration.</p>"
            f"<p class='decl-name'><code>{ANALYZE}</code></p><dl class='decl-list'>{lines}</dl>"
            f"<p class='decl-verified'><b>verified_by</b> {e(rules.get('verified_by') or 'Not declared')}</p>"
            "<a class='inline-action' href='/rules'>Every capability’s rules" + icon('arrow', 14) + "</a></aside>")


def challenge(rec: dict, outcome: dict | None) -> str:
    buttons = ''.join(f"<button class='btn btn-secondary' name='attack' value='{e(k)}'>{e(label)}</button>"
                      for k, label in ATTACKS.items())
    result = ''
    if outcome:
        value = outcome.get('result') or {}
        if outcome.get('error'):
            result = f"<div class='alert' role='alert'>{e(outcome['error'])}</div>"
        elif value.get('refused'):
            result = (f"<div class='refusal' role='status'><p class='refusal-head'>{icon('shield', 18)}Refused by "
                      f"<code>{e(value.get('refused_by'))}</code></p><p class='refusal-why'>{e(value.get('reason'))}</p>"
                      f"<p class='refusal-note'>Tamper: {e(value.get('label'))}. The untampered replay still gives "
                      f"${int(value.get('recorded_amount', 0)):,}m, so the recorded answer is unchanged.</p></div>")
        else:
            result = ("<div class='alert' role='alert'>The tampered input was accepted. That is a checker failure; "
                      "report it.</div>")
    return ("<section class='break-it' id='break-it' aria-labelledby='break-title'><h2 id='break-title'>Try to break it</h2>"
            "<p>Change one thing the model proposed, the way a wrong model would. The same checker re-runs on the recorded "
            "evidence, offline. No model call, nothing spent.</p>"
            f"<form method='post' action='/proof/challenge#break-it' class='break-actions'><input type='hidden' name='run_id' value='{e(rec['id'])}'>{buttons}</form>"
            + result + "</section>")


def empty(state: str = '', rid: str = '', error: str = '') -> str:
    if state == 'running':
        return (f"<section class='workpaper wp-pending' data-run-refresh data-refresh-url='/proof?run={e(rid)}'>"
                f"<p class='wp-ref-line'>W/P FCF-1 · Running</p><h2>Running live on NVIDIA’s filing</h2>"
                "<p role='status'>Retrieving, proposing, checking. This page refreshes on its own and never re-submits.</p></section>")
    note = f"<div class='alert' role='alert'>{e(error)}</div>" if error else ''
    return ("<section class='workpaper wp-pending'><p class='wp-ref-line'>W/P FCF-1 · Not yet run on this instance</p>"
            "<h2>NVIDIA free cash flow, Q2 FY2027</h2>"
            "<p>One model call on Nemotron proposes the inputs. Every other step is code. About a minute.</p>" + note +
            "<form method='post' action='/proof/run' class='break-actions'><button class='btn'>Run the proof live" + icon('arrow', 16) + "</button>"
            "<button class='btn btn-secondary' name='report_id' value='ANTH-S1-2026'>Or check Anthropic’s S-1 revenue</button></form></section>")


def other_rails() -> str:
    return ("<section class='other-rails'><h2>The same kernel, elsewhere</h2><ul>"
            "<li><a href='/?q=Record%20NVIDIA%27s%20Q2%20free%20cash%20flow%20as%20%2424%2C077m'>Record a wrong NVIDIA figure and watch the claim check refuse it</a></li>"
            "<li><a href='/?q=What%20was%20Anthropic%27s%20revenue%20in%20the%20first%20half%20of%202026%3F'>Check Anthropic’s S-1 revenue against the archived statement cell</a></li>"
            "<li><a href='/reports'>Seven filings, every indexed file reconciled live against the corpus</a></li>"
            "<li><a href='/screen'>One publisher can report; only two can verify (synthetic fixture)</a></li>"
            "</ul></section>")
