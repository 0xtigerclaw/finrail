"""Presentation of real reports. All report actions use the shared dispatcher."""
from .views import e, badge, icon, panel, table, raw
from urllib.parse import quote

from finrail.extensions.finance.public_reports import PROFILES, catalog, checked_fcf, checked_run


def evidence_html(found, used_quotes=None):
    used_quotes = used_quotes or {}
    blocks = []
    for item in found.get('evidence', []):
        quotes = used_quotes.get(item['id']) or []
        if not quotes:
            blocks.append(f"<details class='disclosure report-evidence'><summary>{e(item['id'])} · {e(item.get('source_name') or 'Report excerpt')}</summary>"
                          f"<p class='company-meta'>{e(item['source_uri'])}</p><pre class='source-excerpt'>{e(item['text'])}</pre></details>")
            continue
        text = e(item['text'])
        for quote in quotes:
            marked = e(quote)
            if marked and marked in text:
                text = text.replace(marked, f"<mark class='cell-mark'>{marked}</mark>", 1)
        blocks.append(f"<article class='filing-excerpt'><div class='filing-head'><span>{e(item['id'])} · {e(item.get('source_name') or 'Report excerpt')}</span>"
                      f"{badge('checked', 'Input source')}</div><pre>{text}</pre>"
                      f"<p class='filing-uri'>{e(item['source_uri'])}</p></article>")
    return ''.join(blocks)


def _rail_step(label, detail):
    return (f"<li class='proof-step'><span class='proof-node'>{icon('check', 13)}</span>"
            f"<span class='proof-copy'><strong>{e(label)}</strong><span>{e(detail)}</span></span></li>")


def expression(value) -> str:
    """The recorded formula with each metric replaced by its checked amount: `24,077 − 2,677 − 59`."""
    amounts = {v['metric']: int(v['amount']) for v in value['inputs']}
    return ' '.join(f"{amounts[t]:,}" if t in amounts else '−' if t == '-' else t for t in value['formula'].split())


def reconciled(value) -> bool:
    """Whether the run tied to the issuer's own figure (older NVIDIA records carry only reported_fcf)."""
    return bool(value.get('reconciled')) or (value.get('definition') == 'issuer_fcf' and value.get('reported_fcf') is not None)


def reconciled_line(value) -> str:
    rec = value.get('reconciled')
    if rec:
        return f"Matches {rec['label']} ({int(rec['value']):,}) in the same filing, table {rec.get('table', '?')}"
    if reconciled(value):
        return f"Matches the issuer-reported ${int(value['reported_fcf']):,}m in the same filing"
    return value.get('reconciliation', '')


def proof_rail(value):
    if reconciled(value):
        verdict, verdict_detail = 'Reconciled', reconciled_line(value)
    else:
        verdict, verdict_detail = 'Definition labelled', value.get('reconciliation', '') + ', and the answer says so'
    return ("<ol class='proof-rail' aria-label='How this answer was proven'>"
            + _rail_step('Retrieved', f"{value['retrieval']['backend']} · only {value['report_id']} files searched")
            + _rail_step('Rows bound', f"{len(value['inputs'])} typed inputs · the model selected row IDs; code attached the exact source text")
            + _rail_step('Cells checked', f"{len(value['checks'])} checks · fingerprint, period column, currency, units, archived cells")
            + _rail_step('Calculated', f"{expression(value)} · Decimal arithmetic in code, never by the model")
            + _rail_step(verdict, verdict_detail)
            + "</ol>")


def multiple_html(value) -> str:
    """A price-to-sales result: the person's figure, the checked revenue, the division."""
    return (f"<div class='report-answer'><div><h3>Price-to-sales at a valuation you supplied</h3>"
            f"<p>{e(value['company'])} · {e(value['period'])} · revenue run <a href='/proof?run={e(value['revenue_run'])}'><code>{e(value['revenue_run'])}</code></a></p></div>"
            f"<p class='report-amount'>{e(value['price_to_sales'])}×<span>valuation / six-month revenue</span></p></div>"
            f"<div class='report-formula'><span>Formula</span><code>{e(value['valuation_usd_m'])} / {e(value['revenue_usd_m'])} = {e(value['price_to_sales'])}</code></div>"
            f"<p>{badge('checked', 'Arithmetic only')} {e(value['label'])}</p>"
            f"<p class='company-meta'>{e(value['basis'])} {e(value['share_count'])}</p>"
            "<ul class='report-checks'>" + ''.join(f"<li>{icon('check',16)}{e(c)}</li>" for c in value['checks']) + "</ul>")


