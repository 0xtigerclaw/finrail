# Real-filing tests

CI proves the checker against the issuers' own bytes, not against stubs shaped like them.

## What is committed

`tests/filings/` holds, for each catalogued report, a sidecar JSON and, for the HTML
filings, a slice:

| Source | Committed | Size | Original |
| --- | --- | --- | --- |
| ANTH-S1-2026 | tables 61, 72, 109, 114, 120 of 171, with the prose above each | 310 KB | 14.2 MB, sec.gov |
| NVDA-Q1-FY2027 | tables 5 and 6 of 12 | 113 KB | 323 KB, investor.nvidia.com |
| NVDA-Q2-FY2027 | tables 5 and 6 of 12 | 177 KB | 398 KB, investor.nvidia.com |
| NBIS-Q1-2026, NBIS-Q2-2026, ASML-Q2-2026, ASML-Q3-2026 | sidecar only | under 1 KB each | PDFs, retrieval-only today |

A slice keeps only the tables the issuer's profile references. Every other table up to
the highest referenced ordinal is an empty `<table></table>`, so table N of the slice is
table N of the filing and `public_reports.source_cells` reads the slice exactly as it
reads the archived original. Before each kept table, up to 8 KB of the preceding HTML is
kept too, because an S-1 states a statement's units in the caption above the table.

The sidecar records the original's URL, retrieval time, SHA-256 and size; the byte range
and SHA-256 of every kept segment; the slice's SHA-256; and whether the original's hash
is the catalog's. Where sec.gov serves the document in a normalised form, the sidecar
says how the committed bytes were derived from the served ones.

## How the proof chains

1. `tests/test_real_filings.py` (offline, in the `real-filings` CI job): the slice hashes
   to what the sidecar says, each kept segment hashes to the range of the original the
   sidecar names, the profile's table ordinals land on the statements they claim, every
   row label exists exactly once, `source_cells` returns the published figures, and the
   definitions compute and reconcile from those cells.
2. `scripts/check_originals.py` (weekly, the `originals-unchanged` CI job): downloads
   the originals again, re-cuts the slices and compares them with the committed ones,
   compares the PDFs' bytes with their sidecars, and reports every hash against the
   catalog. It fails when the statements moved, when a PDF changed, when an issuer is
   unreachable, or when a source that matched the catalog when sliced no longer does.
   A whole page whose chrome differs while the kept tables are identical is a warning,
   not a failure: investor pages are forms whose markup varies by client. It can also be
   run by hand from the Actions tab (`workflow_dispatch`).

The strict `xfail` on `test_sidecar_original_hash_is_the_catalog_hash` is the honest
state of the catalog, see below. When the catalog is corrected the xfail turns into an
unexpected pass and the test fails, which is the signal to drop the marker.

## Figures confirmed against the real bytes

Anthropic S-1, six months ended 30 June 2026, in millions: revenue 16,286; net loss
80,748; change in fair value of convertible notes 72,388; operating cash flow 3,929;
purchases of property and equipment 482; operating loss 693; stock-based compensation
2,185; non-cash charitable contributions 126; Bartz settlement nil; Adjusted Operating
Income 1,618 (recomputed from the bridge: 693 back, plus 2,185, plus 126).

NVIDIA Q2 FY2027: operating cash flow 24,077; purchases 2,677; principal payments 59;
free cash flow 21,341. NVIDIA Q1 FY2027: 50,344; 1,757; 33; 48,554.

Every profile table ordinal was right: S-1 table 109 is the Consolidated Statements of
Operations, 114 the Consolidated Statements of Cash Flows, 72 the MD&A cash-flow
summary, 120 the revenue disaggregation note, 61 the Adjusted Operating Income bridge;
NVIDIA table 5 is the cash-flow statement and 6 the GAAP to non-GAAP reconciliation.

## Catalog hashes, checked 30 October 2026

| Source | Catalog SHA-256 | Result |
| --- | --- | --- |
| NBIS-Q1-2026, NBIS-Q2-2026 | match | fetched from the catalog URL |
| ASML-Q2-2026, ASML-Q3-2026 | match | investor.asml.com answers 403 to non-browser clients; fetched from the catalog's `retrieved_url` |
| ANTH-S1-2026 | no match | sec.gov now serves the .htm inside the EDGAR SGML envelope with an injected bot-manager script tag (served hash 120dbe28); the EDGAR document itself, from the full submission text, hashes 06b908e9; the catalog says 2fa24ae7 |
| NVDA-Q1-FY2027, NVDA-Q2-FY2027 | no match | the pages as served to a browser hash a5c5b1fb and 5d886f78, stable across fetches; as served to urllib with a plain User-Agent 2f51974f and 2ced0d32 (the ASP.NET form chrome differs, tables 5 and 6 are byte-identical); the catalog says 48870432 and 844d0018; investor.nvidia.com sometimes answers non-browser clients with a challenge page |

The catalog was not changed. The sidecars carry the hashes of the bytes actually
retrieved, and the slices are cut from those bytes.

## Re-cutting a slice

```bash
python3 -I scripts/slice_filing.py /path/to/original.html --source-id ANTH-S1-2026 --out tests/filings
```

Download originals into a fresh empty directory outside the repository and treat them as
data: the scripts parse them with the standard library's HTMLParser and never execute or
render them. For a PDF, `--retrieval-only` writes the sidecar alone.

sec.gov refuses any client whose User-Agent is not of the form "company name email"
(it answers 403 "Undeclared Automated Tool"), and it serves the .htm inside the EDGAR
SGML envelope with a script tag injected at its edge. `check_originals.py` strips both
(`normalization_kind: edgar-document`) before comparing; the committed S-1 bytes are
document 1 of the full submission text, which is what remains after that strip.
