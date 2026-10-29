"""Offline adversarial contracts. No provider credentials or expected-answer RAG."""
import copy
import hashlib
import json
from urllib.error import HTTPError

import pytest

from finrail.core.errors import ContractViolation, NotConfigured, OpFailed
from finrail.extensions.finance import public_reports as r


def fixture():
    item = copy.deepcopy(r.report('NVDA-Q1-FY2027'))
    header = '<tr><td>($ in millions)</td></tr><tr><td>Three Months Ended</td></tr><tr><td>April 26,</td><td>January 25,</td></tr><tr><td>2026</td><td>2026</td></tr>'
    rows = [('operating_cash_flow', '50,344', '27,414'), ('cash_purchases', '(1,757', '(1,227'), ('principal_payments', '(33', '(52')]
    table5 = '<table>' + header + ''.join('<tr><td>' + r.ROWS[key] + '</td><td></td><td>' + value + '</td><td>)</td><td>' + comparative + '</td></tr>' for key, value, comparative in rows) + '</table>'
    table6 = '<table>' + header + '<tr><td>Free cash flow</td><td>$</td><td>48,554</td><td>34,902</td></tr></table>'
    raw = ('<table></table>' * 4 + table5 + table6).encode()
    item['sha256'] = hashlib.sha256(raw).hexdigest()
    cells = r.source_cells(item, raw)
    evidence = [{'id': 'E1', 'text': '\n'.join(cells[key]['source_row'] for key in r.ROWS), 'source_uri': item['indexed_uris'][0]}]
    found = {'report_id': item['source_id'], 'evidence': evidence, 'backend': 'test stub'}
    candidate = {'report_id': item['source_id'], 'period_end': item['period_end'], 'currency': 'USD', 'unit': 'million', 'definition': 'issuer_fcf',
                 'inputs': [{'metric': key, 'value': cells[key]['value'], 'row_id': f'E1:L{number}'} for number, key in enumerate(r.ROWS, 1)]}
    return item, raw, cells, found, candidate


def compute(candidate_change=None):
    item, _, cells, found, proposed = fixture()
    if candidate_change:
        candidate_change(proposed)
    return r.check_and_compute(item, proposed['definition'], proposed, found, cells)


def test_real_accounting_definition_and_simple_fcf_differ():
    assert compute()['amount'] == '48554'
    simple = compute(lambda p: (p.update(definition='simple_fcf'), p['inputs'].pop()))
    assert simple['amount'] == '48587'
    assert 'Different definition' in simple['reconciliation']


@pytest.mark.parametrize('field,value', [('report_id', 'NVDA-Q2-FY2027'), ('period_end', '2026-07-26'), ('currency', 'EUR'), ('unit', 'billion')])
def test_wrong_financial_scope_is_refused(field, value):
    with pytest.raises(ContractViolation):
        compute(lambda p: p.update({field: value}))


@pytest.mark.parametrize('value', ['27414', '50344.01', 'NaN', 'Infinity', '-50344', '50,344', '', 50344, None])
def test_wrong_source_column_or_invalid_amount_is_refused(value):
    with pytest.raises(ContractViolation):
        compute(lambda p: p['inputs'][0].update(value=value))


@pytest.mark.parametrize('change', [
    lambda p: p['inputs'][0].update(row_id='invented'),
    lambda p: p['inputs'][0].update(quote='Net cash provided by operating activities was 50,344'),
    lambda p: p['inputs'][0].update(row_id='E1:L99'),
    lambda p: p['inputs'][0].update(metric='cash_purchases'),
    lambda p: p['inputs'].pop(),
    lambda p: p['inputs'].append(p['inputs'][0]),
])
def test_missing_duplicate_or_fabricated_evidence_fails(change):
    with pytest.raises(ContractViolation):
        compute(change)


def test_hash_header_period_and_reconciliation_checks():
    item, raw, cells, found, p = fixture()
    with pytest.raises(ContractViolation, match='SHA-256'):
        r.source_cells(item, raw + b'edited')
    wrong = raw.replace(b'April 26,', b'July 26,')
    item['sha256'] = hashlib.sha256(wrong).hexdigest()
    with pytest.raises(ContractViolation, match='date column'):
        r.source_cells(item, wrong)
    cells['reported_fcf']['value'] = '48555'
    with pytest.raises(ContractViolation, match='reconcile'):
        r.check_and_compute(item, 'issuer_fcf', p, found, cells)


def test_zero_is_a_valid_input_not_missing():
    item, _, cells, found, p = fixture()
    p['inputs'][2].update(value='0', row_id='E1:L4')
    found['evidence'][0]['text'] += '\n' + r.ROWS['principal_payments'] + ' | 0'
    cells['principal_payments']['value'] = '0'
    cells['reported_fcf']['value'] = '48587'
    assert r.check_and_compute(item, 'issuer_fcf', p, found, cells)['amount'] == '48587'