def _used_quotes(value):
    used = {}
    for item in value['inputs']:
        if item.get('quote'):
            used.setdefault(item['evidence_id'], []).append(item['quote'])
    return used


def result_html(value):
    if 'price_to_sales' in value:
        return multiple_html(value)
    if 'amount' not in value:
        return (f"<p><strong>{e(value['backend'])}</strong> · {e(value['report_id'])}</p>"
                "<p>Retrieved excerpts are evidence to inspect; this search has not verified a numerical answer.</p>"
                + evidence_html(value))
    rows = ''.join(f"<tr><td>{e(v['metric'].replace('_',' ').capitalize())}<span class='company-meta'>HTML table {v['table']}, row {v['row_number']} · {e(v.get('column', 'current period'))}</span></td>"
                   f"<td class='num'>{e(format(int(v['amount']), ','))}</td><td>{e(v['evidence_id'])}</td></tr>" for v in value['inputs'])
    body = (f"<div class='report-answer'><div><h3>{e(value['metric'])}</h3><p>{e(value['company'])} · {e(value['period'])} · period ended {e(value['period_end'])}</p></div>"
            f"<p class='report-amount'>${int(value['amount']):,}<span>USD millions</span></p></div>"
            + proof_rail(value)
            + f"<p>{badge('checked','Source cells checked')} {e(reconciled_line(value))}</p>"
            + table(['Input', 'USD millions', 'Citation'], rows, 'Verified inputs')
            + f"<div class='report-formula'><span>Formula</span><code>{e(value['formula'])} = {e(value['amount'])}</code></div>"
            + "<p class='company-meta'>Spend and loss inputs are positive amounts; the source presents them in parentheses. Checks validate this calculation against the published report, not the issuer’s accounting or an investment decision.</p>"
            + "<ul class='report-checks'>" + ''.join(f"<li>{icon('check',16)}{e(c)}</li>" for c in value['checks']) + "</ul>"
            + f"<p><a class='inline-action' href='{e(value['source_url'])}' target='_blank' rel='noopener noreferrer'>Open issuer report {icon('arrow',16)}</a></p>"
            + f"<details class='disclosure'><summary>Archived cells and report fingerprint</summary>{raw(value['inputs'], 'Exact row evidence')}<p class='mono'>{e(value['source_sha256'])}</p></details>"
            + f"<h3>Retrieved evidence</h3><p class='company-meta'>{e(value['retrieval']['backend'])}. Only files for {e(value['report_id'])} were searched. Excerpts that supplied an input are shown as filing excerpts with the used row marked.</p>"
            + evidence_html(value['retrieval'], _used_quotes(value)))
    return body


COMPANIES = ('NVIDIA', 'Nebius', 'ASML', 'Anthropic')
ASKS = {'NVIDIA': ("What was NVIDIA's free cash flow in {q}?",), 'Nebius': ("What did Nebius say about capex in {q}?",),
        'ASML': ("What did ASML say about guidance in {q}?",),
        'Anthropic': ("What was Anthropic's revenue in the first half of 2026?",
                      "What does Anthropic's S-1 say about compute commitments?")}
#: The figure shown as checked on a filing card, per company: (definition, label).
CARD_CHECK = {'NVIDIA': ('issuer_fcf', 'Free cash flow'), 'Anthropic': ('revenue', 'Revenue')}


