# Real-report RAG demo contract

Request: ingest the six curated real quarterly reports into GCP managed retrieval, connect Finrail and show a verifiable finance workflow. Existing light investment-workspace design and terminal-only release controls remain.

## Required behavior
- Original reports archived in a private, versioned GCS path with SHA-256/source metadata; raw source material never treated as instructions.
- One Google-managed RAG corpus, explicit serverless mode where supported; no silent dedicated Spanner provisioning. Preserve any existing project RAG data/mode.
- Raw-report and layout-aware source excerpts can be indexed, but curated expected answers and benchmark answers are never indexed.
- Retrieval explicitly scoped to report/company/quarter; returned evidence records source, page/table, and real backend. No silent fixture fallback when configured retrieval fails.
- Nebius remains the reasoning/extraction engine. Retrieved table context becomes typed proposed facts. Deterministic source-cell checks establish period, currency, units and definition before Decimal arithmetic.
- Initial workflow: NVIDIA issuer-defined FCF for Q1/Q2 FY2027. Formula OCF - cash PPE/intangible purchases - related principal payments; 48,554 / 21,341 USD millions. Same data yields different clearly labelled simple FCF. Unsupported/wrong-period/currency claims are refused. Broader retrieval across all six reports remains available without promising arbitrary numerical computation.
- Read-only CLI capability and console page share the same dispatcher. UI shows real reports, retrieval citations, inputs, formula, observed errors, and limits. Synthetic and public-company contexts clearly separated.
- Default service account can read only Finrail's source bucket and invoke necessary retrieval/embedding APIs; no API keys copied into source. Keep existing Nebius/Tavily secret bindings.

## Verification
- Offline contract tests for backend errors, scope, malicious/unknown citations, number/period/currency mismatches, accounting definitions, zero inputs, Decimal computations and archive integrity.
- Actual GCP import/list/retrieve verified; actual Nebius extraction + source validation + calculation on a retrieved report; inspect traces.
- Package assets/data verified; existing suite passes; desktop/mobile UI and independent finish review before deployment.

## Stop conditions
A quota, unsupported region/mode or authorization failure is reported as such. Use another documented supported region within this project when needed; do not silently replace managed RAG with a local search fixture or provision scaled/dedicated infrastructure. If managed setup cannot be completed, preserve working artifacts and report the concrete remaining blocker.
