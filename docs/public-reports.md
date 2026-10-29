# Managed public-report demo

This extends the synthetic investment workspace with six real issuer disclosures:
Nebius Q1/Q2 2026, NVIDIA Q1/Q2 FY2027, and ASML Q2/Q3 2026. These are the latest
two published quarters per issuer as of the archived research date, 29 October 2026.
Fiscal quarters and quarter-end dates remain explicit; they are not aligned calendar periods.

## Deployed resources

- Project: `petrosoft-2202` (157951168487).
- Private, versioned archive: `gs://finrail-reports-petrosoft-2202/2026-10-29/`, Mumbai.
- Managed RAG corpus: `projects/157951168487/locations/us-central1/ragCorpora/9170072111186706432`.
- RAG Engine mode: **serverless**. The API accepted this mode only in `us-central1`;
  the console and archive remain in `asia-south1`. No dedicated Spanner provisioned.
- Embeddings: `text-embedding-005`; chunk size 1,024, overlap 150, import limit 60 requests/minute.
- Ten indexed files: four original PDFs and six source-preserving text extractions.
  NVIDIA's original HTML is privately archived and used for exact source-cell checks.
- PDF extracts preserve physical page markers. HTML extracts preserve table/row order
  and repeat headers across row groups. This is deterministic preprocessing followed
  by Google's default text/PDF parsing, **not Document AI Layout Parser**.
- The corpus contains no curated fact store, calculation outputs or benchmark answers.

The runtime identity has `storage.objectViewer` on this bucket only and the project
custom role `finrailReportReader` containing `aiplatform.ragCorpora.get/query` and
`aiplatform.ragFiles.get/list`. It has no report import, object write or corpus delete
permission. The Google RAG service agent has bucket read access. Existing model and
search credentials remain Secret Manager bindings.

## User workflow

Open `/reports`. Search is always scoped to the selected report's actual imported
file IDs, and every returned URI must match that report's allowlist. Search renders
excerpts; it does not synthesize or certify numerical claims.

The numerical workflow supports NVIDIA's Q1/Q2 FY2027 FCF:

1. Google retrieves actual source excerpts for the selected report.
2. Nebius proposes typed values and verbatim evidence quotes; document content is
   never treated as instructions. This is a recorded model call, not a fixture.
3. The original HTML is fetched privately, its SHA-256 checked, and its table headers
   checked for the current quarter and units. Only the first numeric column in the
   exact cash-flow row can support an input; comparative/year-to-date values fail.
4. Each typed amount must match the cited quote and exact archived cell.
5. Python `Decimal` calculates OCF minus cash PPE/intangible purchases minus related
   principal payments. The result must reconcile exactly to issuer-reported FCF.

The separate simple-FCF definition omits principal payments and is clearly labelled;
it is never presented as NVIDIA's FCF. The two issuer-defined results are $48,554m and
$21,341m. They are evaluator/reference results here, not extraction-model inputs.
Checks establish traceability to the published figures, not an audit of the issuer.

Unsupported metrics, unknown sources, missing inputs, mismatched units, wrong columns,
altered source bytes, failed services and empty retrieval fail explicitly. There is
no local fixture fallback. Other companies currently support source retrieval only.

## Runtime and CLI

Set `FINRAIL_RAG_CORPUS` to the resource above. Production authenticates with the
Cloud Run workload identity. `FINRAIL_GCP_ACCESS_TOKEN` is an optional short-lived
operator override for local verification; never commit it or set it in Cloud Run.
Keep `FINRAIL_ENGINE=nebius` and the existing Nebius secret binding.

```sh
finrail get finance.reports.catalog
finrail do finance.reports.search report_id=NBIS-Q2-2026 query="Operating cash flow and liquidity"
finrail do finance.reports.analyze report_id=NVDA-Q2-FY2027 definition=issuer_fcf
```

Search and analysis use the same recorded dispatcher as the console and consume the
shared per-instance demo action budget. Analysis runs in a detached worker; subsequent
GET requests inspect its existing result without replaying a paid request. Keep the
existing max-one-instance and CPU-always-on Cloud Run settings for this demo.
The budget limits actions per instance, not total project billing across restarts.

## Re-ingestion

`scripts/ingest_public_reports.py` accepts the curated source bundle containing
`sources.json`, `raw/` and PDF `working/*.txt`. It verifies approved fingerprints,
archives originals and source excerpts without overwriting different bytes, and
starts a managed import. Supply an authenticated operator, existing private bucket
and corpus. It never creates IAM grants, changes RAG mode, or uploads answer files.
Poll the returned operation until done; require all ten files imported, list their
`gcsSource.uris`, and update each report's `rag_file_ids` only from matching URIs.
Runtime metadata is in `finrail/extensions/finance/public_data/reports.json`.

Do not recreate the corpus for ordinary imports. Do not switch a populated region's
database mode without reviewing its other users. Serverless avoids a dedicated
Spanner instance but embeddings, retrieval, storage and the model can still incur costs.

## Verification

`tests/test_public_reports.py` contains offline source-integrity, period, currency,
unit, accounting-definition, quotation, comparative-column, numerical and failure
contracts. Provider integration checks are run separately with real Google retrieval
and Nebius calls. The larger 36-case curated benchmark is not yet integrated or run
through this workflow; do not report its reference preparation as harness accuracy.