def _corpus_band(corpus: dict) -> str:
    if corpus.get('error'):
        return (f"<div class='notice'>Live corpus check unavailable: {e(corpus['error'])} The catalog below is what was "
                "ingested; its files could not be confirmed live just now.</div>")
    v = corpus.get('result') or {}
    good = v.get('reconciled')
    active = sum(1 for f in v.get('files', []) if f['state'] == 'ACTIVE')
    verdict = (f"{active} of {v.get('catalogued')} catalogued files are indexed and ACTIVE. Nothing else is in the corpus."
               if good else f"Mismatch: missing {len(v.get('missing', []))}, uncatalogued {len(v.get('extra', []))}.")
    return ("<section class='corpus-band'><div><p class='wp-ref-line'>Google Vertex AI RAG Engine · checked live just now</p>"
            f"<h2>{e(v.get('corpus'))}</h2><p class='wp-meta'>{e(v.get('description'))}</p></div>"
            f"<dl class='corpus-facts'><div><dt>Embedding</dt><dd>{e(v.get('embedding'))}</dd></div>"
            f"<div><dt>Corpus</dt><dd>{e(v.get('state'))}</dd></div><div><dt>Files</dt><dd>{len(v.get('files', []))}</dd></div></dl>"
            f"<p class='corpus-verdict {'ok' if good else 'bad'}'>{icon('check' if good else 'alert', 16)}{e(verdict)}</p></section>")


def _filing(r: dict, live: dict) -> str:
    files = ''
    for fid, uri in zip(r.get('rag_file_ids') or [], r.get('indexed_uris') or []):
        f = live.get(fid)
        state = f['state'] if f else 'not confirmed'
        files += (f"<li><span class='file-state {'ok' if state == 'ACTIVE' else ''}'>{e(state)}</span>"
                  f"<code>{e(uri.rsplit('/', 1)[-1])}</code><span class='company-meta'>RAG file {e(fid)}</span></li>")
    checked = ''
    if r['company'] in CARD_CHECK:
        definition, label = CARD_CHECK[r['company']]
        rec = checked_run(r['source_id'], definition)
        checked = (f"<p class='filing-checked'>{icon('check', 15)}{e(label)} checked: <b>${int(rec['result']['amount']):,}m</b> "
                   f"<a href='/proof?run={e(rec['id'])}'>workpaper</a></p>" if rec else
                   f"<p class='filing-checked muted'>{e(label)} not yet checked on this instance.</p>")
    period = 'Period ended' if PROFILES.get(r['company'], {}).get('period', 'quarter') != 'quarter' else 'Quarter ended'
    chips = ''.join(f"<a class='chip' href='/?q={quote(q.format(q=r['report_period']), safe='')}'>Ask: {e(q.format(q=r['report_period']))}</a>"
                    for q in ASKS[r['company']])
    source = 'EDGAR source' if 'sec.gov' in r['url'] else 'Issuer source'
    return (f"<article class='filing'><div class='filing-top'><div><h3>{e(r['report_period'])}</h3>"
            f"<p class='company-meta'>{period} {e(r['period_end'])} · published {e(r['published_at'])} · {e(r['audit_status'])}</p></div>"
            f"<a class='inline-action' href='{e(r['url'])}' target='_blank' rel='noopener noreferrer'>{source}{icon('arrow', 14)}</a></div>"
            f"{checked}<dl class='filing-facts'><div><dt>Archived original</dt><dd><code>gs://{e(r['bucket'])}/{e(r['object'])}</code></dd></div>"
            f"<div><dt>SHA-256</dt><dd><code>{e(r['sha256'][:16])}…</code></dd></div></dl>"
            f"<p class='filing-label'>Indexed in the corpus</p><ul class='filing-files'>{files or '<li><span class=file-state>not yet ingested</span><span class=company-meta>Run the operator ingestion in docs/public-reports.md</span></li>'}</ul>"
            f"{chips}</article>")


def filings(configured: bool, corpus: dict) -> str:
    live = {f['file_id']: f for f in ((corpus or {}).get('result') or {}).get('files', [])}
    reports = catalog()
    body = _corpus_band(corpus or {})
    for company in COMPANIES:
        cards = ''.join(_filing(r, live) for r in reports if r['company'] == company)
        body += f"<section class='company-filings'><h2>{e(company)}</h2><div class='filing-grid'>{cards}</div></section>"
    return body
