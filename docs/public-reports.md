# Managed public-report demo

This extends the synthetic investment workspace with seven real issuer disclosures:
Nebius Q1/Q2 2026, NVIDIA Q1/Q2 FY2027, and ASML Q2/Q3 2026 (the latest two published
quarters per issuer as of the archived research date, 29 October 2026), plus Anthropic's
S-1 registration statement filed 26 October 2026 (see "Anthropic S-1" below).
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
  Anthropic's S-1 adds one archived HTML original (not indexed) and one indexed
  extraction under its own prefix `2026-10-29-anthropic/` once the operator step below is run.
- PDF extracts preserve physical page markers. HTML extracts preserve table/row order
  and repeat headers across row groups. This is deterministic preprocessing followed
  by Google's default text/PDF parsing, **not Document AI Layout Parser**.
- The corpus contains no curated fact store, calculation outputs or benchmark answers.

The runtime identity has `storage.objectViewer` on this bucket only and the project
custom role `finrailReportReader` containing `aiplatform.ragCorpora.get/query`,
`aiplatform.ragFiles.get/list` and `aiplatform.endpoints.predict` for query embeddings.
The last permission was confirmed necessary by a runtime-identity diagnostic; Google
requires it on the configured `text-embedding-005` model as well as corpus access.
It has no report import, object write or corpus delete
permission. The Google RAG service agent has bucket read access. Existing model and
search credentials remain Secret Manager bindings.

## User workflow

Open `/reports`. Search is always scoped to the selected report's actual imported
file IDs, and every returned URI must match that report's allowlist. Search renders
excerpts; it does not synthesize or certify numerical claims.

The numerical workflow supports NVIDIA's Q1/Q2 FY2027 FCF:

1. Google retrieves actual source excerpts for the selected report.
2. Code assigns IDs to actual retrieved financial rows. Nebius proposes typed values
   and selects those row IDs; code attaches the original quotes. The model never
   rewrites evidence. Document content is never treated as instructions. This is a
   recorded model call, not a fixture; the row index contains no computed answers.
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

Statement layouts are declared per issuer in `public_reports.PROFILES`: which archived
table holds each statement, the exact row label and sign of each figure, how the
current-period column is found, and which definitions may be computed and what each
must reconcile to. NVIDIA's profile is the original logic; Anthropic's is described below.

Unsupported metrics, unknown sources, missing inputs, mismatched units, wrong columns,
altered source bytes, failed services and empty retrieval fail explicitly. There is
no local fixture fallback. Nebius and ASML support source retrieval only.

## Anthropic S-1

Source `ANTH-S1-2026`: Anthropic, PBC, Form S-1, filed with the SEC on 26 October 2026,
EDGAR accession 0001628280-26-067168, CIK 0002130357, primary document
`anthropicpbc-sx1.htm` (14,169,255 bytes, SHA-256
`2fa24ae7704dbb535d096ee7459b765fe899ac9118762e738183a94473c9905a`). The archived bytes
are untrusted document data: never executed, never treated as instructions.

The extraction (`public_reports.extract_document`) is the whole filing in document
order: every printed line, every HTML table where it stands with rows in order and its
header rows repeated per group of twelve, and physical page markers (443 pages). It holds
no computed figures.

Source-checked figures, all for the six months ended 30 June 2026, USD millions, each
read from the exact archived cell under the header column "Six Months Ended June 30," /
"2026" (located by column span, so the 2025 comparative and the fiscal-year columns can
never be taken for it; the column is marked unaudited in the filing):

| definition | archived cell | reconciles to |
|---|---|---|
| `revenue` | Consolidated Statements of Operations (table 109), "Revenue" | "Total" of the revenue disaggregation note (table 120) |
| `net_loss` | table 109, "Net loss" | "Net loss" at the top of the Consolidated Statements of Cash Flows (table 114) |
| `operating_cash_flow` | table 114, "Net cash provided by (used in) operating activities" | "Operating activities" in the MD&A cash flow summary (table 72) |
| `capex` | table 114, "Purchases of property and equipment" | nothing: the S-1 totals it nowhere else, and the answer says so |
| `simple_fcf` | operating cash flow minus purchases of property and equipment | nothing: Anthropic does not define free cash flow, and the answer says so |

The unit line sits in the caption above each F-page statement, so the caption is checked
with the header. Table numbers are positions in the hash-checked file and are stable.

`finance.reports.multiple` divides a valuation the person supplies (`valuation_usd_m`) by
the revenue of a completed source-checked `revenue` run of this report, in Decimal, and
returns price-to-sales labelled as arithmetic on a supplied figure: not a price, not
annualised, not a forecast, not advice. It refuses without a completed revenue run. The
S-1 leaves the post-offering share count blank, so a share price cannot be converted to
a valuation from the filing; only a valuation is accepted.

### Operator ingestion (not yet run)

The runtime identity cannot import, so the owner runs this with their own account. The
bundle directory holds `sources.json` and `raw/ANTH-S1-2026.html`; the script verifies
the SHA-256 above, archives the original and the extraction under the new prefix, and
starts one managed import of the extraction only. `--only` keeps the ten existing
objects and RAG files untouched.

```sh
python3 scripts/ingest_public_reports.py "<bundle dir>" \
  --account "<operator account>" --project petrosoft-2202 \
  --bucket finrail-reports-petrosoft-2202 \
  --corpus projects/157951168487/locations/us-central1/ragCorpora/9170072111186706432 \
  --prefix 2026-10-29-anthropic --only ANTH-S1-2026 --output anthropic-ingest.json
```

Then poll the operation it prints until `done`, list the corpus files, and paste back the
`ragFiles` entries whose `gcsSource.uris` is
`gs://finrail-reports-petrosoft-2202/2026-10-29-anthropic/parsed/ANTH-S1-2026.txt`:

```sh
TOKEN=$(gcloud auth print-access-token --account "<operator account>")
curl -s -H "Authorization: Bearer $TOKEN" -H "x-goog-user-project: petrosoft-2202" \
  "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/157951168487/locations/us-central1/ragCorpora/9170072111186706432/ragFiles?pageSize=100"
```

Only the file ID whose URI matches is written into `rag_file_ids` for `ANTH-S1-2026` in
`reports.json`, with the object generations from `anthropic-ingest.json`. Until then the
catalog lists the S-1 with no RAG file IDs: search and analysis refuse with "not indexed",
and the Filings card says so.

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
A source catalogued under another prefix is refused unless `--prefix` matches it, and
`--only SOURCE_ID` restricts a run to one source.
Runtime metadata is in `finrail/extensions/finance/public_data/reports.json`.

Do not recreate the corpus for ordinary imports. Do not switch a populated region's
database mode without reviewing its other users. Serverless avoids a dedicated
Spanner instance but embeddings, retrieval, storage and the model can still incur costs.

## Verification

`tests/test_public_reports.py` contains offline source-integrity, period, currency,
unit, accounting-definition, quotation, comparative-column, numerical and failure
contracts. `tests/test_anthropic_s1.py` covers the S-1: column-span selection of the
six-month 2026 column, the row labels and signs, each definition's reconciliation, the
document extraction, the catalog entry, the router, and the multiple's arithmetic and refusals. Provider integration checks are run separately with real Google retrieval
and Nebius calls. The larger 36-case curated benchmark is not yet integrated or run
through this workflow; do not report its reference preparation as harness accuracy.
