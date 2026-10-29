"""Presentation of real reports. All report actions use the shared dispatcher."""
from .views import e, badge, icon, panel, table, raw
from finrail.extensions.finance.public_reports import catalog, DEFINITIONS


def evidence_html(found):
    blocks = []
    for item in found.get('evidence', []):
        blocks.append(f"<details class='disclosure report-evidence'><summary>{e(item['id'])} · {e(item.get('source_name') or 'Report excerpt')}</summary>"
                      f"<p class='company-meta'>{e(item['source_uri'])}</p><pre class='source-excerpt'>{e(item['text'])}</pre></details>")
    return ''.join(blocks)


def result_html(value):
    if 'amount' not in value:
        return (f"<p><strong>{e(value['backend'])}</strong> · {e(value['report_id'])}</p>"
                "<p>Retrieved excerpts are evidence to inspect; this search has not verified a numerical answer.</p>"
                + evidence_html(value))
    rows = ''.join(f"<tr><td>{e(v['metric'].replace('_',' ').capitalize())}<span class='company-meta'>HTML table {v['table']}, row {v['row_number']} · current quarter</span></td>"
                   f"<td class='num'>{e(format(int(v['amount']), ','))}</td><td>{e(v['evidence_id'])}</td></tr>" for v in value['inputs'])
    body = (f"<div class='report-answer'><div><h3>{e(value['metric'])}</h3><p>{e(value['company'])} · {e(value['period'])} · quarter ended {e(value['period_end'])}</p></div>"
            f"<p class='report-amount'>${int(value['amount']):,}<span>USD millions</span></p></div>"
            f"<p>{badge('checked','Source cells checked')} {e(value['reconciliation'])}</p>"
            + table(['Input', 'USD millions', 'Citation'], rows, 'Verified FCF inputs')
            + f"<div class='report-formula'><span>Formula</span><code>{e(value['formula'])} = {e(value['amount'])}</code></div>"
            + "<p class='company-meta'>Spend inputs are positive amounts; the source presents cash outflows in parentheses. Checks validate this calculation against the published report, not the issuer’s accounting or an investment decision.</p>"
            + "<ul class='report-checks'>" + ''.join(f"<li>{icon('check',16)}{e(c)}</li>" for c in value['checks']) + "</ul>"
            + f"<p><a class='inline-action' href='{e(value['source_url'])}' target='_blank' rel='noopener noreferrer'>Open issuer report {icon('arrow',16)}</a></p>"
            + f"<details class='disclosure'><summary>Archived cells and report fingerprint</summary>{raw(value['inputs'], 'Exact row evidence')}<p class='mono'>{e(value['source_sha256'])}</p></details>"
            + f"<h3>Retrieved evidence</h3><p class='company-meta'>{e(value['retrieval']['backend'])}. Only files for {e(value['report_id'])} were searched.</p>"
            + evidence_html(value['retrieval']))
    return body


def workspace(configured):
    reports = catalog()
    options = ''.join(f"<option value='{e(r['source_id'])}'>{e(r['company'])} · {e(r['report_period'])}</option>" for r in reports)
    nv_options = ''.join(f"<option value='{e(r['source_id'])}'>{e(r['report_period'])} · ended {e(r['period_end'])}</option>" for r in reports if r['company'] == 'NVIDIA')
    definitions = ''.join(f"<option value='{key}'>{e(label)}</option>" for key, label in DEFINITIONS.items())
    disabled = '' if configured else ' disabled'
    analysis = ("<p>Reconstruct NVIDIA’s free cash flow from its published cash-flow statement. The model extracts inputs; source-cell checks and decimal arithmetic produce the answer.</p>"
                "<form method='post' action='/reports/analyze'>"
                f"<div class='form-field'><label for='analysis-report'>NVIDIA report</label><select id='analysis-report' name='report_id'>{nv_options}</select></div>"
                f"<div class='form-field'><label for='fcf-definition'>Accounting definition</label><select id='fcf-definition' name='definition'>{definitions}</select>"
                "<p class='form-hint'>NVIDIA’s definition also subtracts principal payments on property, equipment and intangible assets. Simple FCF excludes those payments.</p></div>"
                f"<div class='form-actions'><button class='btn' type='submit'{disabled}>Calculate and check FCF {icon('arrow',16)}</button></div></form>")
    search = ("<p>Search a single company and quarter. Each result opens the retrieved source text; search alone does not verify a figure.</p>"
              "<form method='post' action='/reports/search'><div class='form-field'>"
              f"<label for='search-report'>Published report</label><select id='search-report' name='report_id'>{options}</select></div>"
              "<div class='form-field'><label for='report-query'>What evidence do you need?</label>"
              "<input id='report-query' name='query' required minlength='3' maxlength='500' value='Operating cash flow, capital expenditure and liquidity' aria-describedby='query-help'>"
              "<p class='form-hint' id='query-help'>Try revenue growth, cash balances, or operating cash flow.</p></div>"
              f"<div class='form-actions'><button class='btn btn-secondary' type='submit'{disabled}>{icon('search',16)} Search report</button></div></form>")
    rows = ''.join(f"<tr><td><strong>{e(r['company'])}</strong><span class='company-meta'>{e(r['source_id'])}</span></td><td>{e(r['report_period'])}<span class='company-meta'>Ended {e(r['period_end'])}</span></td>"
                   f"<td>{'USD' if r['company'] != 'ASML' else 'EUR'}<span class='company-meta'>Unaudited disclosure</span></td>"
                   f"<td><a class='inline-action' href='{e(r['url'])}' target='_blank' rel='noopener noreferrer'>Issuer source{icon('arrow',16)}</a></td></tr>" for r in reports)
    status = ("<div class='notice'>Managed retrieval is not configured on this instance. The source catalog is available; contact the operator to enable report actions.</div>" if not configured else '')
    return (status + "<div class='content-grid'><div class='primary-column'>"
            + panel('Check a real financial calculation', analysis) + panel('Archived report library', table(['Company', 'Quarter', 'Reporting', ''], rows, 'Six published company reports'), badge('info','6 public reports'))
            + "</div><div class='secondary-column'>" + panel('Find source evidence', search)
            + panel('How the proof works', "<ol class='guide-list'><li><strong>Retrieve</strong><p>Google managed RAG searches only the selected report’s files.</p></li><li><strong>Extract</strong><p>Nebius proposes typed inputs with verbatim source quotes.</p></li><li><strong>Check and calculate</strong><p>Code checks the archived report’s fingerprint, quarter, units and exact cells before arithmetic.</p></li></ol><p class='field-help'>Numerical verification currently covers NVIDIA FCF for two quarters. Nebius and ASML support evidence retrieval. Expected benchmark answers are never indexed.</p>")
            + "</div></div>")
