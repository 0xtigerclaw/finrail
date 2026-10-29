#!/usr/bin/env python3
"""Archive and index an approved six-report source bundle (operator only).

Requires an authenticated gcloud operator and pre-created private bucket/corpus.
Never uploads the curated facts, calculations or expected benchmark answers.
The source bundle contains sources.json, raw/, and working/<id>.txt for PDFs.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from finrail.extensions.finance.public_reports import Tables


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('bundle', type=Path)
    ap.add_argument('--account', required=True)
    ap.add_argument('--project', required=True)
    ap.add_argument('--bucket', required=True)
    ap.add_argument('--corpus', required=True)
    ap.add_argument('--prefix', default='2026-10-29')
    ap.add_argument('--output', type=Path, required=True, help='Operator result file; check the async import before updating runtime IDs')
    args = ap.parse_args()
    manifest = json.loads((Path(__file__).resolve().parents[1] / 'finrail/extensions/finance/public_data/reports.json').read_text())
    sources = {s['source_id']: s for s in json.loads((args.bundle / 'sources.json').read_text())}
    access = subprocess.run(['gcloud', 'auth', 'print-access-token', '--account=' + args.account], capture_output=True, text=True, check=True).stdout.strip()

    def request(url, data=None, content_type='application/json'):
        req = Request(url, data=data, headers={'Authorization': 'Bearer ' + access,
                      'Content-Type': content_type, 'x-goog-user-project': args.project})
        with urlopen(req, timeout=90) as response:
            return json.load(response)

    def upload(obj, content, mime):
        base = 'https://storage.googleapis.com/storage/v1/b/' + args.bucket + '/o/' + quote(obj, safe='')
        try:
            existing = request(base)
            # Compare the original bytes, not only mutable object metadata.
            req = Request(base + '?alt=media', headers={'Authorization': 'Bearer ' + access})
            with urlopen(req, timeout=90) as response:
                if hashlib.sha256(response.read()).digest() != hashlib.sha256(content).digest():
                    raise ValueError('Archived object differs; use a new versioned prefix: ' + obj)
            return existing
        except HTTPError as exc:
            if exc.code != 404:
                raise
        url = 'https://storage.googleapis.com/upload/storage/v1/b/' + args.bucket + '/o?uploadType=media&ifGenerationMatch=0&name=' + quote(obj, safe='')
        return request(url, content, mime)

    uris, archived = [], []
    for entry in manifest['reports']:
        sid = entry['source_id']
        source = sources[sid]
        raw = (args.bundle / source['local_file']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Approved source fingerprint mismatch: ' + sid)
        pre = (f"REPORT {sid}; {source['company']}; {source['report_period']}; period ending {entry['period_end']}.\n"
               f"Source: {source['url']}\nOriginal SHA-256: {source['sha256']}\n"
               'Source-preserving extraction; original row/column order retained. Not a benchmark answer key.\n\n')
        if source['format'] == 'html':
            parser = Tables()
            parser.feed(raw.decode())
            text = ''
            for number, rows in enumerate(parser.tables[:7], 1):
                for start in range(0, len(rows), 12):
                    header = '\n'.join(' | '.join(r) for r in rows[:10]) if number in (3, 4, 5, 6) and start else ''
                    text += f'\nREPORT {sid}; HTML TABLE {number}; rows {start+1}-{min(start+12,len(rows))}\n' + header + '\n' + '\n'.join(' | '.join(r) for r in rows[start:start+12]) + '\n'
        else:
            text = (args.bundle / 'working' / (sid + '.txt')).read_text()
        objects = [(args.prefix + '/' + source['local_file'], raw, 'application/pdf' if source['format'] == 'pdf' else 'text/html'),
                   (args.prefix + '/parsed/' + sid + '.txt', (pre + text).encode(), 'text/plain')]
        for obj, content, mime in objects:
            metadata = upload(obj, content, mime)
            archived.append({'object': obj, 'generation': metadata['generation'], 'sha256': hashlib.sha256(content).hexdigest()})
            # RAG accepts PDF/text; original HTML is retained for cell checks.
            if mime != 'text/html':
                uris.append('gs://' + args.bucket + '/' + obj)
        print('Archived:', sid, flush=True)
    payload = {'importRagFilesConfig': {'gcsSource': {'uris': uris}, 'maxEmbeddingRequestsPerMin': 60,
               'ragFileTransformationConfig': {'ragFileChunkingConfig': {'fixedLengthChunking': {'chunkSize': 1024, 'chunkOverlap': 150}}}}}
    location = args.corpus.split('/locations/')[1].split('/')[0]
    operation = request('https://' + location + '-aiplatform.googleapis.com/v1beta1/' + args.corpus + '/ragFiles:import', json.dumps(payload).encode())
    args.output.write_text(json.dumps({'archived': archived, 'import': operation}, indent=2) + '\n')
    print('Import started. Poll the recorded operation, list imported files, and bind only matching source URIs to each report ID.')


if __name__ == '__main__':
    main()