def test_retrieval_scopes_the_actual_google_request(monkeypatch):
    item = r.report('NVDA-Q1-FY2027').copy()
    item['rag_file_ids'] = ['123']
    monkeypatch.setenv('FINRAIL_RAG_CORPUS', 'projects/157951168487/locations/us-central1/ragCorpora/99')
    def fake(url, payload):
        assert url.endswith('/locations/us-central1:retrieveContexts')
        assert payload['vertexRagStore']['ragResources'][0]['ragFileIds'] == ['123']
        return {'contexts': {'contexts': [{'text': 'source text', 'sourceUri': item['indexed_uris'][0]}]}}
    monkeypatch.setattr(r, 'google', fake)
    assert r.retrieve(item, 'cash flow')['evidence'][0]['id'] == 'E1'


def test_retrieval_rejects_cross_report_source_and_empty_results(monkeypatch):
    item = r.report('NVDA-Q1-FY2027').copy()
    item['rag_file_ids'] = ['123']
    monkeypatch.setenv('FINRAIL_RAG_CORPUS', 'projects/157951168487/locations/us-central1/ragCorpora/99')
    monkeypatch.setattr(r, 'google', lambda *_: {'contexts': {'contexts': [{'text': 'wrong report', 'sourceUri': 'https://attacker.example'}]}})
    with pytest.raises(ContractViolation, match='outside'):
        r.retrieve(item, 'cash flow')
    monkeypatch.setattr(r, 'google', lambda *_: {})
    with pytest.raises(OpFailed, match='No evidence'):
        r.retrieve(item, 'cash flow')


def test_provider_failure_never_becomes_fixture_success(monkeypatch):
    monkeypatch.setattr(r, 'token', lambda: 'fake-not-a-credential')
    def denied(*args, **kwargs):
        raise HTTPError('https://googleapis.com', 403, 'forbidden', {}, None)
    monkeypatch.setattr(r, 'urlopen', denied)
    with pytest.raises(OpFailed, match='HTTP 403'):
        r.google('https://googleapis.com')


def test_unsupported_metrics_and_unconfigured_corpus_fail(monkeypatch):
    with pytest.raises(ContractViolation):
        r.ReportAnalyze().plan('ASML-Q2-2026')
    with pytest.raises(ContractViolation):
        r.ReportAnalyze().plan('NVDA-Q1-FY2027', 'revenue')
    with pytest.raises(ContractViolation):
        r.ReportAnalyze().plan('ANTH-S1-2026', 'issuer_fcf')
    assert r.ReportAnalyze().plan('ANTH-S1-2026') == {'report_id': 'ANTH-S1-2026', 'definition': 'revenue'}
    monkeypatch.delenv('FINRAIL_RAG_CORPUS', raising=False)
    with pytest.raises(NotConfigured):
        r.retrieve(r.report('NVDA-Q1-FY2027'), 'cash flow')


def test_live_pipeline_uses_model_then_source_gate(monkeypatch):
    item, raw, _, found, p = fixture()
    from finrail.core import llm
    monkeypatch.setattr(r, 'report', lambda _: item)
    monkeypatch.setattr(r, 'retrieve', lambda *args, **kwargs: found)
    monkeypatch.setattr(r, 'archived', lambda _: raw)
    def extract(**kwargs):
        assert 'retrieved_rows' in kwargs['context']
        assert '48554' not in kwargs['context']['retrieved_rows']  # FCF answer absent from this context.
        return p
    monkeypatch.setattr(llm, 'call', extract)
    assert r.ReportAnalyze().execute({'report_id': item['source_id'], 'definition': 'issuer_fcf'})['amount'] == '48554'
    p['inputs'][0]['value'] = '27414'
    with pytest.raises(ContractViolation, match='Source-cell'):
        r.ReportAnalyze().execute({'report_id': item['source_id'], 'definition': 'issuer_fcf'})


def test_console_catalog_disabled_when_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setenv('FINRAIL_STATE', str(tmp_path))
    monkeypatch.delenv('FINRAIL_RAG_CORPUS', raising=False)
    from fastapi.testclient import TestClient
    from console.app import app
    with TestClient(app) as client:
        page = client.get('/reports')
        assert page.status_code == 200
        assert 'Live corpus check unavailable' in page.text and 'not confirmed' in page.text
        assert page.text.count("class='filing'") == 7 and 'NVDA-Q2-FY2027.txt' in page.text
        assert 'ANTH-S1-2026' in page.text and 'not yet ingested' in page.text
        assert 'Run a check directly' not in page.text and "type='submit'" not in page.text
        assert 'All companies and figures are synthetic' not in page.text
        invalid = client.post('/reports/analyze', data={'report_id': 'ASML-Q2-2026'})
        assert invalid.status_code == 400 and 'NVIDIA' in invalid.text
        for action in ('/reports/analyze', '/reports/search'):
            missing = client.post(action, data={})
            assert missing.status_code == 400
            assert 'text/html' in missing.headers['content-type']
            assert 'Return to reports' in missing.text
